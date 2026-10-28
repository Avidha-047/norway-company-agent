"""Builds one company profile: fetch -> verify -> explain -> diff against the previous run."""
import json
import re
import time

import datetime

from . import audit as A
from . import evidence as E
from . import news as N
from . import sources as S
from .facts import SOURCES, FactSet
from .http import BudgetExceeded
from .util import fmt_exact, fmt_orgnr, normalize_orgnr, now_iso, valid_orgnr

SCHEMA_VERSION = "2.0"
# Legal forms that are normally not obliged to file annual accounts in Norway.
NO_ACCOUNTS_DUTY = {"ENK", "ANS", "DA", "FLI", "SAM", "KIRK", "ORGL", "STAT", "FYLK", "KOMM", "IKS", "ESEK", "PERS", "UTLA", "VPFO"}


def build_profile(http, query, previous=None, use_web=True, use_wikidata=True, use_news=True, use_bing=False):
    started = time.time()
    used_before = http.used
    if hasattr(http, "begin_company"):
        http.begin_company()
    try:
        return _build_profile(http, query, previous, use_web, use_wikidata, use_news, use_bing, started, used_before)
    finally:
        if hasattr(http, "end_company") and getattr(http, "free_after_company", False):
            http.end_company()


def _build_profile(http, query, previous, use_web, use_wikidata, use_news, use_bing, started, used_before):
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
            try:
                S.entity_facts(fs, orgnr, kind, rec, api_url)
            except ValueError as e:                 # registry answered with another organisation number
                prof.update(status="lookup_failed", name=None, facts=[], sources=statuses, gaps=[], changes=[],
                            summary=f"The registry response for {fmt_orgnr(orgnr)} did not match the requested number ({e}); "
                                    "nothing was published.", verification={"rejected": [], "withheld": []})
                return prof
            prof["status"] = S.status_of(kind, rec)
            prof["entity_type"] = "sub-unit (establishment)" if "underenhet" in kind else "legal entity"
            prof["name"] = rec.get("navn")
            form = (rec.get("organisasjonsform") or {}).get("kode")
            if kind == "enhet":
                statuses.append(S.roles_facts(http, fs, orgnr))
                statuses.append(S.subunits_facts(http, fs, orgnr))
                if rec.get("sisteInnsendteAarsregnskap") or form not in NO_ACCOUNTS_DUTY:
                    statuses.append(S.accounts_facts(http, fs, orgnr))
            if prof["status"] != "deleted" and http.spare(1):
                ast = S.address_register_facts(http, fs, orgnr, rec)
                if ast:
                    statuses.append(ast)
            if rec.get("registrertIFrivillighetsregisteret") is True and kind == "enhet" and http.spare(1):
                statuses.append(S.voluntary_register_facts(http, fs, orgnr))
            emp = rec.get("antallAnsatte") if isinstance(rec.get("antallAnsatte"), int) else 0
            big = form in ("ASA", "SPA", "SF", "STAT", "KOMM") or emp >= 50 or bool(rec.get("hjemmeside")) and emp >= 20
            lei = None
            if kind in ("enhet", "deleted") and http.spare(1):
                gst, lei = S.gleif_facts(http, fs, orgnr, prof["name"], rejections, thorough=big and http.spare(3))
                statuses.append(gst)
            # Wikidata rarely knows small firms: query it only where an item is plausible
            if use_wikidata and kind in ("enhet", "deleted") and (big or lei) and http.spare(2):
                wst, _ = S.wikidata_facts(http, fs, orgnr, prof["name"], lei, rejections)
                statuses.append(wst)
            from . import web as W
            site = rec.get("hjemmeside")
            if site and W.is_social(site if "://" in site else "https://" + site):
                fs.add("registered_social_profile", "Social media page registered as the website", site,
                       "brreg_enhetsregisteret", api_url, evidence_url=S.brreg_page(orgnr), category="contact",
                       match="Record retrieved from Enhetsregisteret by exact organisation number", key="hjemmeside")
                site = None
            wst = None
            if use_web and site and prof["status"] != "deleted" and http.spare(1):
                wst = S.website_facts(http, fs, orgnr, site, rec=rec, rejections=rejections)
                if wst:
                    statuses.append(wst)
            wd_site = fs.get("official_website")
            if use_web and not site and wd_site and prof["status"] != "deleted" and http.spare(2):
                # a site listed on Wikidata is NOT registered in Enhetsregisteret: it must prove ownership
                wst = S.website_facts(http, fs, orgnr, wd_site, rec=rec, discovered=True, rejections=rejections,
                                      origin="listed on the company's Wikidata item")
                if wst:
                    statuses.append(wst)
                    site = wd_site if wst.get("status") == "ok" else None
                    if not site:
                        wst = None
            if use_web and not site and not wd_site and kind == "enhet" and prof["status"] in ("active", "registered"):
                dst, dstats = S.discover_website(http, fs, orgnr, rec, rejections, use_search=use_bing)
                statuses.extend(dstats)
                if dst:
                    wst, site = dst, dst.get("url")
            if use_news and kind in ("enhet", "deleted"):
                collect_news(http, fs, statuses, orgnr, rec, site, wst, use_bing)
    except BudgetExceeded as e:
        complete = False
        statuses.append(S.SourceStatus.make("agent", None, "stopped", note=str(e)))
        prof.setdefault("status", "lookup_incomplete")
        prof.setdefault("name", rec.get("navn") if rec else None)

    stale_accounts_note(fs)
    facts = fs.items
    ok_sources = {s["source"] for s in statuses if s and s.get("status") in ("ok", "not_found")}
    seen = getattr(http, "seen", {})
    lookup = http.company_lookup if hasattr(http, "company_lookup") else (lambda u: seen.get(u))
    E.attach(facts, lookup)
    withheld = A.verify_facts(facts, lookup)     # publish only what the receipt proves
    changes, facts = diff_against(previous, facts, ok_sources, complete)
    prof["facts"] = facts
    prof["profile"] = flatten(facts)
    prof["news"] = N.dedupe([it for f in facts if f["field"].startswith("news_") and isinstance(f["value"], list)
                             for it in f["value"]])
    prof["key_points"] = key_points(prof, fs)
    prof["risk_signals"] = risk_signals(prof, fs, rec)
    prof["conclusion"] = conclude(prof, fs)
    prof["summary"] = explain(prof, fs)
    prof["verification"] = {
        "method": "Every fact comes from a record fetched by the exact organisation number. Secondary sources "
                  "(GLEIF, Wikidata) are only accepted when their record names this number AND the legal name or LEI agrees; "
                  "financial values are copied as filed and never estimated.",
        "checks": verification_checks(orgnr, fs, prof.get("name")),
        "rejected": rejections,
        "withheld": withheld,
    }
    prof["gaps"] = gaps(prof, kind, rec, statuses)
    prof["sources"] = [dict(s, source_name=SOURCES.get(s.get("source"), {}).get("name"),
                            licence=SOURCES.get(s.get("source"), {}).get("licence")) for s in statuses if s]
    prof["changes"] = changes
    prof["history"] = _clean_history((previous or {}).get("history") or []) + ([{"at": prof["generated_at"], "changes": changes}] if changes and previous else [])
    prof["history"] = prof["history"][-30:]
    prof["verification"]["self_audit"] = A.audit_profile(prof)
    prof["complete"] = complete
    prof["stats"] = {"requests": http.used - used_before, "seconds": round(time.time() - started, 2), "fact_count": len(facts)}
    return prof


