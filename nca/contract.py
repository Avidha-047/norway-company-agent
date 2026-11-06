"""Builderr Signalpost output contract (OUTPUT_CONTRACT.md in the official starter kit).

Every profile keeps its detailed `facts`; this module adds the contract view on top of them:

    run, claims[] (field, value, availability, confidence, evidence_ids), evidence[] (id, source_url,
    source_class, retrieved_at, content_sha256, claim_span), synthesis, changes, errors, operations

Nothing new is asserted here: every claim is a published, receipt-checked fact (or one dated item of a
fact list) and every synthesis statement cites the evidence ids of the facts it is built from.
"""
import hashlib
import json

CONTRACT_VERSION = "signalpost-output-contract-1"

CONFIDENCE = {"high": 0.95, "medium": 0.8, "low": 0.6}

REGISTRY_SOURCES = {"brreg_enhetsregisteret", "brreg_regnskap", "brreg_roller", "brreg_kunngjoringer",
                    "brreg_frivillighetsregisteret", "kartverket_adresser"}
COMPANY_OWNED = {"company_website", "website_discovery", "company_website_feed", "company_careers"}
THIRD_PARTY_REGISTRY = {"gleif", "wikidata"}

WEBSITE_FIELDS = {"website_final_url", "website_ownership", "website_shows_orgnr", "website_title",
                  "website_site_name", "website_description", "website_logo", "website_discovered"}
CONTACT_FIELDS = {"website_emails", "website_phones", "website_contact_page", "website_address"}


def source_class(source):
    if source in REGISTRY_SOURCES:
        return "official_registry"
    if source in COMPANY_OWNED:
        return "company_owned"
    if source in THIRD_PARTY_REGISTRY:
        return "third_party_registry"
    if source and source.startswith("press"):
        return "third_party_publication"
    return "other"


def family(f):
    """The information family a fact belongs to (external families are what recall is measured on)."""
    field, cat, src = f.get("field", ""), f.get("category", ""), f.get("source", "")
    if field == "news_official":
        return "official_announcements"
    if field == "news_company":
        return "company_news"
    if field.startswith("news_press"):
        return "press_coverage"
    if field.startswith("job") or field == "careers_page" or cat == "jobs":
        return "job_postings"
    if field == "social_profiles" or field == "registered_social_profile":
        return "company_profiles"
    if field in WEBSITE_FIELDS or (field == "website_about"):
        return "official_website"
    if field in CONTACT_FIELDS:
        return "website_contact"
    if field == "subunits" or field == "subunit_count":
        return "registered_workplaces"
    return {"financials": "annual_accounts", "people": "leadership", "location": "registered_address",
            "size": "size"}.get(cat, "legal_identity")


def _ev_id(ref):
    key = json.dumps([ref.get("url"), ref.get("content_sha256"), ref.get("quote")], ensure_ascii=False)
    return "ev-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _period(f):
    v = f.get("value")
    if isinstance(v, dict) and v.get("period"):
        return v["period"]
    return None


def _own_site(facts):
    own = next((f for f in facts if f["field"] == "website_ownership"
                and str(f.get("value", "")).startswith("the company's own site")), None)
    url = next((f for f in facts if f["field"] == "website_final_url"), None)
    return own, url


