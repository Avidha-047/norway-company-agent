#!/usr/bin/env python3
"""Norway Company Facts Agent.

  python agent.py 923609016                         # one company, JSON to stdout
  python agent.py 923609016 --format text           # one company, readable
  python agent.py --input companies.txt --out output # the daily test (any text/CSV/JSON file of org numbers)
  python agent.py --sample 1000 --out output        # build 1,000 random profiles
  python agent.py --input companies.txt --out output   # run again later = update + change log
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nca.http import Http  # noqa: E402
from nca.profile import build_profile  # noqa: E402
from nca.runner import read_orgnrs, run_batch, sample_orgnrs, to_text  # noqa: E402


def _safe_console():
    """Never crash on names with letters the console cannot show (e.g. Sámi on a Windows cp1252 console
    or when output is redirected to a file): write UTF-8, replacing anything unencodable."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None):
    _safe_console()
    ap = argparse.ArgumentParser(description="Look up Norwegian companies by organisation number and return sourced, dated facts.")
    ap.add_argument("orgnr", nargs="*", help="organisation numbers (spaces/MVA suffix allowed)")
    ap.add_argument("--input", "-i", action="append", default=[], help="file with organisation numbers (txt/csv/json); '-' for stdin")
    ap.add_argument("--out", "-o", help="output directory (writes companies/*.json, profiles.jsonl, index.csv, run_report.json)")
    ap.add_argument("--sample", type=int, help="pick N random registered companies and profile them")
    ap.add_argument("--seed", type=int, help="random seed for --sample")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--retry-incomplete", action="store_true",
                    help="with --out: re-check only the stored profiles whose lookup failed or whose register source was "
                         "down in that run; all other stored profiles are kept as they are")
    ap.add_argument("--reports-only", metavar="DIR",
                    help="rebuild report.html (and its pages) from the profiles already stored in DIR; no lookups")
    ap.add_argument("--max-requests", type=int, help="hard cap on outbound HTTP requests (default 1900; scaled up for --sample)")
    ap.add_argument("--time-limit", type=int, default=40 * 60, help="seconds before the agent stops starting new requests (default 2400)")
    ap.add_argument("--no-web", action="store_true", help="skip fetching company websites")
    ap.add_argument("--no-wikidata", action="store_true", help="skip Wikidata")
    ap.add_argument("--no-news", action="store_true", help="skip dated news and announcements")
    ap.add_argument("--compact", action="store_true",
                    help="write profiles as a few files under 18 MB (profiles-NN.jsonl, report pages) instead of one file per company")
    ap.add_argument("--bing", action="store_true",
                    help="also use Bing RSS (web + news search). Off by default: Bing's RSS terms limit reuse")
    ap.add_argument("--compare", action="store_true", help="print a side-by-side comparison of the given companies")
    ap.add_argument("--check", action="store_true", help="live connectivity + schema check against known companies")
    ap.add_argument("--audit", metavar="DIR", help="audit saved profiles in DIR (receipts, news, figures, wording)")
    ap.add_argument("--audit-sample", type=int, metavar="N",
                    help="profile N random real companies and report everything the self-audit withheld or flagged")
    ap.add_argument("--debug-search", metavar="ORGNR", help="show raw website-search results for one company")
    ap.add_argument("--format", choices=["json", "text", "html"], default="json",
                    help="stdout format when no --out is given (html writes company_report.html)")
    a = ap.parse_args(argv)

    if a.check:
        return live_check()
    if a.audit:
        return audit_dir(a.audit)
    if a.audit_sample:
        return audit_sample(a.audit_sample, a.seed)
    if a.debug_search:
        from nca import web
        from nca.sources import fetch_entity
        h = Http(max_requests=10)
        kind, rec, _, _ = fetch_entity(h, a.debug_search)
        print("Company:", (rec or {}).get("navn"))
        print("Domains that will be checked:", web.guess_domains((rec or {}).get("navn")))
        from nca.facts import FactSet
        from nca.sources import discover_website
        fs, rej = FactSet(), []
        st, sts = discover_website(h, fs, a.debug_search, rec, rej)
        print("Result:", (st or {}).get("url") or "no website proven", "| rejected:", rej)
        for f in fs.items:
            print(f"  {f['field']}: {str(f['value'])[:90]}  [{f['confidence']}]")
        from nca import news as N
        r = h.get(N.GDELT.format(q="equinor"), retries=0)
        print("GDELT test: http", r.status, r.text()[:200].replace("\n", " "))
        return 0
    if a.reports_only:
        from nca.runner import _load_bundles, write_reports
        from nca.util import now_iso
        stored = list(_load_bundles(a.reports_only).values())
        if not stored:
            print(f"No stored profiles found in '{a.reports_only}'", file=sys.stderr)
            return 2
        write_reports(a.reports_only, stored, now_iso(), compact=True)
        big = [(f, os.path.getsize(os.path.join(a.reports_only, f))) for f in sorted(os.listdir(a.reports_only))
               if os.path.isfile(os.path.join(a.reports_only, f))]
        print(f"Rebuilt the report for {len(stored)} profiles. Files in '{a.reports_only}':", file=sys.stderr)
        for f, n in big:
            print(f"  {f:<28} {n / 1_000_000:6.1f} MB" + ("   <-- too big for GitHub web upload" if n > 25_000_000 else ""),
                  file=sys.stderr)
        return 0
    budget = a.max_requests or (a.sample * 16 + 600 if a.sample else 1900)
    http = Http(max_requests=budget, deadline=time.time() + a.time_limit)

    inputs = list(a.orgnr)
    for path in a.input:
        inputs.append(sys.stdin.read() if path == "-" else path)
    orgnrs = read_orgnrs(inputs) if inputs else []
    if a.sample:
        print(f"Sampling {a.sample} companies from Enhetsregisteret…", file=sys.stderr)
        orgnrs += [n for n in sample_orgnrs(http, a.sample, seed=a.seed) if n not in orgnrs]
    if a.retry_incomplete:
        if not a.out:
            print("--retry-incomplete needs --out (the folder holding the stored profiles)", file=sys.stderr)
            return 2
        from nca.runner import _load_bundles, needs_retry
        stored = _load_bundles(a.out)
        retry = [n for n, p in stored.items() if needs_retry(p)]
        print(f"{len(stored)} stored profiles; {len(retry)} need another check.", file=sys.stderr)
        if not retry:
            return 0
        orgnrs = retry
        if not a.max_requests:
            http.max_requests = len(retry) * 16 + 600
    if not orgnrs:
        bad = [x for x in a.orgnr if x]
        if bad:  # an argument without 9 digits: still answer honestly
            print(json.dumps(build_profile(http, bad[0]), ensure_ascii=False, indent=2))
            return 0
        ap.print_help()
        return 2

    if a.out:
        if a.sample:
            os.makedirs(a.out, exist_ok=True)
            with open(os.path.join(a.out, "sample_orgnrs.txt"), "w") as fh:
                fh.write("\n".join(orgnrs) + "\n")
        profiles, report = run_batch(http, orgnrs, a.out, workers=a.workers, use_web=not a.no_web,
                                     use_wikidata=not a.no_wikidata, use_news=not a.no_news, use_bing=a.bing,
                                     compact=a.compact, keep_others=a.retry_incomplete)
        print(json.dumps(report, indent=2), file=sys.stderr)
        print(f"\nDone: {report['companies']} profiles, {report['dated_news_items']} dated news items, "
              f"{report['outbound_requests']} requests, ${report['external_api_cost_usd']:.2f} API cost.", file=sys.stderr)
        files = sorted(f for f in os.listdir(a.out) if re.fullmatch(r"profiles(-\d+)?\.jsonl", f))
        print(f"Open {os.path.join(a.out, 'report.html')} in a browser to browse the results; machine-readable files: "
              f"{', '.join(files)}, index.csv" + ("" if a.compact else ", companies/*.json"), file=sys.stderr)
        return 0

    # No output dir: print to stdout
    out = []
    for n in orgnrs:
        p = build_profile(http, n, use_web=not a.no_web, use_wikidata=not a.no_wikidata, use_news=not a.no_news, use_bing=a.bing)
        out.append(p)
        if a.format == "text":
            print(to_text(p) + "\n")
    if a.compare:
        from nca.runner import compare_table
        print(compare_table(out))
    elif a.format == "json":
        print(json.dumps(out[0] if len(out) == 1 else out, ensure_ascii=False, indent=2))
    if a.format == "html":
        from nca.report import write_report
        from nca.util import now_iso
        write_report("company_report.html", out, now_iso())
        print("Wrote company_report.html – open it in a browser.", file=sys.stderr)
    print(f"\n[{http.used} outbound requests, $0.00 external API cost]", file=sys.stderr)
    return 0


