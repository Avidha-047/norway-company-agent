"""Version 9 tests: evaluator-safe output.

- the starter kit's command-line flags work (--organisations / --output / --profiles-output / --report)
- JSONL batches are read by the organisation_number field only
- every input gets an envelope even when the time budget runs out or the evaluator stops the run (SIGTERM)
- envelopes are lean and keep the contract fields

Run:  python -m unittest tests.test_v9 -v
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from nca import runner as R  # noqa: E402
from tests.test_offline import ORG, FakeHttp, routes  # noqa: E402

CONTRACT_KEYS = {"organisation_number", "run", "claims", "evidence", "changes", "errors", "operations"}


def _batch_file(d, orgs, name="batch.jsonl"):
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as fh:
        for n in orgs:
            fh.write(json.dumps({"organisation_number": n, "name": f"TEST {n} 123456789 AS", "employees": 123456789}) + "\n")
    return path


def _read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class ReadBatch(unittest.TestCase):
    def test_jsonl_uses_the_org_number_field_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.jsonl")
            with open(p, "w") as fh:
                fh.write('{"organisation_number":"926284991","name":"RES-IMMO-896469312 NUF","employees":null}\n')
                fh.write('{"organisation_number":"923609016","name":"EQUINOR ASA","municipality_number":"1103"}\n')
            self.assertEqual(R.read_orgnrs([p]), ["926284991", "923609016"])

    def test_json_list_and_plain_text_still_work(self):
        self.assertEqual(R.read_orgnrs(['[{"organisation_number": "923 609 016"}, "984851006"]']),
                         ["923609016", "984851006"])
        self.assertEqual(R.read_orgnrs(["923609016\nNO 984 851 006 MVA\n"]), ["923609016", "984851006"])


class LeanEnvelope(unittest.TestCase):
    def test_contract_fields_kept_and_smaller(self):
        with tempfile.TemporaryDirectory() as d:
            profs, rep = R.run_batch(FakeHttp(routes()), [ORG], d, workers=1, progress=False)
            full = profs[0]
            env = _read_jsonl(os.path.join(d, "envelopes.jsonl"))[0]
            self.assertTrue(CONTRACT_KEYS <= set(env))
            self.assertEqual(env["state"], "complete")
            self.assertTrue(env["claims"] and env["evidence"])
            ids = {e["id"] for e in env["evidence"]}
            for c in env["claims"]:
                self.assertIn(c["availability"], R.TERMINAL)
                self.assertTrue(set(c["evidence_ids"]) <= ids)
            for e in env["evidence"]:
                self.assertTrue(e.get("source_url") and e.get("retrieved_at"))
            full_size = len(json.dumps({k: full.get(k) for k in ("claims", "evidence")}, ensure_ascii=False))
            lean_size = len(json.dumps({k: env.get(k) for k in ("claims", "evidence")}, ensure_ascii=False))
            self.assertLess(lean_size, full_size * 0.8)
            self.assertTrue(rep["validation"]["passed"])
            self.assertFalse(os.path.exists(os.path.join(d, "envelopes.partial.jsonl")))


class HardKill(unittest.TestCase):
    def test_complete_file_exists_from_the_start(self):
        with tempfile.TemporaryDirectory() as d:
            st = R._Stream(d, [ORG, "984851006"], "r1")
            envs = _read_jsonl(os.path.join(d, "envelopes.jsonl"))
            self.assertEqual([e["state"] for e in envs], ["failed", "failed"])
            st.finalise()


class TimeBudget(unittest.TestCase):
    def test_every_input_gets_an_envelope_when_time_runs_out(self):
        real = R.build_profile
        orgs = [ORG, "984851006", "975820440", "982826233"]

        def slow(http, n, **kw):
            if n != ORG:
                time.sleep(4)
            return real(http, n, **kw)
        R.build_profile = slow
        try:
            with tempfile.TemporaryDirectory() as d:
                t0 = time.time()
                profs, rep = R.run_batch(FakeHttp(routes()), orgs, d, workers=4, progress=False,
                                         deadline=time.time() + 1.5)
                self.assertLess(time.time() - t0, 3.5)
                envs = _read_jsonl(os.path.join(d, "envelopes.jsonl"))
                self.assertEqual([e["organisation_number"] for e in envs], orgs)
                self.assertEqual(envs[0]["state"], "complete")
                for e in envs[1:]:
                    self.assertEqual(e["state"], "failed")
                    self.assertTrue(e["errors"])
                self.assertTrue(rep["time_budget_reached"])
                self.assertTrue(rep["validation"]["passed"])
        finally:
            R.build_profile = real


SIGTERM_SCRIPT = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {here!r})
    from nca import runner as R
    from tests.test_offline import ORG, FakeHttp, routes
    real = R.build_profile
    def slow(http, n, **kw):
        if n != ORG:
            time.sleep(30)
        return real(http, n, **kw)
    R.build_profile = slow
    R.run_batch(FakeHttp(routes()), [ORG, "984851006", "975820440"], {out!r}, workers=3, progress=True,
                envelopes_path={env!r})
""")


