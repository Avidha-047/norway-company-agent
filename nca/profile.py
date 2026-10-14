"""Builds one company profile: fetch -> verify -> explain -> diff against the previous run."""
import json
import time

import datetime

from . import news as N
from . import sources as S
from .facts import SOURCES, FactSet
from .http import BudgetExceeded
from .util import fmt_money, fmt_orgnr, normalize_orgnr, now_iso, valid_orgnr

SCHEMA_VERSION = "2.0"
# Legal forms that are normally not obliged to file annual accounts in Norway.
NO_ACCOUNTS_DUTY = {"ENK", "ANS", "DA", "FLI", "SAM", "KIRK", "ORGL", "STAT", "FYLK", "KOMM", "IKS", "ESEK", "PERS", "UTLA", "VPFO"}


def build_profile(http, query, previous=None, use_web=True, use_wikidata=True, use_news=True):
    started = time.time()
    used_before = http.used
    orgnr = normalize_orgnr(query)
    prof = {
        "schema_version": SCHEMA_VERSION,
        "query": str(query),
        "organisation_number": orgnr or None,
        "generated_at": now_iso(),
    }
    if not valid_orgnr(orgnr):
        prof.update(status="invalid_organisation_number", name=None, facts=[], sources=[],
                    summary=f"'{query}' is not a valid Norwegian organisation number (9 digits with a modulus-11 check digit). No lookup was made.",
                    gaps=[], verification={"rejected": []}, changes=[])
        return prof

    fs = FactSet()
    statuses = []
    rejections = []
    complete = True
    kind = rec = None
    try:
        kind, rec, api_url, st = S.fetch_entity(http, orgnr)
        statuses.append(st)
        if kind is None:
            status = "not_found" if st["status"] == "not_found" else "lookup_failed"
            prof.update(status=status, name=None)
        else:
            S.entity_facts(fs, orgnr, kind, rec, api_url)
            prof["status"] = S.status_of(kind, rec)
            prof["entity_type"] = "sub-unit (establishment)" if "underenhet" in kind else "legal entity"
            prof["name"] = rec.get("navn")
            form = (rec.get("organisasjonsform") or {}).get("kode")
            if kind == "enhet":
                statuses.append(S.roles_facts(http, fs, orgnr))
                statuses.append(S.subunits_facts(http, fs, orgnr))
                if rec.get("sisteInnsendteAarsregnskap") or form not in NO_ACCOUNTS_DUTY:
                    statuses.append(S.accounts_facts(http, fs, orgnr))
            if kind in ("enhet", "deleted"):
                gst, lei = S.gleif_facts(http, fs, orgnr, prof["name"], rejections)
                statuses.append(gst)
            else:
                lei = None
            if use_wikidata and kind in ("enhet", "deleted"):
                wst, _ = S.wikidata_facts(http, fs, orgnr, prof["name"], lei, rejections)
                statuses.append(wst)
            site = rec.get("hjemmeside") or fs.get("official_website")
            wst = None
            if use_web and site and prof["status"] != "deleted":
                wst = S.website_facts(http, fs, orgnr, site)
                if wst:
                    statuses.append(wst)
            if use_news and kind in ("enhet", "deleted"):
                collect_news(http, fs, statuses, orgnr, rec, site, wst)
    except BudgetExceeded as e:
        complete = False
        statuses.append(S.SourceStatus.make("agent", None, "stopped", note=str(e)))
        prof.setdefault("status", "lookup_incomplete")
        prof.setdefault("name", rec.get("navn") if rec else None)

    facts = fs.items
    ok_sources = {s["source"] for s in statuses if s and s.get("status") in ("ok", "not_found")}
    changes, facts = diff_against(previous, facts, ok_sources, complete)
    prof["facts"] = facts
    prof["profile"] = flatten(facts)
    prof["news"] = N.dedupe([it for f in facts if f["field"].startswith("news_") and isinstance(f["value"], list)
                             for it in f["value"]])
    prof["key_points"] = key_points(prof, fs)
    prof["risk_signals"] = risk_signals(prof, fs, rec)
    prof["summary"] = explain(prof, fs)
    prof["gaps"] = gaps(prof, kind, rec, statuses)
    prof["verification"] = {
        "method": "Every fact comes from a record fetched by the exact organisation number. Secondary sources "
                  "(GLEIF, Wikidata) are only accepted when their record names this number AND the legal name or LEI agrees; "
                  "financial values are copied as filed and never estimated.",
        "checks": verification_checks(orgnr, fs, prof.get("name")),
        "rejected": rejections,
    }
    prof["sources"] = [dict(s, source_name=SOURCES.get(s.get("source"), {}).get("name"),
                            licence=SOURCES.get(s.get("source"), {}).get("licence")) for s in statuses if s]
    prof["changes"] = changes
    prof["history"] = ((previous or {}).get("history") or []) + ([{"at": prof["generated_at"], "changes": changes}] if changes and previous else [])
    prof["history"] = prof["history"][-30:]
    prof["complete"] = complete
    prof["stats"] = {"requests": http.used - used_before, "seconds": round(time.time() - started, 2), "fact_count": len(facts)}
    return prof