NEWS_FACTS = {
    "brreg_kunngjoringer": ("news_official", "Official announcements (Brønnøysund kunngjøringer)"),
    "company_website_feed": ("news_company", "News from the company's own website"),
    "press": ("news_press", "Press coverage naming this exact company"),
    "press_gdelt": ("news_press_gdelt", "Press coverage naming this exact company (GDELT news index)"),
}


def collect_news(http, fs, statuses, orgnr, rec, site, wst, use_bing):
    form = ((rec or {}).get("organisasjonsform") or {}).get("kode")
    batches = []
    items, st = N.official_announcements(http, orgnr)
    batches.append(("brreg_kunngjoringer", items, st))
    if site and wst and wst.get("feed_url") and http.spare(1):
        items, st = N.company_feed(http, wst.get("url") or site, wst["feed_url"], orgnr, ownership=wst.get("ownership"))
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
    if use_bing and http.spare(1):
        items, st = N.press_coverage(http, (rec or {}).get("navn"), form, corr, orgnr)
        batches.append(("press", items, st))
    if use_bing and http.spare(1) and N.press_eligible((rec or {}).get("navn"), form):
        items, st = N.gdelt_coverage(http, (rec or {}).get("navn"), form, corr, orgnr)
        batches.append(("press_gdelt", items, st))
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
    """Headcount only, stated as such (no size labels that would need other criteria)."""
    if not isinstance(emp, int):
        return None
    return f"{emp:,} registered employees".replace(",", " ")


