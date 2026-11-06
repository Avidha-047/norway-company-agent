# Submission – Norway Company Facts Agent (version 8)

| Item | Value |
|---|---|
| Repository | https://github.com/Avidha-047/norway-company-agent |
| Commit | see the commit link in the submission email (the latest commit on `main`) |
| Command | `python agent.py --input companies.jsonl --out output` (JSONL from Builderr's batch selector, or txt/csv/json; optional `--run-id ID`) |
| Browse results (laptop or phone) | open `output/report.html` – find, compare, and "Where this came from" on every fact |
| Model / API | No LLM. Optional: Brave Search API for website discovery when `BRAVE_SEARCH_API_KEY` is set (Builderr-supplied key; at most one query per company without a website). Without it, no paid API. Python 3.9+, standard library only. Sources: Brønnøysund Enhetsregisteret (entities, roles, sub-units, announcements) Regnskapsregisteret and Frivillighetsregisteret (NLOD 2.0); Kartverket's official address register via Geonorge (CC BY 4.0); GLEIF (CC0); Wikidata (CC0); the company's own website. |
| Expected run cost | $0.00 external API cost (no paid API, no LLM). About 10–11 requests per company (measured on 100 companies from Builderr's universe: 1,061 requests in 206 s). The request cap scales with the batch: 19 per company (at least 1,900), under the official 2,000 per 100 companies; runs stop starting new requests after 40 minutes. A source that stops answering is paused after 5 timeouts in a row. |
| 100-company smoke test | `smoke-test/` – envelopes, profiles, index, report page, run report and self-audit of a fresh live run on 100 companies drawn from Builderr's public universe (100/100 envelopes, 7,230 facts, audit clean); see `smoke-test/README.md`. |
| Pre-built profiles | `profiles/` holds stored profiles built with this agent; every fact cites its live source URL, retrieval time, SHA-256 of the content and the exact quote. |
| Built with JBOX | The agent: no. The Signalpost Explorer website (JBOX bonus) is built with JBOX from this agent's output. |

## Changes in version 8 (aligned with the official starter kit)
- **Starter-kit output contract** (`OUTPUT_CONTRACT.md`): every profile now carries `run`, `claims[]` (field, family, value, availability, numeric confidence, `evidence_ids`, effective date / reporting period), `evidence[]` (id, source_url, `source_class` – official_registry / company_owned / third_party_registry, retrieved_at, content_sha256, `claim_span`), `changes`, `errors` and `operations` (requests, runtime_ms, third_party_cost_usd). `output/envelopes.jsonl` holds exactly one terminal envelope per input in that shape; `run_report.json` adds `expected_count`, `emitted_envelopes`, `validation` (count, uniqueness, terminal states, zero silent drops) and p50/p95 runtime. Each of the seven required sections without data appears as a claim with its explicit state and reason.
- **Synthesis with sources**: `synthesis.what_it_does`, `what_changed` (year-on-year filed figures, latest dated activity, changes since the previous run, hiring) and `what_is_unknown` – every statement lists the evidence ids it rests on.
- **Job postings** from the careers page of the company's **verified** own website (a company-owned source in the source policy): only schema.org JobPosting data naming this employer, adverts on recognised applicant-tracking systems, or advert pages under the careers path; "see all", values and open-application pages are refused. A checked careers page with no identifiable posting is reported as such, never as "not hiring". At most one extra request per verified site.
- **Website discovery**: (1) websites registered on the company's own establishments (sub-units) are tried after the registered e-mail domain; (2) more name-based domain candidates (ø/å/æ spelled both ways, first two words of long names); (3) **optional Brave Search API** – the route recommended in the starter kit (`docs/external-connectors.md`), read from the same environment variable as `scripts/run_brave_discovery.py`: **`BRAVE_SEARCH_API_KEY`**. One query per company without a website (`"<legal name>" <orgnr> <municipality>`); results are used transiently as crawl candidates only (directories and social networks dropped) and nothing from the search response is stored. Every candidate must still prove on its own pages that it is this exact company. Without the variable the agent runs exactly as before; cost per batch is then $0.
- **Websites that failed to load** are retried at the other common form of the registered address (`www.` ↔ bare domain) before plain http, with a standard `Accept-Language` header.

## Changes in version 7.1
- **Phone fix**: a broken short number such as "+47 79 29 40" on a website is no longer read as 47 79 29 40 – the country code is never reused as digits.

## Changes in version 7
- **Terminal envelope**: every input ends in one envelope with the seven required sections (legal identity, annual accounts and history, leadership and workplaces, verified website and profiles, hiring and dated activity, claim-level evidence, refresh metadata), each with an explicit state – `available`, `not_available`, `not_applicable`, `ambiguous`, `failed` or `blocked` – and its reason. Invalid, unknown and failed inputs get an envelope too. Hiring was then reported as `not_available` (version 8 adds careers-page job postings).
- **Request cap scales with the batch** (19 per company), so a 1,000-company official batch is not cut short.
- **100-company smoke-test report** committed in `smoke-test/`.

## Changes in version 6 (from the feedback on version 5)
- **Social links** are kept only when linked from the company's own site and the account name matches the company (name, domain or brand); generic, id-only and related/group accounts are refused with a reason.
- **News** must be a dated article with a headline: menu/header/footer pages, service and staff pages, sliders and bulk-imported posts are rejected.
- **Accounts** show the previous year next to the latest one, with an "up / down" key point using exact filed figures.
- **"Can't tell"**: holding/operating look-alikes and sites shared with a manager or parent are reported as "can't tell" with a reason, and the link is left blank.
- **Confidence** per company and per link (register, website, social, LEI, Wikidata, news) on the report page and in the comparison.
- Websites are also looked for at the domain of the registered e-mail address (not webmail/ISP domains); ownership must still be proven.

## Changes in version 5 (from the audit of version 4)
- Every fact and news item has a reference with the **SHA-256 hash of the retrieved content** and the **exact quoted text** that supports it.
- Announcement titles keep the **original Norwegian**; English is added only for exact known categories (literal).
- **Shared/parent-group websites** are detected; their social accounts, news, contacts and jobs are not attributed to the company.
- Website news must be a **real, dated post** (no Atom `updated` dates, no menu/product/category/job pages).
- Each company has a **conclusion** whose every claim lists its supporting facts; all figures are exact as filed.
- `report.html` supports the three UX tasks on laptop and phone: find a company, compare companies, see where a fact came from.
