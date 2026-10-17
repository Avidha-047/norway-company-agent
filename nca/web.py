"""Company website: discovery for companies without a registered site, and rich fact extraction.

Discovery rule (protects precision): a search-result website is accepted only when
  * the page (home or contact page) shows the organisation number                -> high confidence, or
  * the page names the company AND shows its registered postcode AND street name  -> medium confidence.
Directory/aggregator sites (proff, gulesider, brreg, social networks…) are never treated as the company's site.
"""
import html as htmllib
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .util import norm_name

BING_WEB = "https://www.bing.com/search?format=rss&q={q}&setlang=nb&cc=NO"

DIRECTORY_HOSTS = (
    "brreg.no", "proff.no", "purehelp.no", "gulesider.no", "1881.no", "180.no", "finn.no", "facebook.com",
    "linkedin.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "wikipedia.org", "wikidata.org",
    "regnskapstall.no", "firmanett.no", "bedriftsdatabasen.no", "kununu.com", "allabolag.se", "dnb.com",
    "opencorporates.com", "northdata.com", "northdata.de", "infobel.com", "yelp.com", "tripadvisor.com",
    "google.com", "bing.com", "mapquest.com", "kompass.com", "enirogule", "eniro.no", "hitta.se", "krafta.no",
    "forvalt.no", "ravninfo", "cylex", "bizzdo", "nettavisen.no", "vg.no", "dagbladet.no", "aftenposten.no",
    "e24.no", "dn.no", "nrk.no", "tv2.no", "lovdata.no", "regjeringen.no", "ssb.no", "unstop.com",
    "seeklogo", "apple.com", "amazon.", "ebay.", "pinterest.", "tiktok.com", "reddit.com", "trustpilot.",
    "legelisten.no", "mittanbud.no", "bygg.no", "anbud365", "eiendomspriser", "hjemla.no", "norgeshandel",
    "firmasjekk", "proff.", "virksomhet.", "data.norge.no", "skatteetaten.no",
)
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}(?![\w-])")
PHONE_CTX = re.compile(r"(?i)(?:tlf|telefon|tel|phone|mob|mobil|ring)\W{0,12}((?:\+|00)?47[\s.]?)?(\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}|\d{3}[\s.]?\d{2}[\s.]?\d{3})(?!\d)")
CONTACT_LINK = re.compile(r'(?is)<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>')
CONTACT_WORDS = re.compile(r"(?i)kontakt|contact|om[\s-]?oss|about")
BAD_EMAIL = re.compile(r"(?i)\.(png|jpe?g|gif|svg|webp)$|example\.|sentry|wixpress|@2x|domain\.|yourdomain|email\.com$|godaddy")


GENERIC_MAILBOX = ("post", "postmottak", "firmapost", "info", "kontakt", "contact", "mail", "hei", "hello",
                   "office", "kundeservice", "service", "salg", "sales", "booking", "resepsjon")


def rank_emails(emails):
    """General company mailboxes (post@, info@, kontakt@ …) first, then the rest in page order."""
    def key(e):
        local = e.split("@")[0]
        return (0 if local in GENERIC_MAILBOX else 1 if any(local.startswith(g) for g in GENERIC_MAILBOX) else 2)
    return sorted(emails, key=key)


def host(u):
    h = (urllib.parse.urlsplit(u if "://" in u else "https://" + u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def is_directory(u):
    h = host(u)
    return any(d in h for d in DIRECTORY_HOSTS)


def _text(html):
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html or "")
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def norm_phone(digits_with_prefix):
    d = re.sub(r"\D", "", digits_with_prefix)
    if d.startswith("0047"):
        d = d[4:]
    elif d.startswith("47") and len(d) == 10:
        d = d[2:]
    if len(d) != 8 or d[0] not in "2345679":
        return None
    return f"+47 {d[:2]} {d[2:4]} {d[4:6]} {d[6:]}"


def json_ld_org(html):
    """First Organization/LocalBusiness-like JSON-LD node."""
    for m in re.finditer(r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html or ""):
        try:
            data = json.loads(htmllib.unescape(m.group(1).strip()))
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, dict):
                if "@graph" in node and isinstance(node["@graph"], list):
                    stack.extend(node["@graph"])
                t = node.get("@type")
                types = t if isinstance(t, list) else [t]
                skip = {"WebSite", "WebPage", "BreadcrumbList", "ImageObject", "SearchAction", "Person", "Article",
                        "BlogPosting", "Product", "Offer", "FAQPage", "Question", "Answer", "ListItem", "Event"}
                named_types = [x for x in types if isinstance(x, str)]
                if named_types and not any(x in skip for x in named_types) and (
                        any(k in node for k in ("telephone", "address", "email", "sameAs", "logo"))):
                    return node
            elif isinstance(node, list):
                stack.extend(node)
    return None