def _result_words(ni, cur):
    if ni > 0:
        return "a profit of " + fmt_exact(ni, cur)
    if ni < 0:
        return "a loss of " + fmt_exact(-ni, cur)
    return "a result of " + fmt_exact(0, cur)


def _equity_words(eq, cur):
    if eq > 0:
        return f"positive equity of {fmt_exact(eq, cur)}"
    if eq < 0:
        return f"negative equity of {fmt_exact(-eq, cur)} (equity below zero)"
    return f"equity of {fmt_exact(0, cur)}"


def _dated_news(prof):
    """News items with a real publication date (not an index/first-seen date), newest first."""
    return [n for n in (prof.get("news") or []) if not n.get("date_note")]


def _title_ref(n):
    t = f"\"{n['title']}\""
    return t + (f" (category: {n['title_en']})" if n.get("title_en") else "")


def _scope_note(fs, prefix=""):
    notes = []
    if fs.get(prefix + "accounts_note"):
        notes.append("prepared on a liquidation basis")
    form = (fs.get("legal_form") or "").split(" – ")[0]
    if form == "NUF":
        notes.append("filed under this NUF's organisation number")
    return ("; " + "; ".join(notes)) if notes else ""


def key_points(prof, fs):
    pts = []
    if prof.get("status") in (None, "not_found", "invalid_organisation_number", "lookup_failed"):
        return pts
    ind = fs.get("industry_code_1")
    if ind:
        sec = nace_sector(ind.split(" – ")[0])
        pts.append(f"Sector: {sec} (NACE {ind})" if sec else f"Industry: NACE {ind}")
    if fs.get("founded_date"):
        age = _years_since(fs.get("founded_date"))
        pts.append(f"Founded {fs.get('founded_date')}" + (f" (about {age} years ago)" if age is not None else ""))
    elif fs.get("registered_date"):
        pts.append(f"Registered in Enhetsregisteret since {fs.get('registered_date')} (founding date not stated)")
    sc = size_class(fs.get("employees"))
    if sc:
        pts.append(f"Employees: {sc}" + (f"; {fs.get('subunit_count')} registered establishments" if fs.get("subunit_count") else ""))
    elif fs.get("employee_count_registered") is False:
        pts.append("Employees: no employee count is registered (unknown, not zero)")
    rev, ni, eq = _val(fs, "revenue"), _val(fs, "net_income"), _val(fs, "equity")
    period = fs.get("accounts_period")
    if period and (rev is not None or ni is not None):
        yr = str(period)[-10:-6]
        cur = _cur(fs)
        bits = []
        if rev is not None:
            bits.append(f"revenue {fmt_exact(rev, cur)}")
        if ni is not None:
            bits.append(_result_words(ni, cur))
        if eq is not None:
            bits.append(_equity_words(eq, cur))
        pts.append(f"Financials {yr} (as filed, company level, period {period}{_scope_note(fs)}"
                   + (f"; a newer {fs.get('last_annual_accounts_year')} filing is registered but its figures were not available"
                      if fs.get("accounts_currency_note") else "")
                   + "): " + ", ".join(bits))
    ceo, chair = fs.get("ceo"), fs.get("board_chair")
    lead = []
    if isinstance(ceo, dict):
        lead.append(f"CEO {ceo['name']}")
    if isinstance(chair, dict):
        lead.append(f"chair {chair['name']}")
    if lead:
        pts.append("Leadership (registered): " + ", ".join(lead))
    dn = _dated_news(prof)
    if dn:
        n = dn[0]
        pts.append(f"Most recent dated item found: {_title_ref(n)} ({n['published']}, {n['source']})")
    if fs.get("stock_listings"):
        l = fs.get("stock_listings")[0]
        pts.append(f"Listed on {l['exchange']}" + (f" as {l['ticker']}" if l.get("ticker") else "") + " (per Wikidata)")
    return pts


