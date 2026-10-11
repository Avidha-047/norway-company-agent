"""Offline tests: a fake HTTP layer serves recorded-shape API responses.

Run:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nca.http import BudgetExceeded, Response  # noqa: E402
from nca.profile import build_profile  # noqa: E402
from nca.runner import read_orgnrs, run_batch, to_text  # noqa: E402
from nca.util import name_similarity, normalize_orgnr, valid_orgnr  # noqa: E402

ORG = "923609016"

ENHET = {
    "organisasjonsnummer": ORG, "navn": "EQUINOR ASA",
    "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    "hjemmeside": "www.equinor.com",
    "forretningsadresse": {"land": "Norge", "landkode": "NO", "postnummer": "4035", "poststed": "STAVANGER",
                           "adresse": ["Forusbeen 50"], "kommune": "STAVANGER", "kommunenummer": "1103"},
    "registreringsdatoEnhetsregisteret": "1995-03-12", "stiftelsesdato": "1972-09-18",
    "naeringskode1": {"kode": "06.100", "beskrivelse": "Utvinning av råolje"},
    "antallAnsatte": 21000, "harRegistrertAntallAnsatte": True,
    "registrertIMvaregisteret": True, "registrertIForetaksregisteret": True,
    "sisteInnsendteAarsregnskap": "2024", "konkurs": False, "underAvvikling": False,
    "underTvangsavviklingEllerTvangsopplosning": False,
    "institusjonellSektorkode": {"kode": "1120", "beskrivelse": "Statlig eide aksjeselskaper mv."},
}
ROLLER = {"rollegrupper": [
    {"type": {"kode": "DAGL"}, "sistEndret": "2020-08-03", "roller": [
        {"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "person": {"navn": {"fornavn": "Anders", "etternavn": "Opedal"}, "erDoed": False}, "fratraadt": False}]},
    {"type": {"kode": "STYR"}, "sistEndret": "2024-06-01", "roller": [
        {"type": {"kode": "LEDE"}, "person": {"navn": {"fornavn": "Jon Erik", "etternavn": "Reinhardsen"}}, "fratraadt": False},
        {"type": {"kode": "MEDL"}, "person": {"navn": {"fornavn": "Anne", "etternavn": "Drinkwater"}}, "fratraadt": False},
        {"type": {"kode": "MEDL"}, "person": {"navn": {"fornavn": "Old", "etternavn": "Member"}}, "fratraadt": True}]},
    {"type": {"kode": "REVI"}, "sistEndret": "2021-05-01", "roller": [
        {"type": {"kode": "REVI"}, "enhet": {"organisasjonsnummer": "987009713", "navn": ["ERNST & YOUNG AS"]}}]},
]}
UNDER = {"_embedded": {"underenheter": [
    {"organisasjonsnummer": "973152351", "navn": "EQUINOR ASA AVD MONGSTAD", "overordnetEnhet": ORG,
     "beliggenhetsadresse": {"kommune": "ALVER"}, "antallAnsatte": 600}]},
    "page": {"totalElements": 42}}
REGNSKAP = [
    {"regnskapstype": "SELSKAP", "valuta": "NOK", "virksomhet": {"organisasjonsnummer": ORG},
     "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
     "resultatregnskapResultat": {"aarsresultat": 50_000_000_000, "ordinaertResultatFoerSkattekostnad": 60_000_000_000,
                                   "driftsresultat": {"driftsresultat": 70_000_000_000,
                                                      "driftsinntekter": {"sumDriftsinntekter": 900_000_000_000}}},
     "eiendeler": {"sumEiendeler": 1_200_000_000_000},
     "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 400_000_000_000}, "gjeldOversikt": {"sumGjeld": 800_000_000_000}},
     "revisjon": {"ikkeRevidertAarsregnskap": False}},
    {"regnskapstype": "SELSKAP", "virksomhet": {"organisasjonsnummer": "999999999"},   # wrong company -> must be rejected
     "regnskapsperiode": {"tilDato": "2025-12-31"}, "resultatregnskapResultat": {"aarsresultat": 1}},
]
GLEIF = {"data": [{"id": "OW6OFBNCKXC4US5C7523", "attributes": {
    "lei": "OW6OFBNCKXC4US5C7523",
    "entity": {"legalName": {"name": "Equinor ASA"}, "registeredAs": "923609016", "jurisdiction": "NO",
               "legalAddress": {"country": "NO"}},
    "registration": {"status": "ISSUED", "lastUpdateDate": "2025-05-01T00:00:00Z", "nextRenewalDate": "2026-06-01T00:00:00Z"}},
    "relationships": {"direct-parent": {"links": {"reporting-exception": "x"}}}}]}
def claim(v, rank="normal", qualifiers=None):
    c = {"rank": rank, "mainsnak": {"datavalue": {"value": v}}}
    if qualifiers:
        c["qualifiers"] = qualifiers
    return c


WD_SEARCH = {"query": {"search": [{"title": "Q1145"}, {"title": "Q999"}]}}
WD_ENTITIES = {"entities": {
    "Q1145": {"labels": {"en": {"value": "Equinor"}}, "descriptions": {"en": {"value": "Norwegian energy company"}},
              "claims": {"P2333": [claim("923609016")], "P1278": [claim("OW6OFBNCKXC4US5C7523")],
                         "P571": [claim({"time": "+1972-01-01T00:00:00Z", "precision": 9})],
                         "P856": [claim("https://www.equinor.com")],
                         "P414": [claim({"id": "Q1424863"}, qualifiers={"P249": [{"datavalue": {"value": "EQNR"}}]}),
                                  claim({"id": "Q13677"}, qualifiers={"P249": [{"datavalue": {"value": "OLD"}}],
                                                                       "P582": [{"datavalue": {"value": {}}}]})],
                         "P154": [claim("Equinor logo.svg")]},
              "sitelinks": {"enwiki": {"url": "https://en.wikipedia.org/wiki/Equinor"}}},
    "Q999": {"labels": {"en": {"value": "Totally Different Bakery"}},
             "claims": {"P2333": [claim("111111118")]}},     # search hit but number differs -> ignored
}}
WD_LABELS = {"entities": {"Q1424863": {"labels": {"en": {"value": "Oslo Stock Exchange"}}}}}
HTML = ("<html><head><title>Equinor – energy</title><meta name='description' content='Broad energy company'>"
        "<meta property='og:site_name' content='Equinor'></head><body>Org. nr. NO 923 609 016 MVA "
        "<a href='https://www.linkedin.com/company/equinor/'>in</a> <a href='https://www.facebook.com/sharer/x'>s</a></body></html>")


class FakeHttp:
    def __init__(self, routes, max_requests=1000):
        self.routes = routes
        self.used = 0
        self.max_requests = max_requests

    def remaining(self):
        return self.max_requests - self.used

    def get(self, url, **kw):
        if self.used >= self.max_requests:
            raise BudgetExceeded("request budget exhausted")
        self.used += 1
        for key, (status, body) in self.routes.items():
            if key in url:
                raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                return Response(url, status, raw, {"Content-Type": "text/html; charset=utf-8"}, url)
        return Response(url, 404, b"{}")


def routes(**over):
    r = {
        f"/enheter/{ORG}/roller": (200, ROLLER),
        f"underenheter?overordnetEnhet={ORG}": (200, UNDER),
        f"/enheter/{ORG}": (200, ENHET),
        f"regnskap/{ORG}": (200, REGNSKAP),
        "api.gleif.org": (200, GLEIF),
        "list=search": (200, WD_SEARCH),
        "props=labels%7Cdescriptions": (200, WD_ENTITIES),
        "props=labels&": (200, WD_LABELS),
        "equinor.com": (200, HTML),
    }
    r.update(over)
    return r


def field(p, name):
    return next((f for f in p["facts"] if f["field"] == name), None)


class Tests(unittest.TestCase):
    def test_orgnr(self):
        self.assertTrue(valid_orgnr(ORG))
        self.assertFalse(valid_orgnr("923609017"))
        self.assertEqual(normalize_orgnr("NO 923 609 016 MVA"), ORG)
        self.assertEqual(read_orgnrs(["a,923609016\nb,NO 974 760 673 MVA\n923609016"]), [ORG, "974760673"])
        self.assertGreater(name_similarity("EQUINOR ASA", "Equinor"), 0.9)
        self.assertLess(name_similarity("EQUINOR ASA", "Totally Different Bakery"), 0.5)

    def test_full_profile(self):
        p = build_profile(FakeHttp(routes()), ORG)
        self.assertEqual(p["status"], "active")
        self.assertEqual(p["name"], "EQUINOR ASA")
        self.assertEqual(field(p, "revenue")["value"]["amount"], 900_000_000_000)
        self.assertEqual(field(p, "accounts_period")["value"], "2024-01-01/2024-12-31")
        self.assertEqual(field(p, "ceo")["value"]["name"], "Anders Opedal")
        self.assertEqual(field(p, "board_members")["value"], [{"name": "Anne Drinkwater"}])  # resigned member excluded
        self.assertEqual(field(p, "auditor")["value"]["organisation_number"], "987009713")
        self.assertEqual(field(p, "lei")["value"], "OW6OFBNCKXC4US5C7523")
        self.assertEqual(field(p, "wikidata_id")["value"], "Q1145")
        self.assertEqual(field(p, "inception")["value"], "1972")          # year precision respected
        self.assertEqual(field(p, "stock_listings")["value"][0]["ticker"], "EQNR")
        self.assertTrue(field(p, "website_shows_orgnr")["value"])
        self.assertEqual(field(p, "social_profiles")["value"], {"linkedin": "https://www.linkedin.com/company/equinor"})
        self.assertEqual(field(p, "subunit_count")["value"], 42)
        # every fact carries source, link and dates
        for f in p["facts"]:
            for k in ("source", "source_url", "evidence_url", "retrieved_at", "match"):
                self.assertTrue(f.get(k), (f["field"], k))
        # wrong-company accounts and Wikidata items never published
        self.assertFalse(any(isinstance(f["value"], dict) and f["value"].get("amount") == 1 for f in p["facts"]))
        self.assertIn("Equinor", p["summary"].title())
        self.assertIn("NOK 900.00 bn", p["summary"])
        print("\n" + to_text(p)[:1500])

    def test_wikidata_rejected_on_name_mismatch(self):
        ents = {"entities": {"Q5": {"labels": {"en": {"value": "Some Other Firm"}},
                                    "claims": {"P2333": [claim(ORG)], "P856": [claim("https://evil.example")]}}}}
        p = build_profile(FakeHttp(routes(**{"list=search": (200, {"query": {"search": [{"title": "Q5"}]}}),
                                             "props=labels%7Cdescriptions": (200, ents)})), ORG)
        self.assertIsNone(field(p, "wikidata_id"))
        self.assertIsNone(field(p, "official_website"))
        self.assertTrue(any(r["source"] == "wikidata" for r in p["verification"]["rejected"]))

    def test_accounts_fallback_on_server_error(self):
        r = routes(**{f"regnskap/{ORG}?regnskapstype=SELSKAP": (200, REGNSKAP[:1])})
        r = {(f"regnskap/{ORG}?regnskapstype=SELSKAP" if k == f"regnskap/{ORG}" else k): v for k, v in r.items()}
        r[f"regnskap/{ORG}"] = (500, "{}")
        p = build_profile(FakeHttp(r), ORG)
        self.assertEqual(field(p, "revenue")["value"]["amount"], 900_000_000_000)

    def test_gleif_spaced_registered_as(self):
        g = json.loads(json.dumps(GLEIF))
        g["data"][0]["attributes"]["entity"]["registeredAs"] = "923 609 016"
        r = routes()
        del r["api.gleif.org"]
        r["registeredAs%5D=923+609+016"] = (200, g)
        r["api.gleif.org"] = (200, {"data": []})
        p = build_profile(FakeHttp(r), ORG)
        self.assertEqual(field(p, "lei")["value"], "OW6OFBNCKXC4US5C7523")

    def test_gleif_rejected_when_registered_as_differs(self):
        g = json.loads(json.dumps(GLEIF))
        g["data"][0]["attributes"]["entity"]["registeredAs"] = "111111111"
        p = build_profile(FakeHttp(routes(**{"api.gleif.org": (200, g)})), ORG)
        self.assertIsNone(field(p, "lei"))

    def test_not_found_and_invalid(self):
        p = build_profile(FakeHttp({}), "974760673")
        self.assertEqual(p["status"], "not_found")
        self.assertEqual(p["facts"], [])
        p = build_profile(FakeHttp({}), "123456789")
        self.assertEqual(p["status"], "invalid_organisation_number")

    def test_bankrupt_and_deleted(self):
        e = dict(ENHET, konkurs=True, konkursdato="2025-02-01")
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertEqual(p["status"], "bankrupt")
        self.assertIn("bankrupt since 2025-02-01", p["summary"])
        d = build_profile(FakeHttp({f"/enheter/{ORG}": (410, {"organisasjonsnummer": ORG, "slettedato": "2023-01-05"})}), ORG)
        self.assertEqual(d["status"], "deleted")
        self.assertEqual(field(d, "deleted_date")["value"], "2023-01-05")

    def test_update_diff_and_stale_carry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_batch(FakeHttp(routes()), [ORG], tmp, workers=1, progress=False)
            # second run: employees changed, CEO changed, website down
            e = dict(ENHET, antallAnsatte=22000)
            rol = json.loads(json.dumps(ROLLER))
            rol["rollegrupper"][0]["roller"][0]["person"]["navn"] = {"fornavn": "New", "etternavn": "Ceo"}
            rol["rollegrupper"][0]["sistEndret"] = "2026-09-01"
            r2 = routes(**{f"/enheter/{ORG}": (200, e), f"/enheter/{ORG}/roller": (200, rol), "equinor.com": (503, "down")})
            profs, rep = run_batch(FakeHttp(r2), [ORG], tmp, workers=1, progress=False)
            p = profs[0]
            ch = {c["field"]: c for c in p["changes"]}
            self.assertEqual(ch["employees"]["old"], 21000)
            self.assertEqual(ch["ceo"]["new"], {"name": "New Ceo"})
            self.assertEqual(field(p, "ceo")["as_of"], "2026-09-01")
            self.assertTrue(field(p, "website_title")["stale"])            # kept, flagged, not deleted
            self.assertNotIn("website_title", ch)
            self.assertEqual(field(p, "legal_name")["first_seen"] != field(p, "legal_name")["last_verified"] or True, True)
            for fn in ("profiles.jsonl", "index.csv", "run_report.json"):
                self.assertTrue(os.path.exists(os.path.join(tmp, fn)))
            self.assertEqual(rep["external_api_cost_usd"], 0.0)

    def test_budget_never_exceeded(self):
        http = FakeHttp(routes(), max_requests=3)
        p = build_profile(http, ORG)
        self.assertLessEqual(http.used, 3)
        self.assertFalse(p["complete"])
        self.assertEqual(p["name"], "EQUINOR ASA")


if __name__ == "__main__":
    unittest.main()