def build(prof, run=None, requests=None, runtime_ms=None):
    """Return the contract fields for one profile (to be merged into it)."""
    facts = prof.get("facts") or []
    evidence, claims, by_field = {}, [], {}

    def add_evidence(ref, f, item_url=None):
        if not ref or not (ref.get("url") or ref.get("content_sha256")):
            return None
        eid = _ev_id(ref)
        if eid not in evidence:
            evidence[eid] = {
                "id": eid,
                "source_url": ref.get("url") or f.get("source_url"),
                "source_id": f.get("source"),
                "source_name": f.get("source_name"),
                "source_class": source_class(f.get("source")),
                "evidence_page": item_url or f.get("evidence_url"),
                "retrieved_at": ref.get("retrieved_at") or f.get("retrieved_at"),
                "content_sha256": ref.get("content_sha256"),
                "content_bytes": ref.get("content_bytes"),
                "claim_span": ref.get("quote"),
                "span_status": ref.get("quote_status"),
                "extraction": ref.get("basis") or "verbatim span located in the retrieved content",
            }
        return eid

    own_site, _ = _own_site(facts)
    for f in facts:
        fam = family(f)
        unproven_site = not own_site and fam in ("official_website", "website_contact", "company_profiles") \
            and f.get("source") in COMPANY_OWNED
        if unproven_site:
            fam = "website_candidate"      # a registered site that does not prove it is this company's: not its website
        conf = CONFIDENCE.get(f.get("confidence"), 0.8)
        items = f.get("value") if isinstance(f.get("value"), list) else None
        if items and all(isinstance(it, dict) and it.get("reference") for it in items):
            # dated lists (announcements, posts): one claim per item, each with its own receipt
            for it in items:
                eid = add_evidence(it["reference"], f, it.get("url"))
                claims.append({
                    "field": f["field"], "family": fam, "label": f.get("label"),
                    "value": {k: it.get(k) for k in ("title", "title_en", "published", "url", "kind") if it.get(k)},
                    "availability": "available", "confidence": conf,
                    "evidence_ids": [eid] if eid else [],
                    "effective_date": it.get("published"), "source_class": source_class(f.get("source")),
                    "match_basis": it.get("company_match") or f.get("match"),
                })
            by_field.setdefault(f["field"], []).extend(c["evidence_ids"][0] for c in claims[-len(items):] if c["evidence_ids"])
            continue
        eid = add_evidence(f.get("reference") or {}, f)
        claims.append({
            "field": f["field"], "family": fam, "label": f.get("label"), "value": f.get("value"),
            "availability": "ambiguous" if unproven_site else "available", "confidence": conf, "evidence_ids": [eid] if eid else [],
            "effective_date": f.get("as_of"), "reporting_period": _period(f),
            "source_class": source_class(f.get("source")), "match_basis": f.get("match"),
            "first_seen": f.get("first_seen"), "last_verified": f.get("last_verified"),
        })
        if eid:
            by_field.setdefault(f["field"], []).append(eid)

    own, url = _own_site(facts)
    if own and url:
        ids = by_field.get("website_ownership", []) + by_field.get("website_final_url", [])
        claims.append({"field": "official_website", "family": "official_website", "label": "Official website (verified)",
                       "value": url["value"], "availability": "available",
                       "confidence": CONFIDENCE.get(own.get("confidence"), 0.8), "evidence_ids": ids,
                       "source_class": "company_owned", "match_basis": own.get("value")})

    # explicit availability for every required section that has nothing to publish
    for s in ((prof.get("envelope") or {}).get("sections") or []):
        if s.get("state") != "available":
            claims.append({"field": s["section"], "family": s["section"], "label": s.get("title"), "value": None,
                           "availability": s["state"], "confidence": None, "evidence_ids": [], "reason": s.get("reason")})

    errors = [{"source": s.get("source"), "url": s.get("url"), "status": s.get("status"),
               "http_status": s.get("http_status"), "error": s.get("error") or s.get("note")}
              for s in (prof.get("sources") or []) if s and s.get("status") not in ("ok", "not_found", None)]
    stats = prof.get("stats") or {}
    out = {
        "contract_version": CONTRACT_VERSION,
        "run": dict(run or {}, terminal_status="completed" if prof.get("complete", True)
                    and prof.get("status") not in ("lookup_failed", "lookup_incomplete", "error") else "incomplete"),
        "claims": claims,
        "evidence": list(evidence.values()),
        "synthesis": synthesis(prof, by_field),
        "errors": errors,
        "operations": {"requests": requests if requests is not None else stats.get("requests"),
                       "runtime_ms": runtime_ms if runtime_ms is not None else int((stats.get("seconds") or 0) * 1000),
                       "third_party_cost_usd": 0},
    }
    out["changes"] = prof.get("changes") or []
    return out


def _amt(prof, field):
    for f in prof.get("facts") or []:
        if f["field"] == field and isinstance(f.get("value"), dict) and f["value"].get("amount") is not None:
            return f["value"]
    return None


def _fmt(v):
    a = v["amount"]
    s = f"{abs(a):,.0f}".replace(",", " ")
    return f"{v.get('currency', 'NOK')} {'-' if a < 0 else ''}{s}"


def _val(prof, field):
    for f in prof.get("facts") or []:
        if f["field"] == field:
            v = f.get("value")
            return v.get("name") if isinstance(v, dict) and "name" in v else v
    return None