REGISTER_NOTE_RISK = re.compile(r"(?i)revisor|tvangsoppl|oppløs|konkurs|avvikl|mangler|ikke bekreftet|slett|daglig leder")


def risk_signals(prof, fs, rec):
    sig = []
    st = prof.get("status")
    if st in ("bankrupt", "under_liquidation", "compulsory_liquidation", "deleted"):
        sig.append({"signal": st.replace("_", " "), "severity": "high", "evidence": "Enhetsregisteret status flags"})
    ni, eq = _val(fs, "net_income"), _val(fs, "equity")
    period = fs.get("accounts_period")
    if isinstance(eq, (int, float)) and eq < 0:
        sig.append({"signal": f"negative equity in the {str(period)[-10:-6]} accounts", "severity": "high", "evidence": f"Regnskapsregisteret, period {period}"})
    if isinstance(ni, (int, float)) and ni < 0:
        sig.append({"signal": f"net loss in the {str(period)[-10:-6]} accounts", "severity": "medium", "evidence": f"Regnskapsregisteret, period {period}"})
    if fs.get("accounts_note"):
        sig.append({"signal": "accounts prepared on a liquidation basis", "severity": "high", "evidence": f"Regnskapsregisteret, period {period}"})
    for note in fs.get("register_notes") or []:
        if isinstance(note, dict) and note.get("text"):
            sig.append({"signal": f"register note: {note['text']}", "severity": "medium" if REGISTER_NOTE_RISK.search(note["text"]) else "info",
                        "evidence": f"Enhetsregisteret påtegning{(' of ' + note['date']) if note.get('date') else ''}"})
    form = ((rec or {}).get("organisasjonsform") or {}).get("kode")
    last = (rec or {}).get("sisteInnsendteAarsregnskap")
    reg_age = _years_since(fs.get("registered_date"))
    if form in ("AS", "ASA") and st == "active" and reg_age is not None and reg_age >= 2 and not period:
        try:
            if not last or int(last) < datetime.date.today().year - 2:
                sig.append({"signal": (f"most recent annual accounts registered are for {last}" if last
                                       else "no annual accounts are registered in Enhetsregisteret"),
                            "severity": "medium",
                            "evidence": f"Enhetsregisteret field sisteInnsendteAarsregnskap = {last or 'not set'}"})
        except ValueError:
            pass
    for f in prof.get("facts", []):
        if f["category"] == "people" and f.get("as_of") and _years_since(f["as_of"]) == 0:
            sig.append({"signal": f"the role register entry for '{f['label'].lower()}' was updated within the last 12 months",
                        "severity": "info", "evidence": f"role group last changed {f['as_of']}"})
            break
    recent = [n for n in prof.get("news", []) if n["kind"] == "official_announcement" and _years_since(n["published"]) == 0]
    if recent:
        sig.append({"signal": f"{len(recent)} official announcement(s) in the last 12 months", "severity": "info",
                    "evidence": recent[0]["title"]})
    return sig


