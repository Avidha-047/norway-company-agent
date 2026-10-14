"""Dated news for a company, every item tied to the exact company.

Three sources, strongest first:
  1. Brønnøysund official announcements (kunngjøringer) – fetched by organisation number.
  2. The company's own website news feed (RSS/Atom linked from its registered website).
  3. Press coverage via Bing News RSS – accepted only when the article title/snippet contains
     the exact registered legal name INCLUDING its legal-form suffix (e.g. "Maleran AS"),
     which is unique in the Norwegian Register of Business Enterprises, or the name plus an
     independent corroborator (municipality, CEO/chair surname, website domain, org number).

Every item carries: title, url, published (full YYYY-MM-DD date), source, kind, match.
"""
import datetime
import email.utils
import html as htmllib
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .util import norm_name, orgnr_pattern

KUNNGJ_LIST = "https://w2.brreg.no/kunngjoring/hent_nr.jsp?orgnr={orgnr}"
KUNNGJ_BASE = "https://w2.brreg.no/kunngjoring/"
BING_NEWS = "https://www.bing.com/news/search?q={q}&format=rss&setlang=nb&cc=NO"

# legal forms whose names are normally a person's name -> press search would match the person, not the firm
PERSONAL_FORMS = {"ENK"}
GENERIC = {
    "norge", "norsk", "norway", "service", "services", "holding", "invest", "eiendom", "consult", "consulting",
    "bygg", "transport", "drift", "utvikling", "data", "design", "media", "regnskap", "eiendomsselskap",
    "sameie", "borettslag", "boligsameie", "forening", "stiftelse", "group", "gruppen", "partner", "partners",
}
_DATE_NO = re.compile(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{4})(?!\d)")


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()


def _date_from_any(s):
    """RFC-822, ISO-8601 or dd.mm.yyyy -> 'YYYY-MM-DD' (None when no full date)."""
    if not s:
        return None
    s = s.strip()
    m = _DATE_NO.search(s)
    if m:
        d, mo, y = m.groups()
        try:
            return datetime.date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            return None
    try:
        return email.utils.parsedate_to_datetime(s).date().isoformat()
    except Exception:
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime.date(*map(int, m.groups())).isoformat()
        except ValueError:
            return None
    return None


def _item(title, url, published, source, kind, match, **extra):
    return {"title": title, "url": url, "published": published, "source": source, "kind": kind,
            "company_match": match, **extra}


# --------------------------------------------------------------------------- #
# 1. Official announcements
# --------------------------------------------------------------------------- #

def official_announcements(http, orgnr, limit=15):
    url = KUNNGJ_LIST.format(orgnr=orgnr)
    r = http.get(url, accept="text/html", retries=1, timeout=15)
    if r.status != 200:
        return [], {"source": "brreg_kunngjoringer", "url": url, "status": "error", "http_status": r.status}
    page = r.text()
    if not orgnr_pattern(orgnr).search(page):
        # the page must be about this number; otherwise publish nothing
        return [], {"source": "brreg_kunngjoringer", "url": url, "status": "not_found", "http_status": 200,
                    "note": "page did not show the organisation number"}
    items, seen = [], set()
    for row in re.split(r"(?i)<tr[\s>]", page):
        a = re.search(r'(?is)<a[^>]+href=["\']([^"\']*(?:hent_en|kunngjoring)[^"\']*)["\'][^>]*>(.*?)</a>', row)
        d = _DATE_NO.search(_clean(row))
        if not a or not d:
            continue
        title = _clean(a.group(2))
        link = urllib.parse.urljoin(KUNNGJ_BASE, htmllib.unescape(a.group(1)))
        pub = _date_from_any(d.group(0))
        if not title or not pub or link in seen:
            continue
        seen.add(link)
        items.append(_item(title, link, pub, "Brønnøysundregistrene – kunngjøringer", "official_announcement",
                           f"Official announcement listed under organisation number {orgnr}"))
    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:limit], {"source": "brreg_kunngjoringer", "url": url, "status": "ok" if items else "not_found",
                           "http_status": 200}


# --------------------------------------------------------------------------- #
# 2. Company website feed
# --------------------------------------------------------------------------- #

def find_feed(html, base_url):
    for m in re.finditer(r"(?is)<link\b[^>]*>", html or ""):
        tag = m.group(0)
        if not re.search(r'type=["\']application/(rss|atom)\+xml', tag, re.I):
            continue
        href = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        if href and "comment" not in href.group(1).lower():
            return urllib.parse.urljoin(base_url, htmllib.unescape(href.group(1)))
    return None


