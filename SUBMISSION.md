# Submission – Norway Company Facts Agent

| Item | Value |
|---|---|
| Repository | https://github.com/<your-username>/norway-company-agent |
| Commit hash | `<paste output of: git rev-parse HEAD>` |
| Command | `python agent.py --input companies.txt --out output` |
| Company profiles | `profiles/` folder – 1,000+ profiles (`profiles/companies/*.json`, `profiles/profiles.jsonl`, `profiles/index.csv`) |
| Model / API | No LLM or paid API. Deterministic Python 3.9+ (standard library only). Free public sources: Brønnøysund Enhetsregisteret, roles and sub-units API, Regnskapsregisteret (NLOD 2.0); GLEIF LEI API (CC0); Wikidata SPARQL (CC0); company websites registered in Enhetsregisteret. |
| Expected run cost | $0.00 external API cost per daily test. About 700–900 outbound requests and 3–8 minutes for 100 companies (hard caps: 1,900 requests, 40 minutes). |
| Built with JBOX | No |

## Notes for the evaluators
- Input: any text, CSV or JSON file containing organisation numbers; numbers may also be passed as arguments.
- Every fact carries `source_url`, `evidence_url`, `as_of` (source date), `retrieved_at`, `confidence` and a `match` explanation.
- Re-running with the same `--out` folder updates profiles and records changes (`changes`, `history`, `first_seen`, `last_verified`, `stale`).
- Financial values are copied exactly as filed in Regnskapsregisteret; nothing is estimated.