def stale_accounts_note(fs):
    """If Enhetsregisteret lists a newer filed year than the figures we could retrieve, say so explicitly
    instead of letting older figures look like the current ones."""
    period, last = fs.get("accounts_period"), fs.get("last_annual_accounts_year")
    if not period or not last:
        return
    try:
        have, newest = int(str(period)[-10:-6]), int(str(last))
    except ValueError:
        return
    if newest > have:
        src = next((f for f in fs.items if f["field"] == "last_annual_accounts_year"), None)
        fs.add("accounts_currency_note", "Accounts currency note",
               f"Enhetsregisteret lists annual accounts filed for {newest}, but the accounts register only returned "
               f"figures for {have}; the {have} figures are shown and are not the most recent filing",
               "brreg_enhetsregisteret", src["source_url"] if src else None, evidence_url=src["source_url"] if src else None,
               as_of=str(period)[-10:], category="financials", match=src["match"] if src else None,
               key="sisteInnsendteAarsregnskap")


def _ev(fs, *fields):
    """Fields that exist, with their evidence links: the support for one claim."""
    out = []
    for f in fs.items:
        if f["field"] in fields and not any(o["field"] == f["field"] for o in out):
            out.append({"field": f["field"], "evidence_url": f["evidence_url"], "as_of": f.get("as_of")})
    return out