def _host(u):
    h = (urllib.parse.urlsplit(u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _parse_feed(xml_text):
    out = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", errors="replace"))
    except ET.ParseError:
        return out
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        get = lambda name: next((c for c in el if c.tag.split("}")[-1] == name), None)
        t = get("title")
        link_el = get("link")
        link = None
        if link_el is not None:
            link = (link_el.text or "").strip() or link_el.attrib.get("href")
        date_el = next((e for e in (get("pubDate"), get("published"), get("updated"), get("date")) if e is not None), None)
        out.append({"title": _clean(t.text if t is not None else ""), "url": link,
                    "published": _date_from_any(date_el.text if date_el is not None else "")})
    return out


def company_feed(http, site_url, feed_url, orgnr, limit=10):
    if not feed_url or _host(feed_url) != _host(site_url):
        return [], None
    r = http.get(feed_url, accept="application/rss+xml,application/atom+xml,application/xml,text/xml",
                 retries=0, timeout=12, max_bytes=1_500_000)
    if r.status != 200:
        return [], {"source": "company_website_feed", "url": feed_url, "status": "error", "http_status": r.status}
    items = []
    for it in _parse_feed(r.text()):
        if it["title"] and it["url"] and it["published"]:
            items.append(_item(it["title"], it["url"], it["published"], f"Company website ({_host(site_url)})",
                               "company_news", "Published in the news feed of the website registered for this "
                               f"organisation number ({orgnr}) in Enhetsregisteret"))
    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:limit], {"source": "company_website_feed", "url": feed_url,
                           "status": "ok" if items else "not_found", "http_status": 200}


# --------------------------------------------------------------------------- #
# 3. Press coverage
# --------------------------------------------------------------------------- #

def _suffix_pattern(legal_name):
    """Regex for the exact registered name incl. legal suffix, tolerant of case and spacing."""
    toks = re.findall(r"\w+", legal_name or "")
    if not toks:
        return None
    body = r"[\s\-.,&]*".join(re.escape(t) for t in toks)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.I)


def press_eligible(legal_name, form):
    if form in PERSONAL_FORMS:
        return False
    core = norm_name(legal_name)
    toks = [t for t in core.split() if t not in GENERIC]
    return len(core) >= 4 and bool(toks)


def _real_url(bing_link):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(bing_link).query)
    return (q.get("url") or [bing_link])[0]


def press_coverage(http, legal_name, form, corroborators, orgnr, limit=10):
    if not press_eligible(legal_name, form):
        return [], None
    q = urllib.parse.quote(f'"{legal_name}"')
    url = BING_NEWS.format(q=q)
    r = http.get(url, accept="application/rss+xml,text/xml", retries=1, timeout=15)
    if r.status != 200:
        return [], {"source": "press", "url": url, "status": "error", "http_status": r.status}
    exact = _suffix_pattern(legal_name)
    core = norm_name(legal_name)
    core_pat = _suffix_pattern(core) if len(core) >= 6 else None
    corr = [c for c in corroborators if c and len(c) >= 4]
    items, rejected = [], 0
    try:
        root = ET.fromstring(r.body)
    except ET.ParseError:
        return [], {"source": "press", "url": url, "status": "error", "http_status": 200, "note": "unparseable feed"}
    for el in root.iter("item"):
        title = _clean(el.findtext("title"))
        desc = _clean(el.findtext("description"))
        link = _real_url(el.findtext("link") or "")
        pub = _date_from_any(el.findtext("pubDate"))
        src = None
        for c in el:
            if c.tag.split("}")[-1] in ("Source", "source") and (c.text or "").strip():
                src = c.text.strip()
        text = f"{title} {desc}"
        reason = None
        if exact and exact.search(text):
            reason = f"Article names the exact registered company name '{legal_name}' (legal names are unique in Foretaksregisteret)"
        elif core_pat and core_pat.search(text):
            hit = next((c for c in corr if re.search(rf"(?<!\w){re.escape(c)}(?!\w)", text, re.I)), None)
            if hit:
                reason = f"Article names '{core}' and also mentions '{hit}', which matches this company's registry record"
        if orgnr_pattern(orgnr).search(text):
            reason = f"Article shows organisation number {orgnr}"
        if not reason or not pub or not link.startswith("http"):
            rejected += 1
            continue
        items.append(_item(title, link, pub, src or _host(link), "press", reason))
    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:limit], {"source": "press", "url": url, "status": "ok" if items else "not_found", "http_status": 200,
                           "note": f"{rejected} search results rejected as not provably about this company" if rejected else None}


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = (it["url"].split("#")[0].rstrip("/"), )
        tkey = (re.sub(r"\W+", " ", it["title"].lower()).strip(), it["published"])
        if key in seen or tkey in seen:
            continue
        seen.add(key)
        seen.add(tkey)
        out.append(it)
    return sorted(out, key=lambda x: x["published"], reverse=True)
