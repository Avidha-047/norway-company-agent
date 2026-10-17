"""Batch runner: concurrency, limits, output files, update handling, random sampling."""
import csv
import datetime
import json
import os
import random
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from .http import BudgetExceeded
from .profile import build_profile
from .report import write_report
from .sources import BRREG
from .util import ORGNR_RE, now_iso


def read_orgnrs(paths_or_values):
    seen, out = set(), []
    for item in paths_or_values:
        text = item
        if os.path.isfile(item):
            with open(item, encoding="utf-8-sig") as fh:
                text = fh.read()
        for m in ORGNR_RE.finditer(text):
            n = "".join(m.groups())
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def sample_orgnrs(http, n, seed=None, active_only=True):
    """Random sample from Enhetsregisteret: random registration-date windows, random picks inside each."""
    rnd = random.Random(seed)
    start, end = datetime.date(1995, 1, 1), datetime.date.today() - datetime.timedelta(days=30)
    span = (end - start).days
    found, seen, tries = [], set(), 0
    while len(found) < n and tries < n * 3 + 50:
        tries += 1
        d = start + datetime.timedelta(days=rnd.randrange(span))
        q = {"fraRegistreringsdatoEnhetsregisteret": d.isoformat(),
             "tilRegistreringsdatoEnhetsregisteret": (d + datetime.timedelta(days=2)).isoformat(),
             "size": 100}
        if active_only:
            q["konkurs"] = "false"
        r = http.get(f"{BRREG}/enheter?{urllib.parse.urlencode(q)}")
        units = ((r.json() or {}).get("_embedded") or {}).get("enheter") or [] if r.status == 200 else []
        rnd.shuffle(units)
        for u in units[:4]:
            num = u.get("organisasjonsnummer")
            if num and num not in seen:
                seen.add(num)
                found.append(num)
                if len(found) >= n:
                    break
    return found


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def run_batch(http, orgnrs, out_dir, workers=8, use_web=True, use_wikidata=True, use_news=True, use_bing=False, progress=True):
    os.makedirs(os.path.join(out_dir, "companies"), exist_ok=True)
    t0 = time.time()
    results = {}

    def one(n):
        path = os.path.join(out_dir, "companies", f"{n}.json")
        prev = _load(path)
        prof = build_profile(http, n, previous=prev, use_web=use_web, use_wikidata=use_wikidata, use_news=use_news, use_bing=use_bing)
        # never overwrite a good profile with a budget-truncated one
        if prev and not prof.get("complete", True) and prof.get("status") in ("lookup_incomplete", "lookup_failed"):
            prev["last_attempt"] = {"at": prof["generated_at"], "result": prof["status"]}
            prof = prev
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(prof, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return n, prof

    # keep ~5 core requests (entity, roles, sub-units, accounts, announcements) for every company not yet finished
    CORE = 5
    http.reserve = CORE * max(0, len(orgnrs) - workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, n): n for n in orgnrs}
        for i, f in enumerate(as_completed(futs), 1):
            http.reserve = CORE * max(0, len(orgnrs) - i - workers)
            n = futs[f]
            try:
                n, prof = f.result()
            except BudgetExceeded as e:
                prof = {"organisation_number": n, "status": "lookup_incomplete", "summary": str(e), "facts": []}
            except Exception as e:  # one bad company must never sink the run
                prof = {"organisation_number": n, "status": "error", "summary": f"{type(e).__name__}: {e}", "facts": []}
            results[n] = prof
            if progress:
                print(f"[{i}/{len(orgnrs)}] {n} {prof.get('status')} – {prof.get('name') or ''} "
                      f"({len(prof.get('facts', []))} facts) | requests used {http.used}", flush=True)

    ordered = [results[n] for n in orgnrs if n in results]
    write_outputs(out_dir, ordered)
    write_report(os.path.join(out_dir, "report.html"), ordered, now_iso())
    report = {
        "finished_at": now_iso(),
        "companies": len(ordered),
        "statuses": _count(p.get("status") for p in ordered),
        "facts_total": sum(len(p.get("facts", [])) for p in ordered),
        "facts_per_company_avg": round(sum(len(p.get("facts", [])) for p in ordered) / max(1, len(ordered)), 1),
        "companies_with_changes": sum(1 for p in ordered if p.get("changes")),
        "companies_with_dated_news": sum(1 for p in ordered if p.get("news")),
        "dated_news_items": sum(len(p.get("news") or []) for p in ordered),
        "outbound_requests": http.used,
        "request_budget": http.max_requests,
        "seconds": round(time.time() - t0, 1),
        "external_api_cost_usd": 0.0,
        "note": "All sources are free public APIs/pages; no paid API or LLM is called.",
    }
    with open(os.path.join(out_dir, "run_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return ordered, report


def _count(it):
    d = {}
    for x in it:
        d[x] = d.get(x, 0) + 1
    return d


def _amount(p, cat, field):
    v = (p.get("profile") or {}).get(cat, {}).get(field)
    return v.get("amount") if isinstance(v, dict) else v


def write_outputs(out_dir, profiles):
    with open(os.path.join(out_dir, "profiles.jsonl"), "w", encoding="utf-8") as fh:
        for p in profiles:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    cols = ["organisation_number", "name", "status", "legal_form", "municipality", "industry", "employees",
            "revenue_nok", "net_income_nok", "accounts_period", "ceo", "website", "lei", "news_count", "latest_news_date", "latest_news_title", "fact_count", "summary"]
    with open(os.path.join(out_dir, "index.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for p in profiles:
            pr = p.get("profile") or {}
            ceo = pr.get("people", {}).get("ceo")
            w.writerow([
                p.get("organisation_number"), p.get("name"), p.get("status"),
                pr.get("identity", {}).get("legal_form"), pr.get("location", {}).get("municipality"),
                pr.get("activity", {}).get("industry_code_1"), pr.get("size", {}).get("employees"),
                _amount(p, "financials", "revenue"), _amount(p, "financials", "net_income"),
                pr.get("financials", {}).get("accounts_period"),
                ceo.get("name") if isinstance(ceo, dict) else None,
                pr.get("contact", {}).get("website"), pr.get("identifiers", {}).get("lei"),
                len(p.get("news") or []), (p.get("news") or [{}])[0].get("published"),
                (p.get("news") or [{}])[0].get("title"),
                len(p.get("facts", [])), p.get("summary"),
            ])


def to_text(p):
    """Human-readable view of one profile."""
    lines = [f"{p.get('name') or '(unknown)'} — {p.get('organisation_number')} — status: {p.get('status')}", "",
             p.get("summary") or "", ""]
    cur = None
    for f in sorted((f for f in p.get("facts", []) if f["category"] != "news"), key=lambda f: f["category"]):
        if f["category"] != cur:
            cur = f["category"]
            lines.append(f"== {cur.upper()} ==")
        v = f["value"]
        if isinstance(v, dict) and "amount" in v:
            v = f"{v['amount']:,} {v['currency']} ({v['period']})".replace(",", " ")
        elif isinstance(v, dict) and "name" in v:
            v = v["name"]
        elif isinstance(v, list):
            v = "; ".join(x.get("name", json.dumps(x, ensure_ascii=False)) if isinstance(x, dict) else str(x) for x in v[:8])
        date = f" [as of {f['as_of']}]" if f.get("as_of") else ""
        stale = " (STALE)" if f.get("stale") else ""
        lines.append(f"  {f['label']}: {v}{date}{stale}")
        lines.append(f"      source: {f['evidence_url']}")
    if p.get("key_points"):
        lines.append("== KEY POINTS ==")
        lines += [f"  - {k}" for k in p["key_points"]]
    if p.get("risk_signals"):
        lines.append("== RISK SIGNALS ==")
        lines += [f"  - [{r['severity']}] {r['signal']} ({r['evidence']})" for r in p["risk_signals"]]
    if p.get("news"):
        lines.append("== DATED NEWS & ANNOUNCEMENTS ==")
        for it in p["news"][:15]:
            lines.append(f"  {it['published']}  {it['title']}  [{it['source']}]")
            lines.append(f"      {it['url']}")
            lines.append(f"      match: {it['company_match']}")
    if p.get("gaps"):
        lines.append("== NOT FOUND (and why) ==")
        for g in p["gaps"]:
            lines.append(f"  {g['topic']}: {g['reason']}")
    if p.get("changes"):
        lines.append("== CHANGES SINCE LAST RUN ==")
        for c in p["changes"]:
            lines.append(f"  {c['type']}: {c['field']}")
    return "\n".join(lines)


COMPARE_ROWS = [("Status", None, "status"), ("Legal form", "identity", "legal_form"), ("Municipality", "location", "municipality"),
                ("Industry", "activity", "industry_code_1"), ("Employees", "size", "employees"),
                ("Accounting period", "financials", "accounts_period"), ("Revenue", "financials", "revenue"),
                ("Net income", "financials", "net_income"), ("Equity", "financials", "equity"), ("CEO", "people", "ceo"),
                ("Board chair", "people", "board_chair"), ("Website", "contact", "website")]


def _cell(p, cat, field):
    if cat is None:
        return str(p.get(field) or "—")
    v = (p.get("profile") or {}).get(cat, {}).get(field)
    if v is None and field == "website":
        v = (p.get("profile") or {}).get("contact", {}).get("website_discovered")
    if isinstance(v, dict) and "amount" in v:
        return f"{v['amount']:,} {v['currency']}".replace(",", " ")
    if isinstance(v, dict):
        return v.get("name") or str(v)
    return "—" if v is None else str(v)


def compare_table(profiles):
    """Plain-text side-by-side comparison (values are taken only from sourced facts)."""
    cols = [[p.get("name") or p.get("organisation_number") or "?"] + [_cell(p, c, f) for _, c, f in COMPARE_ROWS]
            + [str(len(p.get("news") or []))] for p in profiles]
    labels = ["Company"] + [r[0] for r in COMPARE_ROWS] + ["Dated news items"]
    width = [max(len(x) for x in labels)] + [min(34, max(len(x) for x in col)) for col in cols]
    lines = []
    for i, lab in enumerate(labels):
        row = [lab.ljust(width[0])] + [col[i][:width[j + 1]].ljust(width[j + 1]) for j, col in enumerate(cols)]
        lines.append(" | ".join(row))
        if i == 0:
            lines.append("-+-".join("-" * w for w in width))
    return "\n".join(lines)