NEWS_FACTS = {
    "brreg_kunngjoringer": ("news_official", "Official announcements (Brønnøysund kunngjøringer)"),
    "company_website_feed": ("news_company", "News from the company's own website"),
    "press": ("news_press", "Press coverage naming this exact company"),
}


def collect_news(http, fs, statuses, orgnr, rec, site, wst):
    form = ((rec or {}).get("organisasjonsform") or {}).get("kode")
    batches = []
    items, st = N.official_announcements(http, orgnr)
    batches.append(("brreg_kunngjoringer", items, st))
    if site and wst and wst.get("feed_url"):
        items, st = N.company_feed(http, wst.get("url") or site, wst["feed_url"], orgnr)
        batches.append(("company_website_feed", items, st))
    corr = []
    addr = (rec or {}).get("forretningsadresse") or (rec or {}).get("beliggenhetsadresse") or {}
    corr += [addr.get("kommune"), addr.get("poststed")]
    for role in ("ceo", "board_chair"):
        v = fs.get(role)
        if isinstance(v, dict) and v.get("name"):
            corr.append(v["name"].split()[-1])
    if site:
        corr.append(N._host(site if "://" in site else "https://" + site))
    for l in fs.get("stock_listings") or []:
        corr.append(l.get("ticker"))
    if http.remaining() > 40:
        items, st = N.press_coverage(http, (rec or {}).get("navn"), form, corr, orgnr)
        batches.append(("press", items, st))
    for src, items, st in batches:
        if st:
            statuses.append(S.SourceStatus(**{k: v for k, v in st.items() if v is not None}))
        if items:
            field, label = NEWS_FACTS[src]
            newest = items[0]["published"]
            fs.add(field, label, items, src, st["url"], evidence_url=items[0]["url"], as_of=newest,
                   category="news", confidence="high" if src != "press" else "medium",
                   match="; ".join(sorted({i["company_match"] for i in items}))[:400],
                   note=f"{len(items)} dated items; each item has title, source URL, full publication date and its own company-match reason")


