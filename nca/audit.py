"""Self-audit: re-check every published claim against its receipt before (and after) publishing.

Two layers:
  * verify_facts()  – run inside every profile build. Each fact must have a content hash and a quote that
                      is verbatim in the retrieved content AND contains the fact's value. Facts that fail are
                      withheld (moved to verification.withheld) instead of being published.
  * audit_profile() – checks a finished profile for consistency: news rules, group-site rule, conclusion and
                      summary figures, employee counts, accounts currency, translations, wording.
"""
import datetime
import html as htmllib
import json
import re

from .evidence import DERIVED, sha256

NEWS_KINDS = {"official_announcement", "company_news", "press"}
BANNED_WORDS = re.compile(r"(?i)\blatest\b|\bcurrent(ly)? (revenue|profit|result)s?\b")
GROUP_ONLY_FIELDS = {"social_profiles", "website_emails", "website_phones", "website_about", "website_contact_page",
                     "website_address", "website_logo", "website_site_name", "website_description", "news_company",
                     "website_founding_date"}


def _norm(s):
    s = htmllib.unescape(str(s))
    try:
        s = json.loads('"' + s.replace('"', '\\"') + '"') if "\\u" in s else s
    except Exception:
        pass
    return re.sub(r"\s+", " ", s).strip().lower()


def _value_tokens(field, value):
    """The part(s) of a value that must be visible in its quote."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, dict):
        if "amount" in value:
            return [str(value["amount"])]
        if isinstance(value.get("lei"), str):
            return [value["lei"]]
        if isinstance(value.get("wikidata"), str):
            return [value["wikidata"]]
        if isinstance(value.get("organisation_number"), str):
            return [value["organisation_number"]]
        if isinstance(value.get("ticker"), str):
            return [value["ticker"]]
        if isinstance(value.get("name"), str):
            return [value["name"].split()[-1]]
        if isinstance(value.get("title"), str):
            return [value["title"]]
        return []
    if isinstance(value, list):
        return _value_tokens(field, value[0]) if value else []
    v = str(value)
    if field.endswith("_url") or field in ("website", "website_discovered", "website_final_url", "logo",
                                           "wikipedia_en", "wikipedia_no", "website_contact_page", "website_logo", "address_map"):
        return []          # URLs are checked by their own derived basis
    if " – " in v:
        v = v.split(" – ")[0]
    if field == "address_coordinates":
        return re.findall(r"-?\d+\.\d+", v)
    if field in ("business_address", "postal_address", "website_address", "address_official"):
        v = v.split(", ")[0]
        m = re.fullmatch(r"(\d{4}) (.+)", v)
        if m:                                # no street line: postcode and place are separate keys in the filing
            return [m.group(1), m.group(2)]
    if field == "website_phones" or field == "phone" or field == "mobile":
        return [re.sub(r"\D", "", v)[-8:]]
    if len(v) > 80:
        v = v[:60]
    return [v]


def _bool_ok(field, value, quote):
    if not isinstance(value, bool):
        return True
    if field in DERIVED:
        return True
    lit = "true" if value else "false"
    return re.search(r":\s*" + lit + r"\b", quote or "") is not None


def check_fact(f, content_text=None):
    """List of problems with one fact's receipt ([] = fine)."""
    probs = []
    ref = f.get("reference") or {}
    status = ref.get("quote_status") or ""
    if not ref.get("content_sha256"):
        probs.append("no content hash")
    if not ref.get("quote"):
        probs.append("no supporting quote")
        return probs
    if f["field"] in DERIVED or status.startswith("derived"):
        if not ref.get("basis"):
            probs.append("derived fact without a stated basis")
        return probs
    q = ref["quote"]
    if content_text is not None and not status.endswith("page_text") and q not in content_text:
        probs.append("quote is not verbatim in the retrieved content")
    if not _bool_ok(f["field"], f["value"], q):
        probs.append("quoted flag does not match the published true/false")
    qn = " ".join((_norm(q), _norm(re.sub(r'"\s*,\s*"', " ", q)),      # a quoted JSON list reads as its joined text
                   _norm(re.sub(r'"\s*,\s*"', ", ", q))))
    qd = re.sub(r"\D", "", q)
    for tok in _value_tokens(f["field"], f["value"]):
        if tok.isdigit():
            if tok not in qd and tok not in q:
                probs.append(f"value {tok} not found in its quote")
        elif _norm(tok) not in qn:
            probs.append(f"value '{tok[:40]}' not found in its quote")
    return probs


