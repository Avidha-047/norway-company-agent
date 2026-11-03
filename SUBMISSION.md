# Submission – Norway Company Facts Agent (version 7)

| Item | Value |
|---|---|
| Repository | https://github.com/Avidha-047/norway-company-agent |
| Commit | see the commit link in the submission email (the latest commit on `main`) |
| Command | `python agent.py --input companies.txt --out output` |
| Browse results (laptop or phone) | open `output/report.html` – find, compare, and "Where this came from" on every fact |
| Model / API | No LLM and no paid API. Python 3.9+, standard library only. Sources: Brønnøysund Enhetsregisteret (entities, roles, sub-units, announcements) Regnskapsregisteret and Frivillighetsregisteret (NLOD 2.0); Kartverket's official address register via Geonorge (CC BY 4.0); GLEIF (CC0); Wikidata (CC0); the company's own website. |
| Expected run cost | $0.00 external API cost (no paid API, no LLM). About 9–10 requests per company (measured: 842 for 100 companies in 148 s). The request cap scales with the batch: 19 per company (at least 1,900), under the official 2,000 per 100 companies; runs stop starting new requests after 40 minutes. A source that stops answering is paused after 5 timeouts in a row. |
| 100-company smoke test | `smoke-test/` – run report, index, profiles, report page and self-audit of a fresh run of the command above on 100 companies. |
| Pre-built profiles | `profiles/` holds stored profiles built with this agent; every fact cites its live source URL, retrieval time, SHA-256 of the content and the exact quote. |
| Built with JBOX | No |

## Changes in version 7
- **Terminal envelope**: every input ends in one envelope with the seven required sections (legal identity, annual accounts and history, leadership and workplaces, verified website and profiles, hiring and dated activity, claim-level evidence, refresh metadata), each with an explicit state – `available`, `not_available`, `not_applicable`, `ambiguous`, `failed` or `blocked` – and its reason. Invalid, unknown and failed inputs get an envelope too. Hiring is reported as `not_available` (no permitted job-posting source is queried), never as "not hiring".
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
