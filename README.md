# Norway Company Facts Agent

Give it a Norwegian organisation number; it returns a verified company profile where **every fact has a source link, a source date and an explanation of how it was matched to that exact company**.

```bash
python agent.py --input companies.txt --out output
```

No installs, no API keys, no paid services: Python 3.9+ standard library only. **External API cost: $0.00.**

---

## Quick start

| Task | Command |
|---|---|
| One company (JSON) | `python agent.py 923609016` |
| One company (readable) | `python agent.py 923609016 --format text` |
| One company (browser page) | `python agent.py 923609016 --format html` → opens as `company_report.html` |
| Daily test – a list of numbers (txt / csv / json, any layout) | `python agent.py --input companies.txt --out output` |
| Build 1,000 random profiles | `python agent.py --sample 1000 --seed 2026 --out profiles` |
| Refresh / update existing profiles | run the same `--out` command again |
| Live health check of all sources | `python agent.py --check` |
| Offline tests | `python -m unittest discover -s tests -v` |

Org numbers may be written `923609016`, `923 609 016` or `NO 923 609 016 MVA`.

## Outputs (`--out DIR`)

| File | Contents |
|---|---|
| `companies/<orgnr>.json` | Full profile per company (facts, evidence, summary, gaps, changes, history) |
| `profiles.jsonl` | All profiles, one per line |
| `index.csv` | One row per company: name, status, form, municipality, industry, employees, revenue, net income, CEO, website, LEI, summary |
| `run_report.json` | Companies, statuses, facts per company, dated news items, outbound requests used, runtime, cost |
| `report.html` | **Open in any browser:** searchable list of all companies with summary, key points, risk signals, dated news and every fact with its source link |

### A fact looks like this

```json
{
  "field": "revenue",
  "label": "Operating revenue (company)",
  "category": "financials",
  "value": {"amount": 900000000000, "currency": "NOK", "period": "2024-01-01/2024-12-31", "statement": "company"},
  "source": "brreg_regnskap",
  "source_name": "Brønnøysund Register Centre – Register of Company Accounts (Regnskapsregisteret)",
  "source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/923609016",
  "evidence_url": "https://virksomhet.brreg.no/nb/oppslag/enheter/923609016",
  "as_of": "2024-12-31",
  "retrieved_at": "2026-10-11T09:00:00Z",
  "first_seen": "2026-10-11T09:00:00Z",
  "last_verified": "2026-10-11T09:00:00Z",
  "confidence": "high",
  "match": "Annual statement filed under this exact organisation number (virksomhet.organisasjonsnummer verified)",
  "note": "Exact figure as filed; not estimated or recomputed"
}
```

Each profile also has:
- `news` – **dated news and announcements**, newest first. Every item has `title`, `url` (the original source), `published` (full YYYY-MM-DD date), `source`, `kind` and its own `company_match` reason
- `key_points` – sector (English NACE section), age, size class, latest financials as filed, leadership, latest news, listing
- `risk_signals` – bankruptcy/liquidation, negative equity, losses, overdue accounts, recent role changes, recent official announcements
- `summary` – a plain-English paragraph built only from the published facts
- `profile` – the same facts grouped by category for easy reading
- `gaps` – what was **not** found and why (e.g. "Legal form ENK is normally not required to file annual accounts")
- `verification` – checks that passed and any candidate records that were **rejected** as belonging to another company
- `changes` / `history` – what changed since the previous run

## Sources (all public and permitted)