def conclude(prof, fs):
    """A short verdict on the company. Every claim lists the facts that support it; no claim is made
    without at least one supporting fact, and the verdict is only given when its own claim is supported."""
    st = prof.get("status")
    if st in (None, "not_found", "invalid_organisation_number", "lookup_failed"):
        return None
    claims = []

    def claim(text, *fields):
        sup = _ev(fs, *fields)
        if sup:
            claims.append({"claim": text, "supported_by": sup})
            return True
        return False

    name = prof.get("name") or "The entity"
    form = fs.get("legal_form")
    code = form.split(" – ")[0] if form else None
    kind = FORM_EN.get(code) or (form.split(" – ", 1)[-1] if form else "entity")
    sub_unit = "sub-unit" in (prof.get("entity_type") or "")
    if sub_unit:
        kind = "establishment (sub-unit)"
    sector = fs.get("industry_sector_en")
    place = (fs.get("municipality") or "").title()
    status_word = {"active": "an active", "deleted": "a deleted", "bankrupt": "a bankrupt",
                   "under_liquidation": "a liquidating", "compulsory_liquidation": "a compulsorily dissolving"}.get(st, "a registered")
    t = f"{name} is {status_word} {kind}"
    t += f" in {sector.split(' – ', 1)[-1].lower()}" if sector else ""
    t += f", based in {place}" if place else ""
    if sub_unit and fs.get("parent_unit"):
        t += f"; it belongs to the main unit {fs.get('parent_unit')}, which holds its bankruptcy and liquidation status"
    claim(t + ".", "legal_form", "industry_sector_en", "industry_code_1", "municipality", "bankrupt",
          "under_liquidation", "compulsory_dissolution", "deleted_date", "parent_unit")

    emp = fs.get("employees")
    if isinstance(emp, int):
        sub = fs.get("subunit_count")
        claim(f"It has {size_class(emp)}" + (f" and {sub} registered establishments" if sub else "") + ".",
              "employees", "subunit_count")
    elif fs.get("employee_count_registered") is False:
        claim("Its employee count is not registered, so its headcount is unknown.", "employee_count_registered")

    period = fs.get("accounts_period")
    rev, ni, eq = _val(fs, "revenue"), _val(fs, "net_income"), _val(fs, "equity")
    if period and (rev is not None or ni is not None):
        yr = str(period)[-10:-6]
        cur = _cur(fs)
        bits = []
        if rev is not None:
            bits.append(f"revenue of {fmt_exact(rev, cur)}")
        if ni is not None:
            bits.append(_result_words(ni, cur))
        sent = f"In its {yr} accounts (company level, period {period}{_scope_note(fs)}) it reported " + " and ".join(bits)
        if eq is not None:
            sent += ", with " + _equity_words(eq, cur)
        claim(sent + ".", "accounts_period", "revenue", "net_income", "equity", "accounts_note")
        if fs.get("accounts_currency_note"):
            claim(f"These are not its most recent accounts: a {fs.get('last_annual_accounts_year')} filing is registered "
                  "but its figures were not available.", "accounts_currency_note", "last_annual_accounts_year")
    elif code in NO_ACCOUNTS_DUTY and not fs.get("last_annual_accounts_year"):
        art = "an" if str(kind)[:1].lower() in "aeiou" else "a"
        claim(f"As {art} {kind} ({code}) it is normally not required to publish annual accounts.", "legal_form")

    gov = [x for x in ("ceo", "board_chair", "auditor") if fs.get(x)]
    if gov:
        names = {"ceo": "CEO", "board_chair": "board chair", "auditor": "auditor"}
        parts = [names[x] for x in gov]
        sent = "It has a registered " + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0])
        if fs.get("accounts_audited") is True and period:
            sent += f", and its {str(period)[-10:-6]} accounts were audited"
        claim(sent + ".", *gov, "accounts_audited")

    dn = _dated_news(prof)
    if dn:
        n = dn[0]
        claims.append({"claim": f"The most recent dated item found is {_title_ref(n)}, published {n['published']} ({n['source']}).",
                       "supported_by": [{"field": "news", "evidence_url": n["url"], "as_of": n["published"]}]})

    if not prof.get("complete", True):
        return {"verdict": "Incomplete – not all sources were checked in this run", "text": " ".join(c["claim"] for c in claims),
                "claims": claims, "method": "Each claim is built only from the listed facts; nothing is inferred beyond them."}
    risks = [r for r in prof.get("risk_signals", []) if r["severity"] in ("high", "medium")]
    verdict = None
    if st == "deleted":
        verdict = "Deleted"
    elif st in ("bankrupt", "compulsory_liquidation", "under_liquidation"):
        verdict = "Not operating normally"
    if risks:
        claims.append({"claim": "Watch-outs: " + "; ".join(r["signal"] for r in risks) + ".",
                       "supported_by": [{"field": "risk_signals", "evidence": r["evidence"]} for r in risks]})
        verdict = verdict or ("Needs attention" if any(r["severity"] == "high" for r in risks) else "Some caution")
    elif st == "active":
        checks = ["bankruptcy", "liquidation", "compulsory dissolution"]
        if eq is not None:
            checks.append("negative equity")
        if ni is not None:
            checks.append("losses")
        if claim("No risk flags were found in the registers checked (" + ", ".join(checks) + ").", "bankrupt",
                 "under_liquidation", "compulsory_dissolution"):
            verdict = "No registry risk flags"
    if verdict is None:
        verdict = "Status held on the main unit" if sub_unit else "No verdict – status flags not stated in the register"
    return {"verdict": verdict, "text": " ".join(c["claim"] for c in claims), "claims": claims,
            "method": "Each claim is built only from the listed facts; nothing is inferred beyond them."}


def flatten(facts):
    out = {}
    for f in facts:
        out.setdefault(f["category"], {})[f["field"]] = f["value"]
    return out


