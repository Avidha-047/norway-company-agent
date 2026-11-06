# 100-company smoke test

Fresh live run on 6 November 2026 (laptop, Windows, Python standard library only, no API key) of:

```
python agent.py --input companies.txt --out smoke-test
python agent.py --audit smoke-test
```

`companies.txt` holds 100 organisation numbers drawn at random from Builderr's public universe
(`signalpost-company-universe-2025.jsonl.gz`, archive SHA-256 `1c89710e…0a5984f5a0384`).

| Result | Value |
|---|---|
| Inputs / terminal envelopes | 100 / 100 (`envelopes.jsonl`, validation passed: exact count, unique numbers, terminal states, zero silent drops) |
| Published facts | 7,230 (72.3 per company), every one with source URL, retrieval time, SHA-256 of the content and an exact or derived quote |
| Self-audit | CLEAN – 0 withheld, 0 consistency issues (`audit.txt`) |
| Outbound requests | 1,059 for 100 companies (budget 1,900); p50 9.4 s, p95 26.4 s per company with 8 parallel workers |
| Run time | 195 s |
| Third-party cost | $0.00 |

Files: `envelopes.jsonl` (starter-kit output contract, one per input), `profiles.jsonl` (full profiles with facts,
claims, evidence, synthesis), `index.csv`, `report.html` (find / compare / "Where this came from", laptop and phone),
`run_report.json`, `audit.txt`.

Run with exactly the code of the commit that contains it (version 8); the audit was re-run on these files with the same code.
