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
| Daily test – a list of numbers (JSONL from Builderr's selector, txt / csv / json) | `python agent.py --input companies.jsonl --out output` |
| Build 1,000 random profiles | `python agent.py --sample 1000 --seed 2026 --out profiles` |
| Refresh / update existing profiles | run the same `--out` command again |
| Live health check of all sources | `python agent.py --check` |
| Self-audit on 50 random real companies | `python agent.py --audit-sample 50` |
| Re-check only stored profiles whose lookup failed or whose register source was down (keeps all others) | `python agent.py --retry-incomplete --out profiles --compact` |
| Audit saved profiles | `python agent.py --audit profiles` |
| Write a few files under 18 MB instead of one per company | add `--compact` |
| Show how a website is found for one company | `python agent.py --debug-search 975820440` |
| Compare companies side by side | `python agent.py 923609016 984851006 --compare` (also: tick "compare" in `report.html`) |
| Offline tests | `python -m unittest discover -s tests -v` |

Org numbers may be written `923609016`, `923 609 016` or `NO 923 609 016 MVA`.

## Optional: Brave Search for website discovery

Set `BRAVE_SEARCH_API_KEY` (the variable used by the starter kit's `run_brave_discovery.py`) to let the agent
search for the website of companies that register none. One query per such company
(`"<legal name>" <orgnr> <municipality>`); directories and social networks are dropped, results are used only as
crawl candidates and are not stored, and a site is published only when its own pages prove it is this exact
company. Without the variable nothing changes and the run costs $0.

## Output contract (Builderr starter kit)

Every profile follows the starter kit's `OUTPUT_CONTRACT.md` on top of its detailed `facts`:

| Key | Content |
|---|---|
| `run` | run_id, started_at, completed_at, terminal_status |
| `claims[]` | field, family (legal_identity, annual_accounts, leadership, registered_workplaces, official_website, company_profiles, website_contact, job_postings, official_announcements, company_news), value, availability, confidence (0–1), `evidence_ids`, effective date / reporting period |
| `evidence[]` | id, source_url, source_class (official_registry / company_owned / third_party_registry), retrieved_at, content_sha256, `claim_span` (the exact quoted text) |
| `synthesis` | what_it_does, what_changed, what_is_unknown – each statement with its evidence ids |
| `changes`, `errors`, `operations` | material changes since the previous run; sources that failed; requests, runtime_ms, third_party_cost_usd (always 0) |

`output/envelopes.jsonl` holds exactly one terminal envelope per input in this shape (including invalid, unknown and failed inputs), and `run_report.json` records `expected_count`, `emitted_envelopes` and the validation checks.

## Outputs (`--out DIR`)

| File | Contents |
|---|---|
| `companies/<orgnr>.json` | Full profile per company (facts, evidence, summary, gaps, changes, history) |
| `profiles.jsonl` | All profiles, one per line |
| `index.csv` | One row per company: name, status, form, municipality, industry, employees, revenue, net income, CEO, website, LEI, summary |
| `run_report.json` | Companies, statuses, facts per company, dated news items, outbound requests used, runtime, cost |
| `report.html` | **Open in any browser, laptop or phone:** find a company (name, org number, municipality, industry; `report.html#923609016` deep-links), compare companies (tick *Compare* on two or more), and tap **Where this came from** on any fact or news item to see the source, retrieval time, content hash and exact quote |

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
| **Kartverket address register** via Geonorge (CC BY 4.0) | The registered street address confirmed in Norway's official address register, its map position (ETRS89 latitude/longitude) and a map link. Published only on one exact street + number + postcode match; c/o and PO-box lines are never looked up | 1 |
| **Frivillighetsregisteret** (NLOD 2.0) | For units registered there: registration date, activity categories, grassroots-share participation (only fields present in a record carrying this exact org number) | 1 |
| **GLEIF** (CC0) | LEI, LEI status/renewal, direct & ultimate parent | 1–3 |
| **Wikidata** (CC0) | Wikidata ID, description, inception, ISIN, stock exchange & ticker, parent organisation, logo, Wikipedia links | 1 |
| Company's **own website** (registered in Brreg) | Title, description, brand name, "about" text, emails, phone numbers, address, logo, social profiles, schema.org data, contact page | 1–3 |
| **Website discovery** for companies with no registered site: the agent checks the domains that match the registered name (e.g. `maleran.no`, `maleran.com`) | The same website facts, but only after the site is **proven** to belong to the company: its pages show the organisation number (high confidence) or the registered name **and** registered street address + postcode (medium). Otherwise nothing is published and the candidate is listed under `verification.rejected` | 0–3 |
| Brønnøysund **announcements** (kunngjøringer) | Dated official announcements: board changes, address/name changes, mergers, liquidation, bankruptcy | 1 |
| Company **website news feed** (RSS/Atom linked from the registered site) | Dated company news | 0–1 |
| **Press coverage** (GDELT news index; Bing News RSS optional with `--bing`) | Dated articles naming the exact company | 1 (skipped for sole proprietorships, whose names are personal names) |

Bing RSS is **off by default** because Bing's RSS terms restrict reuse of its results; enable it only if that is acceptable for your use (`--bing`).

### How news is matched to the right company
- **Official announcements** are fetched by organisation number, and the page must show that number.
- **Company news** comes only from a feed on the same domain as the website registered for that number.
- **Press articles** are kept only if the title or snippet contains the **exact registered name including its legal suffix** (e.g. "Maleran AS"; such names are unique in Foretaksregisteret), or the name plus an independent corroborator from the registry (municipality, CEO/chair surname, website domain, ticker), or the organisation number. Everything else is rejected and counted. Items without a full publication date are never published.

Typical cost: **about 10–13 outbound requests per company** → roughly 1,000–1,300 for the daily 100 companies (limit 2,000; hard cap 1,900).

## Evidence receipts (every fact)

Every fact and every news item carries a `reference`:

```json
"reference": {
  "url": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
  "retrieved_at": "2026-10-23T11:50:16Z",
  "content_sha256": "c7bd44b46c336b1d6208601cd35df036f6e8eef9a7200f5ff07136a10e07dd6f",
  "content_bytes": 2817,
  "quote": "\"antallAnsatte\": 21000",
  "quote_status": "exact"
}
```

- `content_sha256` is the SHA-256 hash of the exact bytes retrieved, so anyone can verify the content later.
- `quote` is a **verbatim substring** of that content which supports the fact (tested: every quote is checked to be present in the retrieved content).
- `quote_status` is `exact`, or `derived` for facts computed from the quoted text (e.g. the English NACE section looked up from the quoted industry code), with `basis` explaining the step.

## Self-audit (built in)

Before a profile is published, every fact is re-checked against its receipt: the content hash must match the
retrieved bytes, the quote must be verbatim in that content, and the published value must appear in the quote
(numbers digit-for-digit, true/false flags as the quoted literal). Anything that fails is **withheld** – listed
under `verification.withheld` with the reason – instead of being published. Each profile is then checked for
consistency (`verification.self_audit`): every figure in the summary and conclusion must equal a published
amount, news items need full publication dates and receipts, translations must be exact, shared/group
websites must not contribute company facts, and wording must not imply a filing is the most recent one unless it is.

## Website ownership

A website is treated as the company's own only if its pages show **this organisation number** (and no other
company's number, unless this one stands next to the company's full name) or the **full registered name with its
legal suffix** ("Telenor Norge AS" – "Telenor Group" is not enough). For the registered site only, the registered
phone number, or the registered e-mail when it is on the site's own domain, also counts. A group footer listing other
companies' numbers makes it a **shared or parent-group site**; a registered address that redirects to another domain
without showing this number makes it **ownership not proven**. In both cases its social accounts, news, contact
details and job posts are **not** attributed to the company. Sites not registered in
Enhetsregisteret (name-matching domains, or a site listed on Wikidata) are used only if they show this
organisation number, or the full registered name **and** the registered address. Contact details are published only
for mailboxes on the site's own domain and never from web-agency credits ("Laget av …").

## News rules

- Official announcements keep the **original Norwegian title**; an English `title_en` is added only for exact, known announcement categories (literal translation), never guessed.
- Website news must be a **real article with a date and a headline** on the company's own site. Rejected: Atom `updated` dates (never a publish date); pages linked from the site's menu, header or footer (fixed pages); service/offer pages (catering, prices, "we offer"), staff and team pages, product, category, contact and job pages; slider, draft and copy items (`slide-2`, `…-cloned`); and posts that share one date with two or more others (a bulk import of old posts, not publication dates).
- Stock-quote pages are not news. Press search (Bing RSS / GDELT) is off by default (`--bing` to enable): Bing's RSS terms restrict reuse and GDELT dates are index dates, not publication dates.

## Social links

A social account is published only if it is linked from the company's own (proven) website **and** its account
name matches the company: its registered name, its website's domain or its brand. Generic links (`facebook.com/pages`,
`/groups`, `profile.php`, `instagram.com/explore`), numeric ids that cannot be checked (`facebook.com/2008`, YouTube
channel ids) and accounts of a related brand, venue, group or holding company are refused, and each refusal is listed
with its reason under `verification.rejected`.

