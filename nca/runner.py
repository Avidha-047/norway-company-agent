"""Batch runner: concurrency, limits, output files, update handling, random sampling."""
import csv
import html as htmllib
import datetime
import json
import os
import random
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from .http import BudgetExceeded
from .profile import build_profile
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


def _load_bundles(out_dir):
    """Previous profiles from compact output (profiles.jsonl or profiles-NN.jsonl)."""
    prev = {}
    for name in sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []:
        if re.fullmatch(r"profiles(-\d+)?\.jsonl", name):
            with open(os.path.join(out_dir, name), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        p = json.loads(line)
                        if p.get("organisation_number"):
                            prev[p["organisation_number"]] = p
                    except ValueError:
                        pass
    return prev


FAILED = ("lookup_failed", "lookup_incomplete", "error")


def needs_retry(p):
    """Stored profiles worth checking again: failed lookups, or a register source that was down in that run."""
    if p.get("status") in FAILED or not p.get("facts"):
        return True
    for s in p.get("sources") or []:
        if s.get("source") in ("brreg_kunngjoringer", "brreg_regnskap", "brreg_roller") and s.get("status") == "error":
            return True
        if s.get("source") == "brreg_kunngjoringer" and "did not show the organisation number" in (s.get("note") or ""):
            return True
    return False


def run_batch(http, orgnrs, out_dir, workers=8, use_web=True, use_wikidata=True, use_news=True, use_bing=False,
              progress=True, compact=False, keep_others=False):
    os.makedirs(out_dir, exist_ok=True)
    if not compact:
        os.makedirs(os.path.join(out_dir, "companies"), exist_ok=True)
    bundled = _load_bundles(out_dir)
    n_prev = len(set(bundled) | ({f[:-5] for f in os.listdir(os.path.join(out_dir, "companies")) if f.endswith(".json")}
                                 if os.path.isdir(os.path.join(out_dir, "companies")) else set()))
    if progress:
        print(f"Found {n_prev} profiles from a previous run in '{out_dir}'"
              + ("; changes since then will be recorded." if n_prev else
                 " - this is a first run, so there is nothing to compare against yet."), flush=True)
    t0 = time.time()
    results = {}

    def one(n):
        path = os.path.join(out_dir, "companies", f"{n}.json")
        prev = _load(path) or bundled.get(n)
        prof = build_profile(http, n, previous=prev, use_web=use_web, use_wikidata=use_wikidata, use_news=use_news, use_bing=use_bing)
        # never overwrite a good profile with a budget-truncated one
        if prev and (not prof.get("complete", True) or prof.get("status") in ("lookup_incomplete", "lookup_failed")) \
                and prev.get("complete", True) and prev.get("status") not in ("lookup_incomplete", "lookup_failed"):
            prev["last_attempt"] = {"at": prof["generated_at"], "result": prof["status"]}
            prof = prev
        if not compact:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(prof, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        return n, prof

    # Core lookups (entity, roles, sub-units, accounts, announcements, +1 spare) are guaranteed for every company
    # not yet finished - including the ones in progress; optional sources only use what is left over.
    CORE = 6
    http.reserve = CORE * len(orgnrs)
    http.free_after_company = True            # keep memory flat: bodies are dropped once a profile is written
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, n): n for n in orgnrs}
        for i, f in enumerate(as_completed(futs), 1):
            http.reserve = CORE * max(0, len(orgnrs) - i)
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
    if keep_others:          # a partial refresh: keep every stored profile that was not re-checked in this run
        ordered += [p for n, p in bundled.items() if n not in results]
    from .profile import _clean_history
    for p in ordered:
        if p.get("history"):
            p["history"] = _clean_history(p["history"])
    write_outputs(out_dir, ordered, compact=compact)
    write_reports(out_dir, ordered, now_iso(), compact=compact)
    report = {
        "finished_at": now_iso(),
        "companies": len(ordered),
        "statuses": _count(p.get("status") for p in ordered),
        "facts_total": sum(len(p.get("facts", [])) for p in ordered),
        "facts_per_company_avg": round(sum(len(p.get("facts", [])) for p in ordered) / max(1, len(ordered)), 1),
        "companies_with_changes": sum(1 for p in ordered if p.get("changes")),
        "companies_with_dated_news": sum(1 for p in ordered if p.get("news")),
        "facts_with_content_hash": sum(1 for p in ordered for f in p.get("facts", []) if (f.get("reference") or {}).get("content_sha256")),
        "facts_with_exact_or_derived_quote": sum(1 for p in ordered for f in p.get("facts", []) if (f.get("reference") or {}).get("quote")),
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


PART_BYTES = 18_000_000      # keeps every file under GitHub's 25 MB web-upload limit


def _chunks(items, size_of, limit=None):
    limit = limit or PART_BYTES
    part, size = [], 0
    for it in items:
        n = size_of(it)
        if part and size + n > limit:
            yield part
            part, size = [], 0
        part.append(it)
        size += n
    if part:
        yield part


def write_reports(out_dir, profiles, generated, compact=False):
    from .report import write_report
    for old in os.listdir(out_dir):
        if re.fullmatch(r"report-\d+\.html", old):
            os.remove(os.path.join(out_dir, old))
    if not compact:
        write_report(os.path.join(out_dir, "report.html"), profiles, generated)
        return
    # Split by the real size of each written page: every file must stay under PART_BYTES (GitHub web upload limit)
    tmp = os.path.join(out_dir, ".report-size-check.html")
    def fits(part):
        write_report(tmp, part, generated)
        return os.path.getsize(tmp) <= PART_BYTES
    parts, todo = [], [profiles]
    while todo:
        part = todo.pop(0)
        if len(part) > 1 and not fits(part):
            mid = len(part) // 2
            todo[:0] = [part[:mid], part[mid:]]
        else:
            parts.append(part)
    if os.path.exists(tmp):
        os.remove(tmp)
    if len(parts) == 1:
        write_report(os.path.join(out_dir, "report.html"), profiles, generated)
        return
    links, idx = [], []
    for i, part in enumerate(parts, 1):
        name = f"report-{i:02d}.html"
        write_report(os.path.join(out_dir, name), part, generated)
        links.append(f'<li><a href="{name}">{name}</a> – {len(part)} companies '
                     f'({htmllib.escape(str(part[0].get("name") or part[0].get("organisation_number")))} … '
                     f'{htmllib.escape(str(part[-1].get("name") or part[-1].get("organisation_number")))})</li>')
        for p in part:
            idx.append([p.get("organisation_number"), p.get("name") or "",
                        ((p.get("profile") or {}).get("location") or {}).get("municipality") or "", p.get("status") or "", i])
    data = json.dumps(idx, ensure_ascii=False).replace("</", "<\\/")
    with open(os.path.join(out_dir, "report.html"), "w", encoding="utf-8") as fh:
        fh.write('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                 '<title>Norway Company Profiles</title><style>'
                 ':root{--bg:#fff;--fg:#1a1c1e;--mut:#5f6368;--line:#dadce0;--acc:#1a56db}'
                 '@media (prefers-color-scheme:dark){:root{--bg:#121417;--fg:#e8eaed;--mut:#9aa0a6;--line:#3c4043;--acc:#8ab8ff}}'
                 'body{font:16px/1.5 system-ui,sans-serif;max-width:860px;margin:auto;padding:16px;background:var(--bg);color:var(--fg)}'
                 'a{color:var(--acc)}input{width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;border:1px solid var(--line);'
                 'border-radius:8px;background:var(--bg);color:var(--fg)}#hits a{display:block;padding:10px 4px;border-bottom:1px solid var(--line);'
                 'text-decoration:none}#hits small{color:var(--mut)}.mut{color:var(--mut)}</style></head><body>'
                 f'<h1>Norway Company Profiles</h1><p class="mut">{len(profiles)} companies · generated {htmllib.escape(generated)}</p>'
                 '<label for="q"><b>Find a company</b> (name, organisation number or municipality)</label>'
                 '<input id="q" type="search" autocomplete="off" placeholder="e.g. Equinor, 923609016 or Bergen" autofocus>'
                 '<div id="hits"></div>'
                 f'<h2>All pages</h2><p class="mut">The profiles are split into {len(parts)} pages so each file stays small. '
                 'Each page has its own search, comparison and source panels.</p>'
                 f'<ul>{"".join(links)}</ul>'
                 f'<script id="idx" type="application/json">{data}</script><script>'
                 'const I=JSON.parse(document.getElementById("idx").textContent),q=document.getElementById("q"),h=document.getElementById("hits");'
                 'const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;"}[c]));'
                 'function run(){const t=q.value.trim().toLowerCase().replace(/\\s+/g," "),d=t.replace(/\\D/g,"");'
                 'if(t.length<2){h.innerHTML="";return}'
                 'const r=I.filter(x=>(d.length>=3&&x[0].includes(d))||x[1].toLowerCase().includes(t)||x[2].toLowerCase().includes(t)).slice(0,50);'
                 'h.innerHTML=r.length?r.map(x=>`<a href="report-${String(x[4]).padStart(2,"0")}.html#${x[0]}">${esc(x[1])} <small>· ${x[0]} · ${esc(x[2])} · ${esc(x[3])}</small></a>`).join("")'
                 ':"<p class=mut>No company matches.</p>"}'
                 'q.addEventListener("input",run);</script></body></html>')


def write_outputs(out_dir, profiles, compact=False):
    for old in os.listdir(out_dir):
        if re.fullmatch(r"profiles(-\d+)?\.jsonl", old):
            os.remove(os.path.join(out_dir, old))
    lines = [json.dumps(p, ensure_ascii=False) + "\n" for p in profiles]
    parts = list(_chunks(lines, lambda l: len(l.encode("utf-8")))) if compact else [lines]
    for i, part in enumerate(parts, 1):
        name = "profiles.jsonl" if len(parts) == 1 else f"profiles-{i:02d}.jsonl"
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.writelines(part)
    cols = ["organisation_number", "name", "status", "legal_form", "municipality", "industry", "employees",
            "revenue", "net_income", "currency", "accounts_period", "ceo", "website", "lei", "news_count", "latest_news_date", "latest_news_title", "fact_count", "verdict", "conclusion", "summary"]
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
                ((p.get("profile") or {}).get("financials", {}).get("revenue") or {}).get("currency")
                if isinstance((p.get("profile") or {}).get("financials", {}).get("revenue"), dict) else None,
                pr.get("financials", {}).get("accounts_period"),
                ceo.get("name") if isinstance(ceo, dict) else None,
                pr.get("contact", {}).get("website"), pr.get("identifiers", {}).get("lei"),
                len(p.get("news") or []), (p.get("news") or [{}])[0].get("published"),
                (p.get("news") or [{}])[0].get("title"),
                len(p.get("facts", [])), (p.get("conclusion") or {}).get("verdict"),
                (p.get("conclusion") or {}).get("text"), p.get("summary"),
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
            v = f"{v['amount']:,} {v.get('currency') or '(currency not stated)'} ({v['period']})".replace(",", " ")
        elif isinstance(v, dict) and "name" in v:
            v = v["name"]
        elif isinstance(v, list):
            v = "; ".join(x.get("name", json.dumps(x, ensure_ascii=False)) if isinstance(x, dict) else str(x) for x in v[:8])
        date = f" [as of {f['as_of']}]" if f.get("as_of") else ""
        stale = " (STALE)" if f.get("stale") else ""
        lines.append(f"  {f['label']}: {v}{date}{stale}")
        lines.append(f"      source: {f['evidence_url']}")
    if p.get("conclusion"):
        lines.append(f"== CONCLUSION: {p['conclusion']['verdict']} ==")
        lines.append("  " + p["conclusion"]["text"])
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
        return f"{v['amount']:,} {v.get('currency') or '(currency not stated)'}".replace(",", " ")
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