# Official announcements are permanent public records: if a run sees none for a company that had some, the
# announcement page was not served properly (busy or throttled) - the earlier ones are kept, marked stale.
NEVER_REMOVED = {"news_official"}
VOLATILE = {"reference", "retrieved_at", "first_seen", "last_verified"}


def _clean_history(hist):
    """Drop recorded 'removals' of permanent records (official announcements): they only reflect a run in which
    the announcement page was not served, never a real change."""
    out = []
    for h in hist:
        ch = [c for c in h.get("changes") or [] if not (c.get("type") == "removed" and c.get("field") in NEVER_REMOVED)]
        if ch:
            out.append(dict(h, changes=ch))
    return out


def _stable(v):
    """The value without per-run receipt details (retrieval time, hash), so an unchanged fact is not a 'change'."""
    if isinstance(v, dict):
        return {k: _stable(x) for k, x in v.items() if k not in VOLATILE}
    if isinstance(v, list):
        return [_stable(x) for x in v]
    return v


def _key(v):
    return json.dumps(_stable(v), sort_keys=True, ensure_ascii=False)


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
        if o["source"] in ok_sources and complete and field not in NEVER_REMOVED:
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
    if fs.get("website_discovered"):
        checks.append({"check": "discovered website proven to belong to this company", "result": "pass"})
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


def _cur(fs, prefix=""):
    """Currency the statement was filed in (never assumed): None when the filing does not state it."""
    for f in ("revenue", "net_income", "equity", "total_assets"):
        v = fs.get(prefix + f)
        if isinstance(v, dict):
            return v.get("currency")
    return None


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
        "active": "The register states no bankruptcy, liquidation or compulsory dissolution.",
        "registered": ("As a sub-unit, its bankruptcy and liquidation status is held on its main unit"
                       + (f" ({fs.get('parent_unit')})" if fs.get("parent_unit") else "") + "."
                       if "sub-unit" in (prof.get("entity_type") or "")
                       else "The register record does not state its bankruptcy and liquidation flags."),
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
        newer = fs.get("accounts_currency_note")
        t = (f"In its {year} accounts (company level; a newer {fs.get('last_annual_accounts_year')} filing is registered "
             f"but its figures were not available)") if newer else f"In its {year} accounts (company level, period {period}{_scope_note(fs)})"
        bits = []
        if rev is not None:
            bits.append(f"operating revenue was {fmt_exact(rev, _cur(fs))}")
        if ni is not None:
            bits.append(f"net income was {fmt_exact(ni, _cur(fs))}")
        parts.append(t + " " + " and ".join(bits) + ".")
    grev = _val(fs, "group_revenue")
    if grev is not None:
        gper = fs.get("group_accounts_period")
        parts.append(f"Group (consolidated) revenue was {fmt_exact(grev, _cur(fs, 'group_'))}"
                     + (f" (group accounts, period {gper}{_scope_note(fs, 'group_')})" if gper else "") + ".")
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
    news = _dated_news(prof)
    if news:
        n = news[0]
        parts.append(f"Most recent dated item found ({n['published']}): {_title_ref(n)} – {n['source']}.")
        parts.append(f"{len(news)} dated news/announcement items are listed with source links.")
    risks = [r["signal"] for r in prof.get("risk_signals", []) if r["severity"] in ("high", "medium")]
    if risks:
        parts.append("Watch-outs: " + "; ".join(risks) + ".")
    return " ".join(parts)