## "Can't tell" and confidence

- When a website names a **look-alike company** (e.g. "Maleran AS" on a candidate site for MALERAN HOLDING AS), or shows
  this company's name together with other organisation numbers (e.g. a housing co-op on its manager's site), the agent
  says **"can't tell"** with the reason and leaves the website link blank.
- Every profile has a `confidence` block, shown on the report page and in the comparison: each kind of link
  (register facts, website, social media, LEI, Wikidata, announcements, website news) with its level
  (high / medium / can't tell) and why, plus an overall level.

## Terminal envelope (one per input)

Every profile has an `envelope` with the seven required sections, each with a state from
`available / not_available / blocked / not_applicable / ambiguous / failed`, the fields behind it and the reason.
Absence is never turned into zero: an accounts outage is `failed`, a legal form without a duty to file is
`not_applicable`, a look-alike website is `ambiguous`. Hiring comes only from the careers page of a
**verified** company website (schema.org JobPosting naming this employer, adverts on recognised applicant-tracking
systems, or advert pages under the careers path); a checked careers page with no identifiable posting is reported
as such, never as "not hiring". Invalid, unknown and unreadable organisation numbers get an envelope too.

## Accounts: two years

Next to the latest filed statement the agent shows the **previous year's** company-level statement (taken from the
same response, or asked for once by year and accepted only if its period and organisation number match), and a key
point such as *"Change from 2024 to 2025: revenue up from NOK a to NOK b"* using only exact filed figures.