def verify_facts(facts, lookup):
    """Withhold any fact whose receipt does not prove it. Returns the withheld list (facts is edited in place)."""
    withheld = []
    for f in list(facts):
        if f.get("stale"):
            continue
        if f.get("category") == "news" and isinstance(f.get("value"), list):
            keep = [it for it in f["value"] if (it.get("reference") or {}).get("content_sha256") and (it.get("reference") or {}).get("quote")]
            for it in f["value"]:
                if it not in keep:
                    withheld.append({"field": f["field"], "value": it.get("title"), "reason": "news item without a verifiable receipt"})
            f["value"] = keep
            if not keep:
                facts.remove(f)
                continue
        ref = f.get("reference") or {}
        res = lookup(ref.get("url")) if ref.get("url") else None
        text = None
        if res is not None and getattr(res, "body", None):
            if sha256(res.body) != ref.get("content_sha256"):
                withheld.append({"field": f["field"], "value": f["value"], "reason": "content hash mismatch"})
                facts.remove(f)
                continue
            try:
                text = res.text()
            except Exception:
                text = None
        probs = check_fact(f, text)
        if probs:
            withheld.append({"field": f["field"], "label": f.get("label"), "value": f["value"], "reason": "; ".join(probs),
                             "source_url": f.get("source_url")})
            facts.remove(f)
    return withheld


def _amounts(facts):
    out = set()
    for f in facts:
        v = f.get("value")
        if isinstance(v, dict) and "amount" in v:
            out.add(int(v["amount"]) if float(v["amount"]).is_integer() else v["amount"])
    return out


def audit_profile(p):
    """Consistency checks on a finished profile. Returns a list of {severity, check, detail}."""
    issues = []
    add = lambda sev, chk, det: issues.append({"severity": sev, "check": chk, "detail": det})
    facts = p.get("facts") or []
    fields = {}
    for f in facts:
        if f["field"] in fields:
            add("medium", "duplicate fact", f["field"])
        fields[f["field"]] = f
        for pr in check_fact(f):
            add("high", "receipt", f"{f['field']}: {pr}")
    today = datetime.date.today().isoformat()
    # employees
    e = fields.get("employees")
    if e is not None and e["value"] == 0 and '"antallAnsatte": 0' not in ((e.get("reference") or {}).get("quote") or "").replace(":0", ": 0"):
        add("high", "employees", "0 employees published without a quoted registered count of 0")
    # accounts currency
    per, last = (fields.get("accounts_period") or {}).get("value"), (fields.get("last_annual_accounts_year") or {}).get("value")
    if per and last and str(last).isdigit() and int(str(last)) > int(str(per)[-10:-6]) and "accounts_currency_note" not in fields:
        add("high", "accounts currency", f"newer filing {last} registered but figures for {per[-10:-6]} are not flagged")
    for f in facts:
        v = f.get("value")
        if isinstance(v, dict) and "amount" in v and not v.get("period"):
            add("high", "accounts period", f"{f['field']} has no accounting period")
    # group sites
    own = (fields.get("website_ownership") or {}).get("value") or ""
    if own.startswith(("shared or parent-group", "ownership not proven")):
        for g in GROUP_ONLY_FIELDS & set(fields):
            add("high", "group website", f"{g} published although the website is a shared/group site")
    # news
    from .news import translate_announcement
    for it in p.get("news") or []:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(it.get("published") or "")):
            add("high", "news date", f"no full publication date: {it.get('title')}")
        elif it["published"] > today:
            add("high", "news date", f"publication date in the future: {it['published']} {it.get('title')}")
        if it.get("kind") not in NEWS_KINDS or not it.get("company_match") or not str(it.get("url", "")).startswith("http"):
            add("high", "news item", f"incomplete item: {it.get('title')}")
        if it.get("title_en") and translate_announcement(it.get("title")) != it["title_en"]:
            add("high", "translation", f"'{it.get('title')}' -> '{it['title_en']}' is not the exact category translation")
        if it.get("kind") == "company_news" and own.startswith(("shared", "ownership not proven")):
            add("high", "group website", f"company news from a shared/group site: {it.get('title')}")
        if not (it.get("reference") or {}).get("content_sha256") or not (it.get("reference") or {}).get("quote"):
            add("high", "news receipt", f"news item without hash/quote: {it.get('title')}")
    # conclusion and text figures
    amounts = _amounts(facts)
    texts = [("summary", p.get("summary") or ""), ("conclusion", (p.get("conclusion") or {}).get("text") or "")] + \
            [("key_points", k) for k in p.get("key_points") or []]
    for name, t in texts:
        if BANNED_WORDS.search(t):
            add("medium", "wording", f"{name} uses wording that implies currency: '{BANNED_WORDS.search(t).group(0)}'")
        for m in re.finditer(r"NOK (-?[\d ]{1,30}\d)", t):
            n = int(m.group(1).replace(" ", ""))
            if n not in amounts and -n not in amounts:
                add("high", "figure", f"{name} states NOK {m.group(1)} which is not an exact published amount")
        if re.search(r"\b0 registered employees\b", t) and (e is None or e["value"] != 0):
            add("high", "employees", f"{name} claims 0 employees")
    for c in (p.get("conclusion") or {}).get("claims") or []:
        if not c.get("supported_by"):
            add("high", "conclusion", f"unsupported claim: {c.get('claim')}")
        for s in c.get("supported_by") or []:
            if s.get("field") not in fields and s.get("field") not in ("news", "risk_signals"):
                add("high", "conclusion", f"claim cites a fact that is not published: {s.get('field')}")
    return issues
