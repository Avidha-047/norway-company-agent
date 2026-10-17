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
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nca.http import Http  # noqa: E402
from nca.profile import build_profile  # noqa: E402
from nca.runner import read_orgnrs, run_batch, sample_orgnrs, to_text  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Look up Norwegian companies by organisation number and return sourced, dated facts.")
    ap.add_argument("orgnr", nargs="*", help="organisation numbers (spaces/MVA suffix allowed)")
    ap.add_argument("--input", "-i", action="append", default=[], help="file with organisation numbers (txt/csv/json); '-' for stdin")
    ap.add_argument("--out", "-o", help="output directory (writes companies/*.json, profiles.jsonl, index.csv, run_report.json)")
    ap.add_argument("--sample", type=int, help="pick N random registered companies and profile them")
    ap.add_argument("--seed", type=int, help="random seed for --sample")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-requests", type=int, help="hard cap on outbound HTTP requests (default 1900; scaled up for --sample)")
    ap.add_argument("--time-limit", type=int, default=40 * 60, help="seconds before the agent stops starting new requests (default 2400)")
    ap.add_argument("--no-web", action="store_true", help="skip fetching company websites")
    ap.add_argument("--no-wikidata", action="store_true", help="skip Wikidata")
    ap.add_argument("--no-news", action="store_true", help="skip dated news and announcements")
    ap.add_argument("--bing", action="store_true",
                    help="also use Bing RSS (web + news search). Off by default: Bing's RSS terms limit reuse")
    ap.add_argument("--compare", action="store_true", help="print a side-by-side comparison of the given companies")
    ap.add_argument("--check", action="store_true", help="live connectivity + schema check against known companies")
    ap.add_argument("--debug-search", metavar="ORGNR", help="show raw website-search results for one company")
    ap.add_argument("--format", choices=["json", "text", "html"], default="json",
                    help="stdout format when no --out is given (html writes company_report.html)")
    a = ap.parse_args(argv)

    if a.check:
        return live_check()
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
    budget = a.max_requests or (a.sample * 16 + 600 if a.sample else 1900)
    http = Http(max_requests=budget, deadline=time.time() + a.time_limit)

    inputs = list(a.orgnr)
    for path in a.input:
        inputs.append(sys.stdin.read() if path == "-" else path)
    orgnrs = read_orgnrs(inputs) if inputs else []
    if a.sample:
        print(f"Sampling {a.sample} companies from Enhetsregisteret…", file=sys.stderr)
        orgnrs += [n for n in sample_orgnrs(http, a.sample, seed=a.seed) if n not in orgnrs]
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
                                     use_wikidata=not a.no_wikidata, use_news=not a.no_news, use_bing=a.bing)
        print(json.dumps(report, indent=2), file=sys.stderr)
        print(f"\nDone: {report['companies']} profiles, {report['dated_news_items']} dated news items, "
              f"{report['outbound_requests']} requests, ${report['external_api_cost_usd']:.2f} API cost.", file=sys.stderr)
        print(f"Open {os.path.join(a.out, 'report.html')} in a browser to browse the results; "
              f"machine-readable files: profiles.jsonl, index.csv, companies/*.json", file=sys.stderr)
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


def live_check():
    """Hits every source for two large and three small companies and shows what came back."""
    big_expect = ["legal_name", "legal_form", "employees", "industry_code_1", "ceo", "board_chair", "auditor",
                  "subunit_count", "accounts_period", "revenue", "net_income", "total_assets", "equity",
                  "lei", "wikidata_id", "stock_listings", "website_title", "website_shows_orgnr", "news_official"]
    small = ("975820440", "993114316", "982826233")
    http = Http(max_requests=160)
    ok = True
    for n in ("923609016", "984851006") + small:
        before = http.used
        p = build_profile(http, n)
        got = {f["field"] for f in p["facts"]}
        print(f"\n{n} {p.get('name')} — status {p.get('status')} — {len(p['facts'])} facts — {http.used - before} requests")
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
            print(f"  news {it['published']} [{it['kind']}] {(it.get('title_en') or it['title'])[:70]} – {it['source']}")
        for rj in p.get("verification", {}).get("rejected", []):
            print("  rejected:", str(rj)[:150])
        ok = ok and p.get("status") in ("active", "under_liquidation", "bankrupt") and bool(p["facts"])
    print("\nRESULT:", "PASS" if ok else "CHECK OUTPUT ABOVE", f"({http.used} requests for 5 companies)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