@unittest.skipUnless(hasattr(signal, "SIGTERM") and os.name == "posix", "POSIX signals")
class StopSignal(unittest.TestCase):
    def test_sigterm_leaves_one_envelope_per_input(self):
        with tempfile.TemporaryDirectory() as d:
            out, env = os.path.join(d, "out"), os.path.join(d, "result", "envelopes.jsonl")
            script = os.path.join(d, "run.py")
            with open(script, "w") as fh:
                fh.write(SIGTERM_SCRIPT.format(here=HERE, out=out, env=env))
            proc = subprocess.Popen([sys.executable, script], cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            deadline = time.time() + 20
            while time.time() < deadline:
                line = proc.stdout.readline().decode()
                if line.startswith("[1/3]"):
                    break
            proc.send_signal(signal.SIGTERM)
            code = proc.wait(timeout=20)
            proc.stdout.close()
            self.assertEqual(code, 0)
            for path in (env, os.path.join(out, "envelopes.jsonl")):
                envs = _read_jsonl(path)
                self.assertEqual([e["organisation_number"] for e in envs], [ORG, "984851006", "975820440"])
                self.assertEqual([e["state"] for e in envs], ["complete", "failed", "failed"])
            with open(os.path.join(out, "run_report.json")) as fh:
                rep = json.load(fh)
            self.assertEqual(rep["emitted_envelopes"], 3)


class StarterKitCommand(unittest.TestCase):
    def test_evaluator_flags(self):
        import agent
        from tests import test_offline as T
        real = R.build_profile

        def fake(http, n, **kw):
            h = T.FakeHttp(T.routes())
            h.run_id = getattr(http, "run_id", None)
            return real(h, n, **kw)
        R.build_profile = fake
        try:
            with tempfile.TemporaryDirectory() as d:
                batch = _batch_file(d, [ORG, "984851006"])
                out = os.path.join(d, "out")
                code = agent.main(["--organisations", batch, "--bulk", "brreg-enheter.csv",
                                   "--output", os.path.join(out, "envelopes.jsonl"),
                                   "--profiles-output", os.path.join(out, "profiles.jsonl"),
                                   "--report", os.path.join(out, "run-report.json"),
                                   "--run-id", "eval-001", "--expected-count", "2", "--time-limit", "120"])
                self.assertEqual(code, 0)
                envs = _read_jsonl(os.path.join(out, "envelopes.jsonl"))
                self.assertEqual([e["organisation_number"] for e in envs], [ORG, "984851006"])
                self.assertEqual({e["run"]["run_id"] for e in envs}, {"eval-001"})
                self.assertEqual(len(_read_jsonl(os.path.join(out, "profiles.jsonl"))), 2)
                with open(os.path.join(out, "run-report.json")) as fh:
                    rep = json.load(fh)
                self.assertEqual((rep["expected_count"], rep["emitted_envelopes"]), (2, 2))
                self.assertTrue(rep["validation"]["passed"])
        finally:
            R.build_profile = real


if __name__ == "__main__":
    unittest.main()