NACE_SECTIONS = [
    (1, 3, "A", "Agriculture, forestry and fishing"), (5, 9, "B", "Mining and quarrying"),
    (10, 33, "C", "Manufacturing"), (35, 35, "D", "Electricity, gas and heating supply"),
    (36, 39, "E", "Water supply, sewerage and waste management"), (41, 43, "F", "Construction"),
    (45, 47, "G", "Wholesale and retail trade"), (49, 53, "H", "Transportation and storage"),
    (55, 56, "I", "Accommodation and food services"), (58, 63, "J", "Information and communication"),
    (64, 66, "K", "Financial and insurance activities"), (68, 68, "L", "Real estate activities"),
    (69, 75, "M", "Professional, scientific and technical activities"),
    (77, 82, "N", "Administrative and support services"), (84, 84, "O", "Public administration"),
    (85, 85, "P", "Education"), (86, 88, "Q", "Health and social work"),
    (90, 93, "R", "Arts, entertainment and recreation"), (94, 96, "S", "Other service activities"),
    (97, 98, "T", "Activities of households"), (99, 99, "U", "Extraterritorial organisations"),
]


def nace_sector(code):
    try:
        div = int(str(code).split(".")[0])
    except ValueError:
        return None
    for lo, hi, sec, name in NACE_SECTIONS:
        if lo <= div <= hi:
            return f"{sec} – {name}"
    return None


def _years_since(d):
    try:
        y, m, dd = map(int, str(d)[:10].split("-"))
        days = (datetime.date.today() - datetime.date(y, m, dd)).days
        return days // 365
    except Exception:
        return None


def size_class(emp):
    if not isinstance(emp, int):
        return None
    if emp == 0:
        return "no registered employees"
    return "micro (1–9 employees)" if emp < 10 else "small (10–49)" if emp < 50 else "medium (50–249)" if emp < 250 else "large (250+)"


def key_points(prof, fs):
    pts = []
    if prof.get("status") in (None, "not_found", "invalid_organisation_number", "lookup_failed"):
        return pts
    ind = fs.get("industry_code_1")
    if ind:
        sec = nace_sector(ind.split(" – ")[0])
        pts.append(f"Sector: {sec}" + (f" (NACE {ind})" if ind else "") if sec else f"Industry: NACE {ind}")
    age = _years_since(fs.get("founded_date") or fs.get("registered_date"))
    if age is not None:
        pts.append(f"Age: about {age} years (founded/registered {fs.get('founded_date') or fs.get('registered_date')})")
    sc = size_class(fs.get("employees"))
    if sc:
        pts.append(f"Size: {sc}" + (f", {fs.get('subunit_count')} registered establishments" if fs.get("subunit_count") else ""))
    rev, ni, eq = _val(fs, "revenue"), _val(fs, "net_income"), _val(fs, "equity")
    period = fs.get("accounts_period")
    if period and rev is not None:
        yr = str(period)[-10:-6]
        bits = [f"revenue {fmt_money(rev)}"]
        if ni is not None:
            bits.append(("profit " if ni >= 0 else "loss ") + fmt_money(abs(ni)))
        if eq is not None:
            bits.append(f"equity {fmt_money(eq)}")
        pts.append(f"Financials {yr} (as filed): " + ", ".join(bits))
    ceo, chair = fs.get("ceo"), fs.get("board_chair")
    lead = []
    if isinstance(ceo, dict):
        lead.append(f"CEO {ceo['name']}")
    if isinstance(chair, dict):
        lead.append(f"chair {chair['name']}")
    if lead:
        pts.append("Leadership: " + ", ".join(lead))
    if prof.get("news"):
        n = prof["news"][0]
        pts.append(f"Latest dated item: \"{n['title']}\" ({n['published']}, {n['source']})")
    if fs.get("stock_listings"):
        l = fs.get("stock_listings")[0]
        pts.append(f"Listed on {l['exchange']}" + (f" as {l['ticker']}" if l.get("ticker") else ""))
    return pts