def gaps(prof, kind, rec, statuses):
    """What was not found, and why - worded by what actually happened to the source (checked and empty,
    failed, skipped, or rejected), never claiming that something does not exist when it was not checked."""
    out = []
    if prof["status"] in ("not_found", "invalid_organisation_number", "lookup_failed"):
        return out
    by = {}
    for s_ in statuses:
        if s_:
            by.setdefault(s_["source"], []).append(s_)
    st = lambda src: {x["status"] for x in by.get(src, [])}
    form = ((rec or {}).get("organisasjonsform") or {}).get("kode")
    fields = {f["field"] for f in prof["facts"]}
    withheld = {w["field"] for w in (prof.get("verification") or {}).get("withheld") or []}
    for w in sorted(withheld & {"ceo", "board_chair", "auditor", "accounts_period", "revenue", "lei", "employees",
                                "legal_form", "website_title"}):
        out.append({"topic": w, "reason": "The source holds this, but its receipt (exact quote in the retrieved content) "
                                          "could not be verified, so it was not published."})
    fields |= withheld          # a withheld fact must never be described as missing below
    if not prof.get("complete", True):
        out.append({"topic": "run", "reason": "This run stopped early (request or time budget), so not every source was checked; "
                                              "missing items below may simply not have been looked up."})
    if kind == "enhet":
        acc = st("brreg_regnskap")
        if "accounts_period" in fields and "revenue" not in fields:
            out.append({"topic": "financials", "reason": "Annual accounts were retrieved, but the filed statement has no operating revenue line."})
        elif "accounts_period" not in fields:
            if form in NO_ACCOUNTS_DUTY and not (rec or {}).get("sisteInnsendteAarsregnskap"):
                out.append({"topic": "financials", "reason": f"Legal form {form} is normally not required to file annual accounts in Norway, so none were looked up."})
            elif "error" in acc:
                out.append({"topic": "financials", "reason": "The Register of Company Accounts did not answer; no figures were published rather than estimated."})
            elif not acc:
                out.append({"topic": "financials", "reason": "The Register of Company Accounts was not queried in this run."})
            else:
                out.append({"topic": "financials", "reason": "The Register of Company Accounts returned no statement for this organisation number."})
    web_st = st("company_website") | st("website_discovery")
    if not fields & {"website", "official_website", "website_discovered", "registered_social_profile"}:
        if not web_st:
            out.append({"topic": "website", "reason": "No website is registered in Enhetsregisteret; no other website was checked in this run."})
        else:
            notes = "; ".join(x.get("note") or "" for x in by.get("website_discovery", []) if x.get("note"))
            out.append({"topic": "website", "reason": "No website is registered in Enhetsregisteret, and no candidate could be proven to "
                                                      "belong to this company" + (f" ({notes})" if notes else "") + "."})
    elif "error" in st("company_website") and "website_title" not in fields:
        out.append({"topic": "website", "reason": "The registered website could not be reached during this run."})
    if "lei" not in fields and kind == "enhet":
        g = st("gleif")
        if "error" in g:
            out.append({"topic": "identifiers", "reason": "GLEIF could not be reached, so the LEI was not checked in this run."})
        elif not g:
            out.append({"topic": "identifiers", "reason": "GLEIF was not queried in this run (request budget)."})
        elif any(r.get("source") == "gleif" for r in (prof.get("verification") or {}).get("rejected", [])):
            out.append({"topic": "identifiers", "reason": "GLEIF returned a record that could not be matched to this company, so no LEI was published."})
        else:
            out.append({"topic": "identifiers", "reason": "GLEIF has no LEI record registered under this organisation number."})
    if "ceo" not in fields and kind == "enhet" and form not in ("ENK",):
        r = st("brreg_roller")
        if "error" in r or not r:
            out.append({"topic": "people", "reason": "The role register could not be retrieved in this run."})
        else:
            out.append({"topic": "people", "reason": "No CEO (daglig leder) is registered; this is optional for many legal forms."})
    if not prof.get("news") and kind == "enhet":
        out.append({"topic": "news", "reason": "No dated announcement or company-site news post that provably refers to this company was found in the sources checked."})
    if "wikidata_id" not in fields and kind == "enhet":
        w = st("wikidata")
        out.append({"topic": "reference", "reason": ("Wikidata was checked and has no verified item for this organisation number." if w and "error" not in w
                                                     else "Wikidata was not queried (only listed or large companies, or LEI holders, are looked up there).")})
    return out
