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
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

from .http import BudgetExceeded
from .profile import build_profile
from .sources import BRREG
from .util import ORGNR_RE, now_iso


ORGNR_KEYS = ("organisation_number", "organization_number", "organisasjonsnummer", "orgnr", "org_nr", "orgNumber")


def _orgnrs_from_json(value):
    """Organisation numbers from a parsed JSON value: only the named org-number fields are read, never names or
    other numbers (a company called 'RES-IMMO-896469312 NUF' must not add a second company)."""
    if isinstance(value, list):
        out = []
        for v in value:
            out += _orgnrs_from_json(v)
        return out
    if isinstance(value, dict):
        for k in ORGNR_KEYS:
            if value.get(k) not in (None, ""):
                m = ORGNR_RE.search(str(value[k]))
                return ["".join(m.groups())] if m else []
        for k in ("organisations", "organizations", "companies", "items", "batch"):
            if isinstance(value.get(k), list):
                return _orgnrs_from_json(value[k])
        return []
    if isinstance(value, (str, int)):
        m = ORGNR_RE.search(str(value))
        return ["".join(m.groups())] if m else []
    return []


def _orgnrs_from_text(text):
    stripped = text.strip()
    if stripped[:1] in "[{":
        try:                                   # a JSON document (list or object)
            return _orgnrs_from_json(json.loads(stripped))
        except ValueError:
            pass
        out = []                               # JSON lines (Builderr's batch selector writes one object per line)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out += _orgnrs_from_json(json.loads(line))
            except ValueError:
                m = ORGNR_RE.search(line)
                if m:
                    out.append("".join(m.groups()))
        return out
    return ["".join(m.groups()) for m in ORGNR_RE.finditer(text)]


def read_orgnrs(paths_or_values):
    seen, out = set(), []
    for item in paths_or_values:
        text = item
        if os.path.isfile(item):
            with open(item, encoding="utf-8-sig") as fh:
                text = fh.read()
        for n in _orgnrs_from_text(text):
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


STOPPED_REASON = ("the run was stopped before this company finished (time or resource budget reached); "
                  "no fact is published for it in this run")