def risk_signals(prof, fs, rec):
    sig = []
    st = prof.get("status")
    if st in ("bankrupt", "under_liquidation", "compulsory_liquidation", "deleted"):
        sig.append({"signal": st.replace("_", " "), "severity": "high", "evidence": "Enhetsregisteret status flags"})
    ni, eq = _val(fs, "net_income"), _val(fs, "equity")
    period = fs.get("accounts_period")
    if isinstance(eq, (int, float)) and eq < 0:
        sig.append({"signal": "negative equity in latest filed accounts", "severity": "high", "evidence": f"Regnskapsregisteret {period}"})
    if isinstance(ni, (int, float)) and ni < 0:
        sig.append({"signal": "reported a net loss in latest filed accounts", "severity": "medium", "evidence": f"Regnskapsregisteret {period}"})
    form = ((rec or {}).get("organisasjonsform") or {}).get("kode")
    last = (rec or {}).get("sisteInnsendteAarsregnskap")
    reg_age = _years_since(fs.get("registered_date"))
    if form in ("AS", "ASA") and st == "active" and reg_age is not None and reg_age >= 2:
        try:
            if not last or int(last) < datetime.date.today().year - 2:
                sig.append({"signal": "annual accounts appear overdue", "severity": "medium",
                            "evidence": f"last submitted accounts year: {last or 'none'}"})
        except ValueError:
            pass
    for f in prof.get("facts", []):
        if f["category"] == "people" and f.get("as_of") and _years_since(f["as_of"]) == 0:
            sig.append({"signal": f"{f['label'].lower()} changed within the last 12 months", "severity": "info",
                        "evidence": f"role register updated {f['as_of']}"})
            break
    recent = [n for n in prof.get("news", []) if n["kind"] == "official_announcement" and _years_since(n["published"]) == 0]
    if recent:
        sig.append({"signal": f"{len(recent)} official announcement(s) in the last 12 months", "severity": "info",
                    "evidence": recent[0]["title"]})
    return sig


def flatten(facts):
    out = {}
    for f in facts:
        out.setdefault(f["category"], {})[f["field"]] = f["value"]
    return out


def _key(v):
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def diff_against(previous, facts, ok_sources, complete):
    """Detect added/changed/removed facts. If a source failed this run, its old facts are
    carried forward and flagged stale instead of being silently dropped."""
    if not previous:
        for f in facts:
            f["first_seen"] = f["retrieved_at"]
            f["last_verified"] = f["retrieved_at"]
        return [], facts
    old = {f["field"]: f for f in previous.get("facts", [])}
    new = {f["field"]: f for f in facts}
    changes = []
    for field, f in new.items():
        o = old.get(field)
        if o is None:
            f["first_seen"] = f["retrieved_at"]
            changes.append({"field": field, "type": "added", "new": f["value"], "source": f["source"]})
        elif _key(o["value"]) != _key(f["value"]):
            f["first_seen"] = f["retrieved_at"]
            f["previous_value"] = o["value"]
            changes.append({"field": field, "type": "changed", "old": o["value"], "new": f["value"],
                            "source": f["source"], "as_of": f.get("as_of")})
        else:
            f["first_seen"] = o.get("first_seen") or o.get("retrieved_at")
        f["last_verified"] = f["retrieved_at"]
    for field, o in old.items():
        if field in new:
            continue
        if o["source"] in ok_sources and complete:
            changes.append({"field": field, "type": "removed", "old": o["value"], "source": o["source"]})
        else:
            carried = dict(o, stale=True)
            carried["note"] = "Source unavailable in this run; value carried over from the last successful check"
            facts.append(carried)
    return changes, facts


def verification_checks(orgnr, fs, name):
    checks = [{"check": "organisation number checksum (mod 11)", "result": "pass"}]
    if fs.get("organisation_number") == orgnr:
        checks.append({"check": "registry record organisation number equals query", "result": "pass"})
    if fs.get("website_shows_orgnr") is True:
        checks.append({"check": "registered website displays the organisation number", "result": "pass"})
    elif fs.get("website_shows_orgnr") is False:
        checks.append({"check": "registered website displays the organisation number", "result": "not shown",
                       "note": "website facts kept at medium confidence"})
    if fs.get("lei"):
        checks.append({"check": "GLEIF registeredAs equals organisation number and name agrees", "result": "pass"})
    if fs.get("wikidata_id"):
        checks.append({"check": "Wikidata P2333 equals organisation number and name/LEI agrees", "result": "pass"})
    if fs.get("accounts_period"):
        checks.append({"check": "annual statement filed under this organisation number", "result": "pass"})
    return checks


