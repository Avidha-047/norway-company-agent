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


CREDIT = re.compile(r"(?i)laget av|levert av|utviklet av|designet av|design av|webdesign|nettside av|nettsiden er laget|"
                    r"powered by|created by|developed by|website by|site by|made by|drevet av")


def _credit_context(text, needle):
    """True when the contact detail sits in a web-agency credit ("Laget av …", "Powered by …")."""
    i = (text or "").find(needle)
    if i < 0:
        return False
    return bool(CREDIT.search(text[max(0, i - 90):i + len(needle) + 20]))


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
        if EMAIL_RE.fullmatch(e) and not BAD_EMAIL.search(e) and e not in clean and not _credit_context(text, e):
            clean.append(e)
    # only mailboxes on the site's own domain (a web agency's or a supplier's address is not the company's)
    same = [e for e in clean if site_host and registrable(e.split("@")[1]) == registrable(site_host)]
    out["emails"] = rank_emails(same)[:3]
    out["other_emails"] = [e for e in clean if e not in same][:5]
    phones = []
    for m in re.finditer(r'(?i)href=["\']tel:([^"\']+)["\']', html or ""):
        p = norm_phone(m.group(1))
        if p and p not in phones:
            phones.append(p)
    for m in PHONE_CTX.finditer(text):
        p = norm_phone((m.group(1) or "") + m.group(2))
        if p and p not in phones and not _credit_context(text, m.group(0)):
            phones.append(p)
    out["phones"] = phones[:3]
    org = json_ld_org(html)
    if org:
        out["ld"] = {
            "_raw": {k: org.get(k) for k in ("vatID", "taxID") if isinstance(org.get(k), (str, int))},
            "name": org.get("name") if isinstance(org.get("name"), str) else None,
            "telephone": org.get("telephone") if isinstance(org.get("telephone"), str) else None,
            "email": (org.get("email") or "").replace("mailto:", "") if isinstance(org.get("email"), str) else None,
            "address": _ld_address(org.get("address")),
            "founding_date": org.get("foundingDate") if isinstance(org.get("foundingDate"), str) else None,
            "same_as": [s for s in (org.get("sameAs") if isinstance(org.get("sameAs"), list) else [org.get("sameAs")]) if isinstance(s, str)][:8],
            "logo": org.get("logo") if isinstance(org.get("logo"), str) else (org.get("logo") or {}).get("url") if isinstance(org.get("logo"), dict) else None,
        }
    out["paragraphs"] = [t for t in (_text(p) for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", html or ""))
                         if 80 <= len(t) <= 600 and not re.search(r"(?i)cookie|personvern|privacy|javascript", t)][:12]
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
    out["raw"] = html or ""
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


_SUFFIX_TOKENS = {"AS", "ASA", "SA", "ANS", "DA", "BA", "NUF", "KS", "IKS", "SF", "KF", "BRL", "STI"}


def full_name_pattern(legal_name):
    """Regex for the FULL registered name, legal suffix included and in capitals ("Maleran Holding AS"):
    name words case-insensitive, at least one separator between words, whole-word boundaries."""
    toks = re.findall(r"[^\W_]+", legal_name or "", re.UNICODE)
    if not toks:
        return None
    parts = []
    for i, t in enumerate(toks):
        if i == len(toks) - 1 and t.upper() in _SUFFIX_TOKENS and len(toks) > 1:
            parts.append(re.escape(t.upper()))                   # suffix must be written as a suffix (AS, ASA)
        else:
            parts.append("(?i:" + re.escape(t) + ")")
    return re.compile(r"(?<![\w])" + r"[\s\-.,&/'’()]+".join(parts) + r"(?![\w])")


def _mod11(n):
    w = [3, 2, 7, 6, 5, 4, 3, 2]
    r = sum(int(d) * x for d, x in zip(n[:8], w)) % 11
    c = 0 if r == 0 else 11 - r
    return c != 10 and c == int(n[8])


ORGNR_ANY = re.compile(r"(?<!\d)(\d{3})[\s.\u00a0]?(\d{3})[\s.\u00a0]?(\d{3})(?!\d)")
ORGNR_CUE = re.compile(r"(?i)org\.?\s*n|organisasjonsn|foretaksreg|mva|\bno\s?$|vat|business id|reg\.?\s*nr")


def org_numbers_on(text, ld=None):
    """Distinct valid organisation numbers WRITTEN AS SUCH on a page: visible text with an org-number cue
    ("Org.nr", "MVA", "Foretaksregisteret", "NO …") right before or after it, plus JSON-LD vatID/taxID.
    Script ids and tracking numbers in raw HTML are not counted."""
    out = {}
    t = text or ""
    for m in ORGNR_ANY.finditer(t):
        n = "".join(m.groups())
        if not _mod11(n):
            continue
        before, after = t[max(0, m.start() - 30):m.start()], t[m.end():m.end() + 6]
        if ORGNR_CUE.search(before) or re.match(r"\s*(mva|MVA)", after):
            out.setdefault(n, []).append(m.start())
    for k in ("vatID", "taxID", "leiCode"):
        v = (ld or {}).get(k) if isinstance(ld, dict) else None
        d = re.sub(r"\D", "", str(v or ""))
        if len(d) == 9 and _mod11(d):
            out.setdefault(d, []).append(-1)
    return out


SECOND_LEVEL = {"kommune.no", "priv.no", "vgs.no", "fhs.no", "folkebibl.no", "museum.no", "herad.no", "stat.no",
                "dep.no", "idrett.no", "mil.no", "co.uk", "org.uk", "ac.uk", "com.au", "co.nz"}


def registrable(host_name):
    parts = (host_name or "").lower().split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host_name


def ownership(pages_text, orgnr, legal_name, registered_url=None, final_url=None, identity_text="", rec=None, ld=None,
              discovered=False):
    """Decide whether a website is this company's own. Returns (verdict, how, detail):
      verdict "own"     – proven: this org number is the only org number written on the pages; or several are
                          written but the site's own identity (title / site name / structured data) names this company;
                          or (no other org number shown) the full legal name with suffix, or the registered
                          e-mail/phone, appears.
      verdict "shared"  – positive evidence it belongs to someone else: other companies' org numbers are written
                          and the site does not identify as this one. (A redirect alone only makes it "unproven".)
      Registered contact details count as proof only for the registered site (never for a discovered one), and
      the registered e-mail only when it is on the site's own domain.
      verdict "unproven" – neither (e.g. a brand-only page): nothing company-specific is published from it.
    """
    nums = org_numbers_on(pages_text, ld)
    others = [n for n in nums if n != orgnr]
    pat = full_name_pattern(legal_name)
    redirected = bool(registered_url and final_url and registrable(host(registered_url)) != registrable(host(final_url)))
    identity_ok = bool(pat and pat.search(identity_text or "")) or orgnr in {re.sub(r"\D", "", str((ld or {}).get(k) or ""))
                                                                           for k in ("vatID", "taxID")}
    if orgnr in nums and not others:
        return "own", "orgnr", "the pages show this organisation number and no other"
    if orgnr in nums and identity_ok:
        return "own", "orgnr", "the pages show this organisation number and the site identifies itself as this company"
    if others:
        return "shared", None, (f"the pages show {len(others)} other organisation number(s)"
                                + (" and do not identify the site as this company" if orgnr in nums else " and not this one"))
    if redirected:
        return "unproven", None, (f"the registered address redirects to another domain ({host(final_url)}) and the "
                                  "pages do not show this organisation number")
    if pat and pat.search(pages_text):
        return "own", "name", "the pages show the company's full registered name"
    if not discovered:
        proof = contact_proof(pages_text, rec, final_url or registered_url)
        if proof:
            return "own", "contact", proof[0]
    return "unproven", None, "the pages show neither this company's organisation number, its full registered name nor its registered contact details"


def contact_proof(pages_text, rec, site_url):
    """(detail, regex) when the pages show the registered e-mail (only if it is on the site's own domain) or the
    registered phone number; None otherwise. Only used for the site registered in Enhetsregisteret."""
    reg_email = ((rec or {}).get("epostadresse") or "").strip().lower()
    site_dom = registrable(host(site_url or ""))
    if reg_email and "@" in reg_email and site_dom and registrable(reg_email.split("@", 1)[1]) == site_dom \
            and reg_email in (pages_text or "").lower():
        return (f"the pages show the e-mail address registered for this company ({reg_email})",
                "(?i)" + re.escape(reg_email))
    for ph in ((rec or {}).get("telefon"), (rec or {}).get("mobil")):
        d = re.sub(r"\D", "", str(ph or ""))[-8:]
        if len(d) == 8:
            pat = r"(?<!\d)" + r"[\s.\-]?".join(d) + r"(?!\d)"
            if re.search(pat, pages_text or ""):
                return "the pages show the phone number registered for this company", pat
    return None


SOCIAL_HOSTS = ("facebook.com", "linkedin.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "tiktok.com")


def is_social(url):
    h = host(url or "")
    return any(h == s or h.endswith("." + s) for s in SOCIAL_HOSTS)


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