## Status wording

"Active" is used only when Enhetsregisteret explicitly states the bankruptcy, liquidation and compulsory-dissolution
flags as false. Sub-units are described as establishments whose status is held on their main unit. Gaps are worded by
what actually happened to each source (checked and empty, could not be reached, not queried), so a failed or skipped
lookup is never reported as "does not exist".

## Conclusion (synthesis)

Each profile has `conclusion` with a `verdict` (e.g. *No registry risk flags*, *Needs attention*, *Incomplete*) and short claims; every claim lists the facts that support it (`supported_by`), and figures are exact as filed.

## How wrong-company errors are prevented

1. **Checksum** – invalid numbers (mod-11) are rejected before any lookup.
2. **Exact-key retrieval** – every source is queried by the org number itself, never by name search.
3. **Record verification** – the returned record must contain the same org number (accounts: `virksomhet.organisasjonsnummer`; GLEIF: `registeredAs` + country NO; Wikidata: property P2333).
4. **Cross-checks** – GLEIF and Wikidata records are accepted only if their legal name matches the registry name (normalised similarity, legal suffixes ignored) or their LEI agrees. Conflicting or ambiguous records are dropped and listed under `verification.rejected`.
5. **No over-claiming** – an employee number is published only when the register holds one ("no count registered" is reported as unknown, never as 0); when Enhetsregisteret lists a newer filing than the figures returned, the profile says so instead of presenting older figures as current.
6. **No fabricated numbers** – financial values are copied exactly as filed, with currency and period. Nothing is estimated, derived or filled in; missing values are explained in `gaps`.
7. **Website evidence** – the agent checks whether the company's site displays the org number; if not, website facts are kept at `medium` confidence.

## Keeping profiles current

Running again with the same `--out` folder re-fetches live data and compares it with the stored profile:
- `changes` lists every **added**, **changed** (old → new) and **removed** fact; `history` keeps the last 30 change sets.
- Unchanged facts keep their `first_seen` date and get a new `last_verified` date.
- If a source is temporarily down, its old facts are **kept and flagged `stale`**, never silently deleted.
- `as_of` carries the source's own date (accounting period end, role-group last-changed date, LEI update date) so freshness is visible per fact.

## Limits and safety
- **Budget-aware:** core register lookups are reserved for every company still waiting; optional sources (website discovery, contact pages, press, Wikidata) are used only when the request budget allows, so the last company in a batch is never starved.
- Wikidata is queried only where an item is plausible (listed/large companies or companies with an LEI).

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