FORM_EN = {
    "AS": "private limited company", "ASA": "public limited company", "ENK": "sole proprietorship",
    "ANS": "general partnership", "DA": "partnership with shared liability", "NUF": "Norwegian branch of a foreign company",
    "SA": "cooperative", "STI": "foundation", "FLI": "association", "KS": "limited partnership", "BA": "company with limited liability",
    "SAM": "jointly owned property", "BRL": "housing cooperative", "ESEK": "condominium", "KOMM": "municipality",
    "STAT": "state body", "IKS": "inter-municipal company", "KF": "municipal enterprise", "SF": "state enterprise",
    "FKF": "county municipal enterprise", "PK": "pension fund", "SPA": "savings bank", "GFS": "mutual insurance company",
    "ORGL": "organisational unit", "KIRK": "church body", "BBL": "housing building cooperative", "PRE": "shipping partnership",
}


def _cap(s):
    return s[:1].upper() + s[1:] if s else s


def _val(fs, field):
    v = fs.get(field)
    return v.get("amount") if isinstance(v, dict) and "amount" in v else v


def explain(prof, fs):
    orgnr = prof["organisation_number"]
    if prof["status"] == "not_found":
        return f"No entity or sub-unit with organisation number {fmt_orgnr(orgnr)} exists in Enhetsregisteret."
    if prof["status"] == "lookup_failed":
        return f"The Brønnøysund registers could not be reached for {fmt_orgnr(orgnr)}; no facts were published rather than guessing."
    name = prof.get("name") or "This entity"
    parts = []
    form = fs.get("legal_form")
    place = fs.get("municipality")
    s = f"{name} (org. no. {fmt_orgnr(orgnr)})"
    code = form.split(" – ")[0] if form else None
    kind_txt = FORM_EN.get(code) or (form.split(" – ", 1)[-1] if form else "entity")
    s += f" is {'an' if kind_txt[:1].lower() in 'aeiou' else 'a'} {kind_txt} ({code})" if code else " is an entity"
    s += f" based in {place.title()}" if place else ""
    reg = fs.get("registered_date") or fs.get("start_date")
    s += f", in the Brønnøysund registers since {reg}." if reg else "."
    parts.append(s)
    status_text = {
        "deleted": f"It has been deleted from the register{(' on ' + fs.get('deleted_date')) if fs.get('deleted_date') else ''}.",
        "bankrupt": f"It is registered as bankrupt{(' since ' + fs.get('bankruptcy_date')) if fs.get('bankruptcy_date') else ''}.",
        "under_liquidation": "It is under liquidation.",
        "compulsory_liquidation": "It is under compulsory liquidation or dissolution.",
        "active": "It is active with no bankruptcy or liquidation registered.",
    }.get(prof["status"])
    if status_text:
        parts.append(status_text)
    ind = fs.get("industry_code_1")
    emp = fs.get("employees")
    if ind or emp is not None:
        t = []
        if ind:
            t.append(f"its main activity is NACE {ind.split(' – ')[0]} ({ind.split(' – ', 1)[-1]})")
        if emp is not None:
            t.append(f"it has {emp:,} registered employees".replace(",", " "))
        parts.append(_cap("; ".join(t)) + ".")
    rev, ni, period = _val(fs, "revenue"), _val(fs, "net_income"), fs.get("accounts_period")
    if period and (rev is not None or ni is not None):
        year = str(period)[-10:-6]
        t = f"In its {year} accounts (company level)"
        bits = []
        if rev is not None:
            bits.append(f"operating revenue was {fmt_money(rev)}")
        if ni is not None:
            bits.append(f"net income was {fmt_money(ni)}")
        parts.append(t + " " + " and ".join(bits) + ".")
    grev = _val(fs, "group_revenue")
    if grev is not None:
        parts.append(f"Group (consolidated) revenue was {fmt_money(grev)}.")
    people = []
    ceo = fs.get("ceo")
    chair = fs.get("board_chair")
    if isinstance(ceo, dict):
        people.append(f"CEO: {ceo['name']}")
    if isinstance(chair, dict):
        people.append(f"board chair: {chair['name']}")
    if isinstance(fs.get("auditor"), dict):
        people.append(f"auditor: {fs.get('auditor')['name']}")
    if people:
        parts.append(_cap("; ".join(people)) + ".")
    extra = []
    if fs.get("lei"):
        extra.append(f"LEI {fs.get('lei')}")
    if fs.get("stock_listings"):
        l = fs.get("stock_listings")[0]
        extra.append(f"listed on {l['exchange']}" + (f" ({l['ticker']})" if l.get("ticker") else ""))
    if isinstance(fs.get("ultimate_parent"), dict):
        extra.append(f"ultimate parent {fs.get('ultimate_parent')['name']}")
    if extra:
        parts.append("Also: " + ", ".join(extra) + ".")
    news = prof.get("news") or []
    if news:
        n = news[0]
        parts.append(f"Most recent dated item ({n['published']}): \"{n['title']}\" – {n['source']}.")
        parts.append(f"{len(news)} dated news/announcement items are listed with source links.")
    risks = [r["signal"] for r in prof.get("risk_signals", []) if r["severity"] in ("high", "medium")]
    if risks:
        parts.append("Watch-outs: " + "; ".join(risks) + ".")
    return " ".join(parts)


