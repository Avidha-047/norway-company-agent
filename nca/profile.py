"""Builds one company profile: fetch -> verify -> explain -> diff against the previous run."""
import json
import time

from . import sources as S
from .facts import SOURCES, FactSet
from .http import BudgetExceeded
from .util import fmt_money, fmt_orgnr, normalize_orgnr, now_iso, valid_orgnr

SCHEMA_VERSION = "1.1"
# Legal forms that are normally not obliged to file annual accounts in Norway.
NO_ACCOUNTS_DUTY = {"ENK", "ANS", "DA", "FLI", "SAM", "KIRK", "ORGL", "STAT", "FYLK", "KOMM", "IKS", "ESEK", "PERS", "UTLA", "VPFO"}


def build_profile(http, query, previous=None, use_web=True, use_wikidata=True):
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
            if use_web and site and prof["status"] != "deleted":
                wst = S.website_facts(http, fs, orgnr, site)
                if wst:
                    statuses.append(wst)
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
    if "wikidata_id" not in fields and kind == "enhet":
        out.append({"topic": "reference", "reason": "No verified Wikidata item links to this number (normal for smaller companies)."})
    return out