def _print_audit(profiles, out_path=None):
    from collections import Counter
    from nca.audit import audit_profile
    withheld, issues, facts = Counter(), Counter(), 0
    examples = []
    for p in profiles:
        facts += len(p.get("facts") or [])
        for w in (p.get("verification") or {}).get("withheld") or []:
            withheld[w["field"]] += 1
            if len(examples) < 25:
                examples.append({"org": p.get("organisation_number"), "kind": "withheld", **w})
        for i in audit_profile(p):
            issues[i["check"]] += 1
            if len(examples) < 25:
                examples.append({"org": p.get("organisation_number"), "kind": "issue", **i})
    print(f"\nAUDIT: {len(profiles)} companies, {facts} published facts")
    print(f"  withheld (could not be proven, not published): {sum(withheld.values())}  {dict(withheld.most_common(12))}")
    print(f"  consistency issues in published output:       {sum(issues.values())}  {dict(issues)}")
    for e in examples[:25]:
        print("   -", json.dumps(e, ensure_ascii=False)[:230])
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({"withheld": dict(withheld), "issues": dict(issues), "examples": examples}, fh, ensure_ascii=False, indent=2)
        print(f"  details written to {out_path}")
    print("RESULT:", "CLEAN" if not issues else "ISSUES FOUND – send this output for fixing")
    return 0 if not issues else 1