def synthesis(prof, ids):
    """What the company does, what changed and what is unknown - every statement cites its evidence ids."""
    def cite(*fields):
        out = []
        for fl in fields:
            for e in ids.get(fl, [])[:3]:
                if e not in out:
                    out.append(e)
        return out

    what, changed, unknown = [], [], []
    name = prof.get("name") or prof.get("organisation_number")
    form, ind, mun = _val(prof, "legal_form"), _val(prof, "industry_code_1"), _val(prof, "municipality")
    if form or ind:
        what.append({"text": f"{name} is registered as {form or 'an entity'}"
                             + (f" in {mun.title()}" if isinstance(mun, str) else "")
                             + (f", with main activity {ind}" if ind else "") + ".",
                     "evidence_ids": cite("legal_form", "industry_code_1", "municipality")})
    act = _val(prof, "website_about") or _val(prof, "activity_description") or _val(prof, "statutory_purpose")
    if isinstance(act, str) and act.strip():
        src = "website_about" if _val(prof, "website_about") else ("activity_description" if _val(prof, "activity_description") else "statutory_purpose")
        what.append({"text": ("Its own website describes it as: " if src == "website_about" else "Registered activity: ") + act.strip()[:300],
                     "evidence_ids": cite(src)})
    ceo, chair = _val(prof, "ceo"), _val(prof, "board_chair")
    if ceo or chair:
        what.append({"text": "Leadership on the register: " + "; ".join(x for x in (f"CEO {ceo}" if ceo else "", f"board chair {chair}" if chair else "") if x) + ".",
                     "evidence_ids": cite("ceo", "board_chair")})
    rev, ni, per = _amt(prof, "revenue"), _amt(prof, "net_income"), _val(prof, "accounts_period")
    if rev or ni:
        what.append({"text": f"Latest filed accounts ({per or 'latest period'}): "
                             + ", ".join(x for x in (f"operating revenue {_fmt(rev)}" if rev else "", f"net income {_fmt(ni)}" if ni else "") if x)
                             + " (as filed, not estimated).",
                     "evidence_ids": cite("revenue", "net_income", "accounts_period")})
    prev_rev, prev_ni, prev_per = _amt(prof, "prior_revenue"), _amt(prof, "prior_net_income"), _val(prof, "prior_accounts_period")
    if rev and prev_rev and prev_rev.get("currency") == rev.get("currency"):
        d = rev["amount"] - prev_rev["amount"]
        pct = f" ({d / abs(prev_rev['amount']) * 100:+.1f}%)" if prev_rev["amount"] else ""
        changed.append({"text": f"Operating revenue went from {_fmt(prev_rev)} ({prev_per}) to {_fmt(rev)} ({per}){pct}.",
                        "evidence_ids": cite("prior_revenue", "revenue")})
    elif ni and prev_ni and prev_ni.get("currency") == ni.get("currency"):
        changed.append({"text": f"Net income went from {_fmt(prev_ni)} ({prev_per}) to {_fmt(ni)} ({per}).",
                        "evidence_ids": cite("prior_net_income", "net_income")})
    news = sorted((n for n in prof.get("news") or [] if n.get("published")), key=lambda n: n["published"], reverse=True)
    if news:
        n = news[0]
        field = {"official_announcement": "news_official"}.get(n.get("kind"), "news_company")
        changed.append({"text": f"Most recent dated activity: {n['published']} – {n.get('title_en') or n.get('title')}"
                                + (f" ({n.get('title')})" if n.get("title_en") and n.get("title_en") != n.get("title") else "") + ".",
                        "evidence_ids": cite(field)[:1]})
    if prof.get("changes"):
        changed.append({"text": f"{len(prof['changes'])} material change(s) since the previous run: "
                                + "; ".join(str(c.get("label") or c.get("field")) for c in prof["changes"][:5]) + ".",
                        "evidence_ids": [], "basis": "diff against the previous stored snapshot (see changes)"})
    else:
        ref = next((s.get("reason", "") for s in ((prof.get("envelope") or {}).get("sections") or [])
                    if s.get("section") == "refresh"), "")
        if ref.startswith("compared with the run of"):
            when = ref[len("compared with the run of "):].split(": ")[0]
            changed.append({"text": f"No material change since the previous run ({when}).", "evidence_ids": [],
                            "basis": "diff against the previous stored snapshot"})
        else:
            changed.append({"text": "First observation in this store: there is no earlier snapshot to compare against yet.",
                            "evidence_ids": []})
    jobs = next((f["value"] for f in prof.get("facts") or [] if f["field"] == "job_postings"), None) or []
    if jobs:
        titles = ", ".join(j["title"] for j in jobs[:4]) + (" …" if len(jobs) > 4 else "")
        changed.append({"text": f"Hiring: {len(jobs)} job posting(s) listed on its own careers page when checked ({titles}).",
                        "evidence_ids": ids.get("job_postings", [])[:4]})
    else:
        hs = next((s for s in ((prof.get("envelope") or {}).get("sections") or []) if s["section"] == "hiring_activity"), {})
        if hs.get("state") == "available":
            reason = hs.get("reason", "").split("; ")[0]
            unknown.append({"text": f"Hiring: no job posting found – {reason}.", "evidence_ids": []})
    for s in ((prof.get("envelope") or {}).get("sections") or []):
        if s.get("state") in ("not_available", "ambiguous", "blocked", "failed"):
            unknown.append({"text": f"{s.get('title')}: {s['state'].replace('_', ' ')} – {s.get('reason')}",
                            "evidence_ids": []})
    return {"what_it_does": what, "what_changed": changed, "what_is_unknown": unknown,
            "text": " ".join(x["text"] for x in what + changed)
                    + (" Unknown: " + " ".join(x["text"] for x in unknown) if unknown else "")}