def _ld_address(a):
    if isinstance(a, list):
        a = a[0] if a else None
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        parts = [a.get("streetAddress"), " ".join(x for x in (a.get("postalCode"), a.get("addressLocality")) if x)]
        return ", ".join(p for p in parts if p) or None
    return None


def extract(html, base_url, site_host):
    """Contact and descriptive facts from one page."""
    out = {}
    text = _text(html)
    emails = []
    for m in re.finditer(r'(?i)mailto:([^"\'?>\s]+)', html or ""):
        emails.append(urllib.parse.unquote(m.group(1)))
    emails += EMAIL_RE.findall(text)
    clean = []
    for e in emails:
        e = e.strip().strip(".").lower()
        if EMAIL_RE.fullmatch(e) and not BAD_EMAIL.search(e) and e not in clean:
            clean.append(e)
    same = [e for e in clean if site_host and site_host.split(".")[-2] in e.split("@")[1]]
    out["emails"] = rank_emails(same or clean)[:3]
    phones = []
    for m in re.finditer(r'(?i)href=["\']tel:([^"\']+)["\']', html or ""):
        p = norm_phone(m.group(1))
        if p and p not in phones:
            phones.append(p)
    for m in PHONE_CTX.finditer(text):
        p = norm_phone((m.group(1) or "") + m.group(2))
        if p and p not in phones:
            phones.append(p)
    out["phones"] = phones[:3]
    org = json_ld_org(html)
    if org:
        out["ld"] = {
            "name": org.get("name") if isinstance(org.get("name"), str) else None,
            "telephone": org.get("telephone") if isinstance(org.get("telephone"), str) else None,
            "email": (org.get("email") or "").replace("mailto:", "") if isinstance(org.get("email"), str) else None,
            "address": _ld_address(org.get("address")),
            "founding_date": org.get("foundingDate") if isinstance(org.get("foundingDate"), str) else None,
            "same_as": [s for s in (org.get("sameAs") if isinstance(org.get("sameAs"), list) else [org.get("sameAs")]) if isinstance(s, str)][:8],
            "logo": org.get("logo") if isinstance(org.get("logo"), str) else (org.get("logo") or {}).get("url") if isinstance(org.get("logo"), dict) else None,
        }
    about = None
    for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", html or ""):
        t = _text(p)
        if 80 <= len(t) <= 600 and not re.search(r"(?i)cookie|personvern|privacy|javascript", t):
            about = t
            break
    out["about"] = about
    contact = None
    for m in CONTACT_LINK.finditer(html or ""):
        href, label = m.group(1), _text(m.group(2))
        if CONTACT_WORDS.search(label) or CONTACT_WORDS.search(href):
            u = urllib.parse.urljoin(base_url, htmllib.unescape(href))
            if host(u) == site_host and u.rstrip("/") != base_url.rstrip("/"):
                contact = u
                if re.search(r"(?i)kontakt|contact", label + href):
                    break
    out["contact_page"] = contact
    out["text"] = text
    return out


def address_match(text, rec):
    """Registered postcode and street name both appear on the page."""
    addr = (rec or {}).get("forretningsadresse") or (rec or {}).get("beliggenhetsadresse") or {}
    pc = addr.get("postnummer")
    lines = [l for l in (addr.get("adresse") or []) if l]
    street = re.sub(r"\s*\d.*$", "", lines[0]).strip() if lines else ""
    if not pc or len(street) < 4:
        return None
    if re.search(rf"(?<!\d){pc}(?!\d)", text) and re.search(re.escape(street), text, re.I):
        return f"{lines[0]}, {pc} {(addr.get('poststed') or '').title()}".strip()
    return None