class _Stream:
    """Writes one terminal envelope per company the moment it finishes (flushed to disk), so a run that is
    stopped from outside still leaves a readable envelopes file for every company it completed, and the
    finaliser can add a 'failed' envelope for every company that did not finish."""

    def __init__(self, out_dir, orgnrs, run_id, envelopes_path=None):
        self.out_dir, self.orgnrs, self.run_id = out_dir, list(orgnrs), run_id
        self.final_path = os.path.join(out_dir, "envelopes.jsonl")
        self.extra_path = envelopes_path
        self.partial = os.path.join(out_dir, "envelopes.partial.jsonl")
        self.lock = threading.RLock()
        self.done = {}                       # orgnr -> lean envelope dict
        self.finalised = False
        self.fh = open(self.partial, "w", encoding="utf-8")
        # A complete envelopes file (finished companies + 'failed' placeholders) is also rewritten about ten
        # times during the run, so even a hard kill that cannot be caught leaves a readable, complete file.
        self.checkpoint_every = max(25, len(self.orgnrs) // 10)
        self._placeholders = {}
        self._write_full(final=False)

    def add(self, prof):
        env = lean_envelope(prof)
        with self.lock:
            if self.finalised:
                return
            self.done[prof.get("organisation_number")] = env
            self.fh.write(json.dumps(env, ensure_ascii=False) + "\n")
            self.fh.flush()
            if len(self.done) % self.checkpoint_every == 0 and len(self.done) < len(self.orgnrs):
                self._write_full(final=False)

    def _envelopes(self, reason=STOPPED_REASON):
        envs = []
        for n in self.orgnrs:
            env = self.done.get(n)
            if env is None:
                env = self._placeholders.get((n, reason))
                if env is None:
                    env = lean_envelope(failed_profile(n, "lookup_incomplete", reason, self.run_id))
                    self._placeholders[(n, reason)] = env
            envs.append(env)
        return envs

    def _write_full(self, final, reason=STOPPED_REASON):
        envs = self._envelopes(reason)
        _atomic_jsonl(self.final_path, envs)
        if self.extra_path and os.path.abspath(self.extra_path) != os.path.abspath(self.final_path):
            os.makedirs(os.path.dirname(os.path.abspath(self.extra_path)), exist_ok=True)
            _atomic_jsonl(self.extra_path, envs)
        return envs

    def finalise(self, reason=STOPPED_REASON):
        """Exactly one envelope per input, in input order; companies that did not finish get a failed envelope."""
        with self.lock:
            if self.finalised:
                return
            self.finalised = True
            try:
                self.fh.close()
            except Exception:
                pass
            envs = self._write_full(final=True, reason=reason)
            try:
                os.remove(self.partial)
            except OSError:
                pass
            return envs


def _atomic_jsonl(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _install_stop_handlers(stream, on_stop):
    """SIGTERM / SIGINT / SIGHUP (what an evaluator sends when a time budget runs out): write every envelope
    and exit cleanly instead of dying with no output."""
    previous = {}
    if threading.current_thread() is not threading.main_thread():
        return previous
    def handler(signum, _frame):
        try:
            if not stream.finalised:          # envelopes already final: only optional extras were being written
                stream.finalise()
                on_stop(signum)
        finally:
            os._exit(0)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                previous[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
    return previous


def _restore_handlers(previous):
    for sig, h in (previous or {}).items():
        try:
            signal.signal(sig, h)
        except (ValueError, OSError, TypeError):
            pass


def run_batch(http, orgnrs, out_dir, workers=8, use_web=True, use_wikidata=True, use_news=True, use_bing=False,
              progress=True, compact=False, keep_others=False, deadline=None, envelopes_path=None,
              report_path=None, write_extras=True):
    os.makedirs(out_dir, exist_ok=True)
    stream = _Stream(out_dir, orgnrs, getattr(http, "run_id", None), envelopes_path)
    t_start = time.time()

    def stop_report(signum):
        _write_run_report(out_dir, report_path, {
            "finished_at": now_iso(), "stopped_by_signal": signum, "expected_count": len(orgnrs),
            "emitted_envelopes": len(orgnrs), "completed_companies": len(stream.done),
            "outbound_requests": http.used, "seconds": round(time.time() - t_start, 1),
            "run_id": getattr(http, "run_id", None), "external_api_cost_usd": 0.0})
    previous_handlers = _install_stop_handlers(stream, stop_report)
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
    timed_out = False
    ex = ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(one, n): n for n in orgnrs}
    try:
        i = 0
        for f in as_completed(futs, timeout=(max(1.0, deadline - time.time()) if deadline else None)):
            i += 1
            http.reserve = CORE * max(0, len(orgnrs) - i)
            n = futs[f]
            try:
                n, prof = f.result()
            except BudgetExceeded as e:
                prof = failed_profile(n, "lookup_incomplete", str(e), getattr(http, "run_id", None))
            except Exception as e:  # one bad company must never sink the run
                prof = failed_profile(n, "error", f"{type(e).__name__}: {e}", getattr(http, "run_id", None))
            if "claims" not in prof:
                from . import contract
                prof.update(contract.build(prof, run={"run_id": getattr(http, "run_id", None)}))
            results[n] = prof
            stream.add(prof)
            if progress:
                print(f"[{i}/{len(orgnrs)}] {n} {prof.get('status')} – {prof.get('name') or ''} "
                      f"({len(prof.get('facts', []))} facts) | requests used {http.used}", flush=True)
    except FuturesTimeout:
        # The run's own time budget is used up: companies still in progress are reported as failed (never dropped)
        timed_out = True
        http.deadline = time.time() - 1           # no new requests from the threads still running
        for f, n in futs.items():
            f.cancel()
            if n not in results:
                results[n] = failed_profile(n, "lookup_incomplete", STOPPED_REASON, getattr(http, "run_id", None))
        if progress:
            print(f"Time budget reached: {len(stream.done)} of {len(orgnrs)} companies finished; "
                  f"the rest are reported as failed.", flush=True)
    finally:
        ex.shutdown(wait=not timed_out, cancel_futures=True)

    # 1. The envelopes first: they are what is scored, so they are written before anything optional.
    stream.finalise()
    ordered = [results[n] for n in orgnrs if n in results]
    if keep_others:          # a partial refresh: keep every stored profile that was not re-checked in this run
        ordered += [p for n, p in bundled.items() if n not in results]
    from .profile import _clean_history
    for p in ordered:
        if p.get("history"):
            p["history"] = _clean_history(p["history"])
    for p in ordered:                      # profiles restored from an earlier run still carry the contract view
        if "claims" not in p:
            from . import contract
            p.update(contract.build(p, run={"run_id": getattr(http, "run_id", None)}))
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
        "run_id": getattr(http, "run_id", None),
        "expected_count": len(orgnrs),
        "emitted_envelopes": len(ordered),
        "validation": validate_envelopes(ordered, orgnrs),
        "operations": dict(_operations(ordered), requests=http.used),
        "note": "All sources are free public APIs/pages; no paid API or LLM is called.",
        "time_budget_reached": timed_out,
        "completed_companies": sum(1 for p in ordered if (p.get("run") or {}).get("terminal_status") == "completed"),
    }
    # 2. The run report, then 3. the optional extras (profiles, index, report pages): a failure there can never
    # touch the envelopes that are already on disk.
    _write_run_report(out_dir, report_path, report)
    if write_extras and not timed_out:
        try:
            write_outputs(out_dir, ordered, compact=compact)
        except Exception as e:
            print(f"warning: profiles/index not written: {type(e).__name__}: {e}", file=sys.stderr)
        try:
            write_reports(out_dir, ordered, now_iso(), compact=compact)
        except Exception as e:
            print(f"warning: report pages not written: {type(e).__name__}: {e}", file=sys.stderr)
    elif write_extras:
        try:                                   # out of time: profiles only, no report pages
            write_outputs(out_dir, ordered, compact=True)
        except Exception as e:
            print(f"warning: profiles/index not written: {type(e).__name__}: {e}", file=sys.stderr)
    _restore_handlers(previous_handlers)
    return ordered, report


def _write_run_report(out_dir, report_path, report):
    for path in [os.path.join(out_dir, "run_report.json")] + ([report_path] if report_path else []):
        try:
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            print(f"warning: run report not written to {path}: {type(e).__name__}: {e}", file=sys.stderr)


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


TERMINAL = ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed")


def failed_profile(n, status, reason, run_id=None):
    """A terminal envelope for a company whose research broke: it is still returned, marked failed."""
    from . import contract
    from .profile import SECTION_TITLES
    prof = {"organisation_number": n, "status": status, "summary": reason, "facts": [], "generated_at": now_iso(),
            "complete": False,
            "envelope": {"states": list(TERMINAL), "generated_at": now_iso(),
                         "sections": [{"section": k, "title": t, "state": "failed", "fields": [], "reason": reason}
                                      for k, t in SECTION_TITLES]}}
    prof.update(contract.build(prof, run={"run_id": run_id, "completed_at": now_iso()}))
    prof["errors"].append({"source": "agent", "status": status, "error": reason})
    return prof


def validate_envelopes(profiles, orgnrs):
    got = [p.get("organisation_number") for p in profiles]
    states_ok = all(c.get("availability") in TERMINAL for p in profiles for c in p.get("claims", []))
    sections_ok = all(s.get("state") in TERMINAL for p in profiles for s in ((p.get("envelope") or {}).get("sections") or []))
    checks = {"exact_expected_count": len(set(got) & set(orgnrs)) == len(set(orgnrs)),
              "unique_organisation_numbers": len(got) == len(set(got)),
              "all_claim_states_terminal": states_ok, "all_section_states_terminal": sections_ok,
              "zero_silent_drops": set(orgnrs) <= set(got)}
    return {"passed": all(checks.values()), "checks": checks}


def _operations(profiles):
    ms = sorted(int((p.get("operations") or {}).get("runtime_ms") or 0) for p in profiles)
    req = [int((p.get("operations") or {}).get("requests") or 0) for p in profiles]
    return {"requests": sum(req), "p50_ms": ms[len(ms) // 2] if ms else None,
            "p95_ms": ms[min(len(ms) - 1, int(len(ms) * 0.95))] if ms else None, "third_party_cost_usd": 0}


ENVELOPE_KEYS = ("organisation_number", "contract_version", "run", "status", "name", "claims", "evidence",
                 "synthesis", "changes", "errors", "operations")


CLAIM_KEYS = ("field", "family", "value", "availability", "confidence", "evidence_ids", "effective_date",
              "reporting_period", "source_class", "reason")
EVIDENCE_KEYS = ("id", "source_url", "source_class", "evidence_page", "retrieved_at", "content_sha256", "claim_span",
                 "span_status")


def lean_envelope(p):
    """One terminal envelope in the starter-kit output contract, without display-only fields (labels, long source
    names, repeated descriptions): about half the size, so a 1,000-company batch stays easy to read and score.
    The full profile (with every display field) is kept in profiles.jsonl."""
    env = {k: p.get(k) for k in ENVELOPE_KEYS}
    env["claims"] = [dict({k: c[k] for k in CLAIM_KEYS if c.get(k) not in (None, "")},
                          evidence_ids=c.get("evidence_ids") or []) for c in p.get("claims") or []]
    env["evidence"] = []
    for ev in p.get("evidence") or []:
        e = {k: ev[k] for k in EVIDENCE_KEYS if ev.get(k) not in (None, "")}
        if e.get("evidence_page") == e.get("source_url"):
            e.pop("evidence_page", None)
        env["evidence"].append(e)
    env["state"] = "complete" if (p.get("run") or {}).get("terminal_status") == "completed" else "failed"
    env["sections"] = (p.get("envelope") or {}).get("sections")
    return env


def write_envelopes(out_dir, profiles, extra_path=None):
    """envelopes.jsonl: exactly one terminal envelope per input, in the starter-kit output contract."""
    envs = [lean_envelope(p) for p in profiles]
    _atomic_jsonl(os.path.join(out_dir, "envelopes.jsonl"), envs)
    if extra_path:
        os.makedirs(os.path.dirname(os.path.abspath(extra_path)), exist_ok=True)
        _atomic_jsonl(extra_path, envs)


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
            "revenue", "net_income", "currency", "accounts_period", "ceo", "website", "lei", "news_count", "latest_news_date", "latest_news_title", "fact_count", "verdict", "confidence", "conclusion", "summary"]
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
                ((p.get("confidence") or {}).get("overall") or "") + ("; can't tell: " + ", ".join(
                    (p.get("confidence") or {}).get("cant_tell") or []).lower() if (p.get("confidence") or {}).get("cant_tell") else ""),
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