| Source | What it gives | Requests / company |
|---|---|---|
| Brønnøysund **Enhetsregisteret** (NLOD 2.0) | Name, legal form, addresses, municipality, NACE codes, employees, founding/registration dates, VAT & register memberships, purpose, website/contact, bankruptcy/liquidation/deletion status | 1 (+1 for sub-units) |
| Brønnøysund **roles** (NLOD 2.0) | CEO, chair, board, deputies, auditor, accountant, owners/partners, with last-changed date | 1 |
| Brønnøysund **sub-units** (NLOD 2.0) | Number of establishments and the first 15 with municipality and staff | 1 |
| Brønnøysund **Regnskapsregisteret** (NLOD 2.0) | Latest filed accounts: revenue, costs, EBIT, pre-tax profit, net income, assets, equity, liabilities – company and group level | 1 (skipped for forms with no filing duty) |
| **GLEIF** (CC0) | LEI, LEI status/renewal, direct & ultimate parent | 1–3 |
| **Wikidata** (CC0) | Wikidata ID, description, inception, ISIN, stock exchange & ticker, parent organisation, logo, Wikipedia links | 1 |
| Company's **own website** (registered in Brreg) | Title, description, brand name, whether it shows the org number, social profiles | 1–2 |
| Brønnøysund **announcements** (kunngjøringer) | Dated official announcements: board changes, address/name changes, mergers, liquidation, bankruptcy | 1 |
| Company **website news feed** (RSS/Atom linked from the registered site) | Dated company news | 0–1 |
| **Press coverage** (Bing News RSS) | Dated articles naming the exact company | 1 (skipped for sole proprietorships, whose names are personal names) |

### How news is matched to the right company
- **Official announcements** are fetched by organisation number, and the page must show that number.
- **Company news** comes only from a feed on the same domain as the website registered for that number.
- **Press articles** are kept only if the title or snippet contains the **exact registered name including its legal suffix** (e.g. "Maleran AS"; such names are unique in Foretaksregisteret), or the name plus an independent corroborator from the registry (municipality, CEO/chair surname, website domain, ticker), or the organisation number. Everything else is rejected and counted. Items without a full publication date are never published.

Typical cost: **about 10–13 outbound requests per company** → roughly 1,000–1,300 for the daily 100 companies (limit 2,000; hard cap 1,900).

## How wrong-company errors are prevented

1. **Checksum** – invalid numbers (mod-11) are rejected before any lookup.
2. **Exact-key retrieval** – every source is queried by the org number itself, never by name search.
3. **Record verification** – the returned record must contain the same org number (accounts: `virksomhet.organisasjonsnummer`; GLEIF: `registeredAs` + country NO; Wikidata: property P2333).
4. **Cross-checks** – GLEIF and Wikidata records are accepted only if their legal name matches the registry name (normalised similarity, legal suffixes ignored) or their LEI agrees. Conflicting or ambiguous records are dropped and listed under `verification.rejected`.
5. **No fabricated numbers** – financial values are copied exactly as filed, with currency and period. Nothing is estimated, derived or filled in; missing values are explained in `gaps`.
6. **Website evidence** – the agent checks whether the company's site displays the org number; if not, website facts are kept at `medium` confidence.

## Keeping profiles current

Running again with the same `--out` folder re-fetches live data and compares it with the stored profile:
- `changes` lists every **added**, **changed** (old → new) and **removed** fact; `history` keeps the last 30 change sets.
- Unchanged facts keep their `first_seen` date and get a new `last_verified` date.
- If a source is temporarily down, its old facts are **kept and flagged `stale`**, never silently deleted.
- `as_of` carries the source's own date (accounting period end, role-group last-changed date, LEI update date) so freshness is visible per fact.

## Limits and safety

- Hard request cap (default **1,900**, `--max-requests`) counting every attempt and retry.
- Time limit (default **40 min**, `--time-limit`) – stops starting new requests before the 45-minute limit.
- 8 parallel workers, retries with back-off on 429/5xx, honours `Retry-After`.
- One failing company never stops the run; a budget-truncated profile never overwrites a good stored one.

## Model / API details

- **No LLM or paid API is used.** Matching, verification and explanations are deterministic code, so there is no hallucination risk and runs are reproducible.
- Runtime: Python ≥ 3.9, standard library only (`requirements.txt` is empty).
- Expected run cost: **$0.00 per daily test** in external API fees. Compute: a few minutes on any laptop or a free GitHub Actions runner.

## Project layout

```
agent.py            CLI (one command)
nca/http.py         budgeted HTTP client
nca/sources.py      source adapters + identity checks
nca/profile.py      profile assembly, explanation, gaps, update diff
nca/runner.py       batch runs, sampling, output files
nca/facts.py        fact model and source catalogue
tests/              offline tests with a fake HTTP layer
.github/workflows/  builds the 1,000-profile dataset in the cloud
```
