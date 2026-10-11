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
    ap.add_argument("--check", action="store_true", help="live connectivity + schema check against known companies")
    ap.add_argument("--format", choices=["json", "text"], default="json", help="stdout format when no --out is given")
    a = ap.parse_args(argv)

    if a.check:
        return live_check()
    budget = a.max_requests or (a.sample * 12 + 600 if a.sample else 1900)
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
        profiles, report = run_batch(http, orgnrs, a.out, workers=a.workers,
                                     use_web=not a.no_web, use_wikidata=not a.no_wikidata)
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 0

    # No output dir: print to stdout
    out = []
    for n in orgnrs:
        p = build_profile(http, n, use_web=not a.no_web, use_wikidata=not a.no_wikidata)
        out.append(p)
        if a.format == "text":
            print(to_text(p) + "\n")
    if a.format == "json":
        print(json.dumps(out[0] if len(out) == 1 else out, ensure_ascii=False, indent=2))
    print(f"\n[{http.used} outbound requests, $0.00 external API cost]", file=sys.stderr)
    return 0


def live_check():
    """Hits every source for Equinor ASA and DNB Bank ASA and reports which key facts came back."""
    expect = ["legal_name", "legal_form", "employees", "industry_code_1", "ceo", "board_chair", "auditor",
              "subunit_count", "accounts_period", "revenue", "net_income", "total_assets", "equity",
              "lei", "wikidata_id", "stock_listings", "website_title", "website_shows_orgnr"]
    http = Http(max_requests=60)
    ok = True
    for n in ("923609016", "984851006"):
        p = build_profile(http, n)
        got = {f["field"] for f in p["facts"]}
        print(f"\n{n} {p.get('name')} — status {p.get('status')} — {len(p['facts'])} facts")
        for s in p.get("sources", []):
            print(f"  source {s['source']:<24} {s['status']:<10} http {s.get('http_status')} {s.get('note') or ''}")
        missing = [e for e in expect if e not in got]
        print("  missing expected:", ", ".join(missing) or "none")
        for rj in p.get("verification", {}).get("rejected", []):
            print("  rejected:", rj)
        bad = [s["source"] for s in p.get("sources", []) if s.get("status") == "error"]
        ok = ok and p.get("status") == "active" and "ceo" in got and not bad
    print("\nRESULT:", "PASS" if ok else "CHECK OUTPUT ABOVE", f"({http.used} requests)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