def gaps(prof, kind, rec, statuses):
    out = []
    if prof["status"] in ("not_found", "invalid_organisation_number", "lookup_failed"):
        return out
    by = {}
    for s in statuses:
        if s:
            by.setdefault(s["source"], []).append(s)
    form = ((rec or {}).get("organisasjonsform") or {}).get("kode")
    fields = {f["field"] for f in prof["facts"]}
    if "revenue" not in fields and kind == "enhet":
        if form in NO_ACCOUNTS_DUTY and not (rec or {}).get("sisteInnsendteAarsregnskap"):
            out.append({"topic": "financials", "reason": f"Legal form {form} is normally not required to file annual accounts in Norway, so none are public."})
        elif any(s["status"] == "error" for s in by.get("brreg_regnskap", [])):
            out.append({"topic": "financials", "reason": "The Register of Company Accounts did not respond; financials were not published rather than estimated."})
        else:
            out.append({"topic": "financials", "reason": "No annual accounts are available in the Register of Company Accounts (newly registered, late filer, or exempt)."})
    if "website" not in fields and "official_website" not in fields:
        out.append({"topic": "website", "reason": "No website is registered in Enhetsregisteret or verified on Wikidata."})
    elif any(s["status"] == "error" for s in by.get("company_website", [])):
        out.append({"topic": "website", "reason": "The registered website could not be reached during this run."})
    if "lei" not in fields and kind == "enhet":
        out.append({"topic": "identifiers", "reason": "No LEI is registered with GLEIF for this organisation number (common for companies without traded securities or large financial contracts)."})
    if "ceo" not in fields and kind == "enhet" and form not in ("ENK",):
        out.append({"topic": "people", "reason": "No CEO (daglig leder) is registered; this is optional for many legal forms."})
    if not prof.get("news") and kind == "enhet":
        out.append({"topic": "news", "reason": "No dated announcement, company-site news or press article that provably refers to this exact company was found."})
    if "wikidata_id" not in fields and kind == "enhet":
        out.append({"topic": "reference", "reason": "No verified Wikidata item links to this number (normal for smaller companies)."})
    return out
