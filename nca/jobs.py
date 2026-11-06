"""Job postings from the careers page of a company's *verified* own website.

The Signalpost source policy lists careers pages on the verified company site as a preferred, company-owned
source. Nothing here guesses: a posting is published only when the careers page itself lists it, either as
schema.org JobPosting structured data or as a link to a recognised applicant-tracking system (or a deeper page
under the careers path) whose link text reads like a job title. The page is quoted as the evidence.
"""
import html as htmllib
import json
import re
import urllib.parse

CAREER_PATH = re.compile(r"(?i)/(karriere|jobb|jobbe-hos-oss|jobb-hos-oss|ledige-stillinger|ledige_stillinger|stillinger|"
                         r"stilling|career|careers|jobs|join-us|work-with-us|vacancies|rekruttering)(?:[/?#.]|$)")
CAREER_TEXT = re.compile(r"(?i)^\s*(karriere|ledige stillinger|jobb hos oss|jobbe hos oss|jobb i [\w\- ]+|stillinger|"
                         r"careers?|jobs|join us|work with us|vacancies|ledige jobber)\s*$")
# applicant-tracking systems used by Norwegian employers: a link there is an individual job advert
ATS = re.compile(r"(?i)^https?://([\w-]+\.)*(webcruiter\.(no|com)|jobbnorge\.no|teamtailor\.com|recman\.(no|page)|"
                 r"hrmanager\.no|easycruit\.com|jobylon\.com|reachmee\.com|simployer\.com|finn\.no|"
                 r"varbi\.com|talentech\.com|emply\.com|workday(jobs)?\.com|smartrecruiters\.com|"
                 r"lever\.co|greenhouse\.io|personio\.(de|com))/")
ATS_AD = re.compile(r"(?i)(/\d{3,}(?:[/?#-]|$)|/job/|/jobs?/\d|/ad\b|finnkode=|/stilling|/vacanc|/position|/apply|/annonse|jobId=|"
                    r"ShowJob|/jobs/[\w-]+)")
GENERIC = re.compile(r"(?i)^(les mer|se mer|søk( her| nå)?|apply( now)?|søk på stillingen|se alle( ledige)?( stillinger)?|"
                     r"ledige stillinger|stillinger|karriere|jobb|jobs?|careers?|klikk her|mer info|åpen søknad|"
                     r"open application|send( en)? åpen søknad|tilbake|back|kontakt( oss)?|hjem|home|meny|menu|"
                     r"facebook|linkedin|instagram|twitter|personvern|cookies?)\.?$")
# a deeper page under the careers path counts as an advert only when its own address says so
AD_SLUG = re.compile(r"(?i)(\d{3,}|stilling|ledig|annonse|vacanc|position|/jobs?/|/jobb/|opening|utlysning)")
A_TAG = re.compile(r"(?is)<a\b[^>]*?href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>")


def _text(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"(?s)<[^>]+>", " ", s or ""))).strip()


def _reg(host):
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def careers_link(html, base_url):
    """The careers page linked from the home page, on the same site (or a careers sub-site of it)."""
    host = urllib.parse.urlsplit(base_url).hostname or ""
    best = None
    for href, inner in A_TAG.findall(html or ""):
        url = urllib.parse.urljoin(base_url, htmllib.unescape(href.strip()))
        h = urllib.parse.urlsplit(url).hostname or ""
        if not url.startswith("http") or _reg(h) != _reg(host):
            continue
        path = urllib.parse.urlsplit(url).path or "/"
        label = _text(inner)
        if CAREER_PATH.search(path + "/") or CAREER_TEXT.match(label):
            # prefer the shortest careers path (the index page rather than one advert)
            if best is None or len(path) < len(urllib.parse.urlsplit(best).path or "/"):
                best = url
    return best


def _ld_postings(html):
    out = []
    for block in re.findall(r"(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html or ""):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                t = node.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    org = node.get("hiringOrganization")
                    out.append({"title": _text(str(node.get("title") or "")),
                                "url": node.get("url"),
                                "published": str(node.get("datePosted") or "")[:10] or None,
                                "valid_through": str(node.get("validThrough") or "")[:10] or None,
                                "hiring_organization": org.get("name") if isinstance(org, dict) else org})
                stack.extend(v for k, v in node.items() if k != "@context")
    return [p for p in out if p["title"]]


def _title_like(label):
    if not (4 <= len(label) <= 120) or GENERIC.match(label):
        return False
    if re.search(r"(?i)\b(cookie|personvern|privacy|logg inn|login|nyhetsbrev|newsletter)\b", label):
        return False
    return bool(re.search(r"[A-Za-zÆØÅæøå]{3}", label))


def postings(html, page_url):
    """Individual job adverts listed on a careers page. Returns a list of dicts (title, url, published?)."""
    items, seen = [], set()
    for p in _ld_postings(html):
        key = (p["title"].lower(), p.get("url"))
        if key not in seen:
            seen.add(key)
            items.append(dict(p, basis="schema.org JobPosting structured data on the careers page"))
    page = urllib.parse.urlsplit(page_url)
    base_path = (page.path or "/").rstrip("/")
    for href, inner in A_TAG.findall(html or ""):
        url = urllib.parse.urljoin(page_url, htmllib.unescape(href.strip()))
        label = _text(inner)
        if not _title_like(label):
            continue
        u = urllib.parse.urlsplit(url)
        is_ats = bool(ATS.match(url)) and bool(ATS_AD.search(u.path + ("?" + u.query if u.query else "")))
        deeper = (_reg(u.hostname or "") == _reg(page.hostname or "") and base_path and
                  (u.path or "").rstrip("/").startswith(base_path + "/") and len((u.path or "").rstrip("/")) > len(base_path) + 3
                  and CAREER_PATH.search(base_path + "/")
                  and bool(AD_SLUG.search((u.path or "")[len(base_path):])))
        if not (is_ats or deeper):
            continue
        key = (label.lower(), url)
        if key in seen or any(label.lower() == k[0] for k in seen):
            continue
        seen.add(key)
        items.append({"title": label, "url": url, "published": None,
                      "basis": "link from the careers page to an applicant-tracking system advert" if is_ats
                      else "advert page under the careers section of the company's own site"})
    return items[:25]