def name_on_page(text, legal_name):
    core = norm_name(legal_name)
    if len(core) < 4:
        return False
    toks = re.findall(r"\w+", core)
    pat = r"[\s\-.,&]*".join(re.escape(t) for t in toks)
    return bool(re.search(rf"(?<!\w){pat}(?!\w)", text, re.I))


DDG_HTML = "https://html.duckduckgo.com/html/?q={q}&kl=no-no"


def _bing_links(body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    return [(it.findtext("link") or "").strip() for it in root.iter("item")]


def _ddg_links(html):
    links = []
    for m in re.finditer(r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"', html or ""):
        href = htmllib.unescape(m.group(1))
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("uddg", [""])[0])
        links.append(href)
    return links


_TRANS = str.maketrans({"æ": "ae", "ø": "o", "å": "a", "é": "e", "ü": "u", "ö": "o", "ä": "a"})


def guess_domains(legal_name, limit=3):
    """Likely own domains from the registered name, e.g. 'MALERAN AS' -> maleran.no, maleran.com."""
    toks = [t.translate(_TRANS) for t in norm_name(legal_name).split()]
    toks = [re.sub(r"[^a-z0-9]", "", t) for t in toks]
    toks = [t for t in toks if t]
    if not toks:
        return []
    joined, hyph = "".join(toks), "-".join(toks)
    if len(joined) < 4 or len(joined) > 40:
        return []
    if all(t in GENERIC_WORDS for t in toks):
        return []
    out = [f"{joined}.no"]
    if len(toks) > 1:
        out.append(f"{hyph}.no")
    out.append(f"{joined}.com")
    return [f"https://{d}/" for d in out[:limit]]


GENERIC_WORDS = {"invest", "holding", "eiendom", "service", "bygg", "transport", "consult", "consulting", "drift",
                 "data", "design", "media", "regnskap", "norge", "norway", "group", "gruppen", "partner", "partners"}


def search_candidates(http, legal_name, rec, limit=2, debug=False):
    """Up to `limit` non-directory sites from web search (Bing RSS, falling back to DuckDuckGo HTML)."""
    addr = (rec or {}).get("forretningsadresse") or (rec or {}).get("beliggenhetsadresse") or {}
    place = addr.get("poststed") or ""
    q = urllib.parse.quote(f'"{legal_name}" {place}'.strip())
    tried, links, used_url, engine = [], [], None, None
    for eng, tmpl, accept in (("bing", BING_WEB, "application/rss+xml,text/xml"), ("duckduckgo", DDG_HTML, "text/html")):
        if engine and links:
            break
        if eng == "duckduckgo" and not http.spare(1):
            break
        url = tmpl.format(q=q)
        r = http.get(url, accept=accept, retries=1, timeout=15)
        got = (_bing_links(r.body) if eng == "bing" else _ddg_links(r.text())) if r.status == 200 else None
        tried.append(f"{eng}: http {r.status}, {len(got) if got is not None else 'unreadable'} results")
        if debug:
            print(f"[{eng}] {url}\n  http {r.status}, {len(r.body)} bytes, results: {got}")
            if got == [] or got is None:
                print("  first 600 chars:", r.text()[:600].replace("\n", " "))
        if got:
            links, used_url, engine = got, url, eng
    core_toks = [t for t in norm_name(legal_name).split() if len(t) >= 3]
    cands, seen = [], set()
    for link in links:
        if not link.startswith("http") or is_directory(link):
            continue
        h = host(link)
        if h in seen:
            continue
        seen.add(h)
        score = sum(1 for t in core_toks if t in h.replace("-", ""))
        cands.append((score, f"https://{urllib.parse.urlsplit(link).netloc}/"))
    cands.sort(key=lambda x: -x[0])
    note = "; ".join(tried) + f"; {len(cands)} candidate site(s)"
    return [c for _, c in cands[:limit]], {"source": "website_discovery", "url": used_url or (BING_WEB.format(q=q)),
                                           "status": "ok" if links else "error", "http_status": 200, "note": note}