def audit_dir(d):
    from nca.runner import _load_bundles
    profs = list(_load_bundles(d).values())
    cdir = os.path.join(d, "companies")
    if os.path.isdir(cdir):
        for name in os.listdir(cdir):
            if name.endswith(".json"):
                with open(os.path.join(cdir, name), encoding="utf-8") as fh:
                    profs.append(json.load(fh))
    return _print_audit(profs)


def audit_sample(n, seed=None):
    http = Http(max_requests=n * 16 + 200, deadline=time.time() + 40 * 60)
    orgnrs = sample_orgnrs(http, n, seed=seed)
    print(f"Auditing {len(orgnrs)} random companies…", file=sys.stderr)
    from nca.runner import run_batch
    profiles, report = run_batch(http, orgnrs, "audit_run", workers=8, progress=True, compact=True)
    print(json.dumps({k: report[k] for k in ("companies", "facts_per_company_avg", "facts_with_content_hash",
                                             "facts_with_exact_or_derived_quote", "dated_news_items", "outbound_requests")}))
    return _print_audit(profiles, os.path.join("audit_run", "audit.json"))


def live_check():
    """Hits every source for two large and three small companies and shows what came back."""
    big_expect = ["legal_name", "legal_form", "employees", "industry_code_1", "ceo", "board_chair", "auditor",
                  "subunit_count", "accounts_period", "revenue", "net_income", "total_assets", "equity",
                  "lei", "wikidata_id", "stock_listings", "website_title", "website_shows_orgnr", "news_official",
                  "address_official", "address_coordinates"]
    small = ("975820440", "993114316", "982826233")
    http = Http(max_requests=160)
    http.keep_bodies = True          # keep raw responses so a withheld fact can be diagnosed
    ok = True
    debug = []
    for n in ("923609016", "984851006") + small:
        before = http.used
        p = build_profile(http, n)
        got = {f["field"] for f in p["facts"]}
        quoted = sum(1 for f in p["facts"] if (f.get("reference") or {}).get("quote"))
        hashed = sum(1 for f in p["facts"] if (f.get("reference") or {}).get("content_sha256"))
        print(f"\n{n} {p.get('name')} — status {p.get('status')} — {len(p['facts'])} facts — {http.used - before} requests"
              f" — {hashed} with content hash, {quoted} with quote")
        if p.get("conclusion"):
            print(f"  conclusion [{p['conclusion']['verdict']}]: {p['conclusion']['text'][:160]}")
        for f in p["facts"]:
            if not (f.get("reference") or {}).get("quote"):
                print(f"  NO QUOTE: {f['field']} = {str(f['value'])[:60]}")
        for w in (p.get("verification") or {}).get("withheld") or []:
            print(f"  WITHHELD: {w['field']} = {str(w['value'])[:50]} ({w['reason'][:80]})")
            res = http.seen.get(w.get("source_url") or "")
            debug.append(f"== {n} {w['field']} = {str(w['value'])[:120]}\n   reason: {w['reason']}\n   url: {w.get('source_url')}\n"
                         + ((res.text()[:4000] if res is not None and res.body else "(no body kept)") + "\n"))
        for i in (p.get("verification") or {}).get("self_audit") or []:
            print(f"  AUDIT: [{i['severity']}] {i['check']}: {i['detail'][:110]}")
        for s in p.get("sources", []):
            print(f"  source {s['source']:<24} {s['status']:<10} http {s.get('http_status')} {s.get('note') or ''}")
        if n not in small:
            missing = [e for e in big_expect if e not in got]
            print("  missing expected:", ", ".join(missing) or "none")
        for f in ("website", "website_discovered", "website_emails", "website_phones", "website_address", "website_about"):
            v = next((x["value"] for x in p["facts"] if x["field"] == f), None)
            if v:
                print(f"  {f}: {str(v)[:90]}")
        for it in (p.get("news") or [])[:5]:
            print(f"  news {it['published']} [{it['kind']}] {it['title'][:60]}"
                  + (f" (English: {it['title_en']})" if it.get("title_en") else "") + f" – {it['source']}")
        for rj in p.get("verification", {}).get("rejected", []):
            print("  rejected:", str(rj)[:150])
        ok = ok and p.get("status") in ("active", "registered", "under_liquidation", "bankrupt") and bool(p["facts"]) \
            and not (p.get("verification") or {}).get("self_audit") and not (p.get("verification") or {}).get("withheld")
    if debug:
        with open("check_debug.txt", "w", encoding="utf-8") as fh:
            fh.write("\n".join(debug))
        print("\nRaw source text for the withheld facts was saved to check_debug.txt")
    print("\nRESULT:", "PASS" if ok else "CHECK OUTPUT ABOVE", f"({http.used} requests for 5 companies)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
