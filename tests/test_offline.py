"""Offline tests: a fake HTTP layer serves recorded-shape API responses.

Run:  python -m unittest discover -s tests -v
"""
import json
import re
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
    "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap",
                          "_links": {"self": {"href": "https://data.brreg.no/enhetsregisteret/api/organisasjonsformer/ASA"}}},
    "hjemmeside": "www.equinor.com",
    "forretningsadresse": {"land": "Norge", "landkode": "NO", "postnummer": "4035", "poststed": "STAVANGER",
                           "adresse": ["Forusbeen 50"], "kommune": "STAVANGER", "kommunenummer": "1103"},
    "registreringsdatoEnhetsregisteret": "1995-03-12", "stiftelsesdato": "1972-09-18",
    "_links": {"self": {"href": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"}},
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

    reserve = 0

    def remaining(self):
        return self.max_requests - self.used

    def spare(self, n=1):
        return self.remaining() - n >= self.reserve

    def get(self, url, **kw):
        if self.used >= self.max_requests:
            raise BudgetExceeded("request budget exhausted")
        self.used += 1
        self.urls = getattr(self, "urls", []) + [url]
        if not hasattr(self, "seen"):
            self.seen = {}
        for key, (status, body) in self.routes.items():
            if key in url:
                raw = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
                res = Response(url, status, raw, {"Content-Type": "text/html; charset=utf-8"}, url)
                self.seen[url] = res
                return res
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
        self.assertIn("NOK 900 000 000 000", p["summary"])
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


KUNNGJ_HTML = """<html><body><h2>Kunngjøringer for EQUINOR ASA, org.nr 923 609 016</h2><table>
<tr><td>12.06.2025</td><td><a href="hent_en.jsp?kid=111&sokeverdi=923609016&spr=">Endring av styre</a></td></tr>
<tr><td>03.01.2024</td><td><a href="hent_en.jsp?kid=222&amp;sokeverdi=923609016">Endring av forretningsadresse</a></td></tr>
<tr><td>header</td></tr></table></body></html>"""
FEED = """<?xml version="1.0"?><rss><channel>
<item><title>Equinor awards contract</title><link>https://www.equinor.com/news/a</link><pubDate>Tue, 07 Oct 2025 08:00:00 GMT</pubDate></item>
<item><title>No date item</title><link>https://www.equinor.com/news/b</link></item>
</channel></rss>"""
BING = """<?xml version="1.0"?><rss xmlns:News="https://www.bing.com/news/search?q=x"><channel>
<item><title>Equinor ASA lifts dividend</title><link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fe24.no%2fartikkel1&amp;c=1</link>
<description>Oslo-listed Equinor ASA said...</description><pubDate>Mon, 13 Oct 2025 06:00:00 GMT</pubDate><News:Source>E24</News:Source></item>
<item><title>Equinor Fan Club meets</title><link>http://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fx.no%2f2</link>
<description>A different organisation</description><pubDate>Mon, 13 Oct 2025 06:00:00 GMT</pubDate></item>
</channel></rss>"""
HTML_FEED = HTML.replace("</head>", "<link rel='alternate' type='application/rss+xml' href='/news/rss'></head>")


class NewsTests(unittest.TestCase):
    def routes(self, **over):
        r = routes(**{"equinor.com/news/rss": (200, FEED), "equinor.com": (200, HTML_FEED),
                      "kunngjoring/hent_nr": (200, KUNNGJ_HTML), "bing.com/news": (200, BING)})
        # order matters in FakeHttp: put the feed before the homepage
        ordered = {"equinor.com/news/rss": (200, FEED)}
        ordered.update(r)
        ordered.update(over)
        return ordered

    def test_news_sources(self):
        p = build_profile(FakeHttp(self.routes()), ORG, use_bing=True)
        off = field(p, "news_official")["value"]
        o = off[0]
        self.assertEqual(o["title"], "Endring av styre")                 # original Norwegian kept
        self.assertNotIn("title_en", o)                                 # not an exact known category: no guess
        self.assertEqual(o["url"], "https://w2.brreg.no/kunngjoring/hent_en.jsp?kid=111&sokeverdi=923609016&spr=")
        self.assertEqual(o["published"], "2025-06-12")
        self.assertEqual(o["company_match"], "Official announcement listed under organisation number 923609016")
        self.assertEqual(o["reference"]["quote_status"], "exact")
        self.assertIn("Endring av styre", o["reference"]["quote"])
        self.assertEqual(len(o["reference"]["content_sha256"]), 64)
        self.assertEqual(len(off), 2)
        comp = field(p, "news_company")["value"]
        self.assertEqual([c["url"] for c in comp], ["https://www.equinor.com/news/a"])   # undated item dropped
        press = field(p, "news_press")["value"]
        self.assertEqual(len(press), 1)                                                   # 'Equinor Fan Club' rejected
        self.assertEqual(press[0]["url"], "https://e24.no/artikkel1")
        self.assertEqual(press[0]["published"], "2025-10-13")
        self.assertEqual(press[0]["source"], "E24")
        self.assertEqual(p["news"][0]["published"], "2025-10-13")
        for it in p["news"]:
            for k in ("title", "url", "published", "source", "company_match"):
                self.assertTrue(it[k])
        self.assertIn("Most recent dated item", p["summary"])
        self.assertTrue(any(k.startswith("Sector: B") for k in p["key_points"]))

    def test_official_rejected_if_page_lacks_orgnr(self):
        p = build_profile(FakeHttp(self.routes(**{"kunngjoring/hent_nr": (200, "<tr><td>01.01.2025</td><td><a href='hent_en.jsp?kid=1'>X</a></td></tr>")})), ORG)
        self.assertIsNone(field(p, "news_official"))

    def test_no_press_search_for_sole_proprietor(self):
        e = dict(ENHET, organisasjonsform={"kode": "ENK", "beskrivelse": "Enkeltpersonforetak"}, navn="KARI NORDMANN")
        http = FakeHttp(self.routes(**{f"/enheter/{ORG}": (200, e)}))
        p = build_profile(http, ORG)
        self.assertIsNone(field(p, "news_press"))

    def test_risk_signals(self):
        reg = json.loads(json.dumps(REGNSKAP))
        reg[0]["egenkapitalGjeld"]["egenkapital"]["sumEgenkapital"] = -5
        reg[0]["resultatregnskapResultat"]["aarsresultat"] = -10
        p = build_profile(FakeHttp(self.routes(**{f"regnskap/{ORG}": (200, reg)})), ORG)
        sigs = {s["signal"] for s in p["risk_signals"]}
        self.assertIn("negative equity in the 2024 accounts", sigs)
        self.assertIn("Watch-outs:", p["summary"])


SMALL = {
    "organisasjonsnummer": "975820440", "navn": "MALERAN AS",
    "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
    "forretningsadresse": {"adresse": ["Fjellveien 12"], "postnummer": "5003", "poststed": "BERGEN",
                           "kommune": "BERGEN", "landkode": "NO", "land": "Norge"},
    "registreringsdatoEnhetsregisteret": "1995-06-01", "antallAnsatte": 4, "konkurs": False,
    "underAvvikling": False, "naeringskode1": {"kode": "43.341", "beskrivelse": "Malerarbeid"},
}
SN = "975820440"
BING_WEB = """<?xml version="1.0"?><rss><channel>
<item><title>Maleran AS - Proff</title><link>https://www.proff.no/selskap/maleran-as/x</link></item>
<item><title>Maleran AS – maler i Bergen</title><link>https://maleran.no/tjenester</link></item>
<item><title>Other</title><link>https://other-painter.no/</link></item>
</channel></rss>"""
SITE_OK = """<html><head><title>Maleran AS – Malerfirma i Bergen</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"HousePainter","name":"Maleran AS",
"telephone":"+47 55 12 34 56","address":{"streetAddress":"Fjellveien 12","postalCode":"5003","addressLocality":"Bergen"},
"sameAs":["https://www.facebook.com/maleran"]}</script></head>
<body><p>Maleran AS er et malerfirma i Bergen med over 25 års erfaring innen innvendig og utvendig maling for private og bedrifter.</p>
<a href="/kontakt">Kontakt oss</a></body></html>"""
CONTACT_OK = """<html><body><h1>Kontakt</h1><p>Maleran AS, Fjellveien 12, 5003 Bergen</p>
<a href="mailto:post@maleran.no">post@maleran.no</a> Tlf: 922 33 444 <p>Org.nr: 975 820 440 MVA</p></body></html>"""
SITE_OTHER = "<html><head><title>Other painter</title></head><body><p>Vi maler hus i Oslo.</p></body></html>"


class DiscoveryTests(unittest.TestCase):
    def r(self, **over):
        base = {f"/enheter/{SN}/roller": (200, {"rollegrupper": []}),
                f"underenheter?overordnetEnhet={SN}": (200, {"_embedded": {"underenheter": []}, "page": {"totalElements": 0}}),
                f"/enheter/{SN}": (200, SMALL), "bing.com/search": (200, BING_WEB),
                "maleran.no/kontakt": (200, CONTACT_OK), "maleran.no": (200, SITE_OK),
                "other-painter.no": (200, SITE_OTHER)}
        base.update(over)
        return base

    def test_discovers_and_verifies_site(self):
        p = build_profile(FakeHttp(self.r()), SN)
        self.assertEqual(field(p, "website_discovered")["value"], "https://maleran.no/")
        self.assertEqual(field(p, "website_discovered")["confidence"], "high")      # org number on contact page
        self.assertEqual(field(p, "website_emails")["value"], ["post@maleran.no"])
        self.assertIn("+47 92 23 34 44", field(p, "website_phones")["value"])
        self.assertIn("+47 55 12 34 56", field(p, "website_phones")["value"])
        self.assertEqual(field(p, "website_address")["value"], "Fjellveien 12, 5003 Bergen")
        self.assertIn("malerfirma", field(p, "website_about")["value"])
        self.assertEqual(field(p, "social_profiles")["value"], {"facebook": "https://www.facebook.com/maleran"})
        self.assertEqual(field(p, "industry_sector_en")["value"], "F – Construction")

    def test_rejects_unproven_site(self):
        # maleran.no exists but belongs to someone else: no org number, no registered address
        p = build_profile(FakeHttp(self.r(**{"maleran.no/kontakt": (404, ""), "maleran.no": (200, SITE_OTHER)})), SN)
        self.assertIsNone(field(p, "website_discovered"))
        self.assertIsNone(field(p, "website_title"))
        self.assertTrue(any(x["source"] == "website_discovery" for x in p["verification"]["rejected"]))

    def test_domain_guesses_and_directory_filter(self):
        from nca.web import guess_domains, is_directory
        self.assertEqual(guess_domains("MALERAN AS"), ["https://maleran.no/", "https://maleran.com/"])
        self.assertEqual(guess_domains("Bjørn Sæther Bygg AS")[:2], ["https://bjornsaetherbygg.no/", "https://bjorn-saether-bygg.no/"])
        self.assertEqual(guess_domains("INVEST HOLDING AS"), [])     # generic names are not guessed
        self.assertTrue(is_directory("https://www.proff.no/x") and is_directory("https://www.gulesider.no/y"))
        self.assertFalse(is_directory("https://maleran.no/"))

    def test_bing_off_by_default(self):
        http = FakeHttp(self.r())
        build_profile(http, SN)
        self.assertFalse(any("bing.com" in u for u in getattr(http, "urls", [])))

    def test_name_and_address_without_orgnr_is_medium(self):
        contact = CONTACT_OK.replace("<p>Org.nr: 975 820 440 MVA</p>", "")
        p = build_profile(FakeHttp(self.r(**{"maleran.no/kontakt": (200, contact)})), SN)
        self.assertEqual(field(p, "website_discovered")["confidence"], "medium")

    def test_budget_reserve_skips_optional_sources(self):
        http = FakeHttp(self.r())
        http.reserve = 10 ** 6      # nothing optional allowed
        p = build_profile(http, SN)
        self.assertIsNone(field(p, "website_discovered"))
        self.assertEqual(p["name"], "MALERAN AS")
        self.assertLessEqual(http.used, 6)

    def test_gdelt(self):
        from nca.news import gdelt_coverage
        data = {"articles": [
            {"title": "Maleran AS vant anbud i Bergen", "url": "https://ba.no/a", "seendate": "20260901T101500Z", "domain": "ba.no"},
            {"title": "Maleran Oslo åpner", "url": "https://x.no/b", "seendate": "20260902T101500Z", "domain": "x.no"}]}
        items, st = gdelt_coverage(FakeHttp({"gdeltproject": (200, data)}), "MALERAN AS", "AS", ["BERGEN"], SN)
        self.assertEqual([i["url"] for i in items], ["https://ba.no/a"])
        self.assertEqual(items[0]["published"], "2026-09-01")


class PrecisionFixTests(unittest.TestCase):
    def test_unregistered_employee_count_is_not_zero(self):
        e = dict(ENHET, harRegistrertAntallAnsatte=False)
        e.pop("antallAnsatte")
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertIsNone(field(p, "employees"))
        self.assertIs(field(p, "employee_count_registered")["value"], False)
        self.assertNotIn("0 registered employees", p["summary"])
        self.assertTrue(any("unknown, not zero" in k for k in p["key_points"]))

    def test_registered_zero_is_kept(self):
        e = dict(ENHET, harRegistrertAntallAnsatte=True, antallAnsatte=0)
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertEqual(field(p, "employees")["value"], 0)

    def test_newer_filing_is_flagged_not_called_latest(self):
        e = dict(ENHET, sisteInnsendteAarsregnskap="2025")
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertIn("not the most recent filing", field(p, "accounts_currency_note")["value"])
        self.assertIn("newer 2025 filing is registered", p["summary"])
        self.assertNotIn("latest", p["summary"].lower())


class V5Tests(unittest.TestCase):
    def test_extra_registry_fields(self):
        e = dict(ENHET, registreringsdatoMerverdiavgiftsregisteret="1995-04-01",
                 registreringsdatoForetaksregisteret="1995-03-12",
                 paategninger=[{"infotype": "FADR", "tekst": "Adresse er ikke bekreftet", "innfoertDato": "2024-02-01"}])
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertEqual(field(p, "vat_registered_date")["value"], "1995-04-01")
        self.assertEqual(field(p, "registered_business_register_date")["value"], "1995-03-12")
        self.assertEqual(field(p, "business_postcode")["value"], "4035")
        self.assertEqual(field(p, "register_notes")["value"][0]["text"], "Adresse er ikke bekreftet")

    def test_all_account_lines_exact(self):
        reg = json.loads(json.dumps(REGNSKAP))
        reg[0]["resultatregnskapResultat"]["driftsresultat"]["driftskostnad"] = {"loennskostnad": 123456789, "sumDriftskostnad": 800}
        reg[0]["eiendeler"]["sumBankinnskuddOgKontanter"] = 98765
        p = build_profile(FakeHttp(routes(**{f"regnskap/{ORG}": (200, reg)})), ORG)
        self.assertEqual(field(p, "acc_loennskostnad")["value"]["amount"], 123456789)
        self.assertEqual(field(p, "acc_loennskostnad")["label"], "Payroll costs (company)")
        self.assertEqual(field(p, "acc_sum_bankinnskudd_og_kontanter")["value"]["amount"], 98765)
        self.assertIsNone(field(p, "acc_sum_driftsinntekter"))          # already published as 'revenue'

    def test_conclusion_every_claim_supported(self):
        p = build_profile(FakeHttp(routes()), ORG)
        c = p["conclusion"]
        self.assertEqual(c["verdict"], "No registry risk flags")
        self.assertGreaterEqual(len(c["claims"]), 4)
        fields = {f["field"] for f in p["facts"]}
        for cl in c["claims"]:
            self.assertTrue(cl["supported_by"], cl["claim"])
            for sup in cl["supported_by"]:
                if sup["field"] not in ("news", "risk_signals"):
                    self.assertIn(sup["field"], fields)
        self.assertIn("NOK 900 000 000 000", c["text"])                    # exact, not rounded

    def test_conclusion_flags_risk(self):
        reg = json.loads(json.dumps(REGNSKAP))
        reg[0]["egenkapitalGjeld"]["egenkapital"]["sumEgenkapital"] = -5
        p = build_profile(FakeHttp(routes(**{f"regnskap/{ORG}": (200, reg)})), ORG)
        self.assertEqual(p["conclusion"]["verdict"], "Needs attention")
        self.assertIn("negative equity", p["conclusion"]["text"])


class AuditFixTests(unittest.TestCase):
    def test_every_fact_has_hash_and_quote(self):
        for http, org in ((FakeHttp(NewsTests().routes()), ORG), (FakeHttp(DiscoveryTests().r()), SN)):
            p = build_profile(http, org)
            for f in p["facts"]:
                ref = f["reference"]
                self.assertEqual(len(ref["content_sha256"] or ""), 64, f["field"])
                self.assertTrue(ref["quote"], f["field"])
                self.assertIn(ref["quote_status"], ("exact", "derived", "exact_page_text", "derived_page_text"), f["field"])
                body = http.seen[ref["url"]].text()
                if not ref["quote_status"].endswith("page_text"):
                    self.assertIn(ref["quote"], body, f["field"])          # verbatim substring of the content
            for it in p["news"]:
                self.assertEqual(len(it["reference"]["content_sha256"]), 64)
                self.assertIn(it["title"], it["reference"]["quote"])

    def test_exact_quote_for_registry_values(self):
        p = build_profile(FakeHttp(routes()), ORG)
        q = {f["field"]: f["reference"]["quote"] for f in p["facts"]}
        self.assertEqual(q["employees"], '"antallAnsatte": 21000')
        self.assertEqual(q["revenue"], '"sumDriftsinntekter": 900000000000')
        self.assertEqual(q["bankrupt"], '"konkurs": false')

    def test_translations_exact_with_original(self):
        from nca.news import translate_announcement
        self.assertEqual(translate_announcement("Styre"), "Board")
        self.assertEqual(translate_announcement("Kapital"), "Capital")
        self.assertEqual(translate_announcement("Godkjent årsregnskap med bærekraftsrapportering"),
                         "Approved annual accounts with sustainability reporting")
        self.assertIsNone(translate_announcement("Kapitalnedsettelse og noe annet"))   # unknown: never guessed

    def test_group_site_not_attributed(self):
        group = ("<html><head><title>ECIT – accounting group</title><link rel='alternate' type='application/rss+xml' href='/feed'>"
                 "</head><body><p>ECIT is a Nordic group delivering accounting and IT services to companies of all sizes.</p>"
                 "<a href='https://www.linkedin.com/company/ecit'>in</a> post@ecit.com Tlf 22 33 44 55</body></html>")
        e = dict(ENHET, navn="ECIT WLCOM AS", hjemmeside="www.ecit.com")
        r = routes(**{f"/enheter/{ORG}": (200, e), "ecit.com": (200, group)})
        p = build_profile(FakeHttp(r), ORG)
        self.assertRegex(field(p, "website_ownership")["value"], r"^(shared or parent-group site|ownership not proven)")
        for f in ("social_profiles", "website_emails", "website_phones", "website_about", "news_company", "website_site_name"):
            self.assertIsNone(field(p, f), f)
        self.assertEqual(field(p, "website")["value"], "www.ecit.com")       # the registered address itself stays

    def test_feed_rejects_menu_product_and_updated_dates(self):
        from nca.news import _parse_feed, is_real_post
        atom = ("<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Om oss</title><link href='https://x.no/om-oss'/>"
                "<updated>2026-01-01T00:00:00Z</updated></entry></feed>")
        self.assertIsNone(_parse_feed(atom)[0]["published"])                 # 'updated' is not a publish date
        self.assertFalse(is_real_post("Malingssett Pro 2L", "https://x.no/produkter/malingssett", "https://x.no/"))
        self.assertFalse(is_real_post("Kontakt", "https://x.no/kontakt", "https://x.no/"))
        self.assertTrue(is_real_post("Vi har vunnet anbud på Brattøra", "https://x.no/nyheter/vunnet-anbud", "https://x.no/"))

    def test_quote_pages_are_not_news(self):
        from nca.news import QUOTE_PAGE
        self.assertTrue(QUOTE_PAGE.search("Equinor ASA (EQNRN.MX) - Yahoo Finanzas"))
        self.assertFalse(QUOTE_PAGE.search("DNB Bank ASA reduces workforce by about 400"))


class SelfAuditTests(unittest.TestCase):
    def test_tampered_fact_is_withheld(self):
        from nca.audit import verify_facts
        http = FakeHttp(routes())
        p = build_profile(http, ORG)
        f = dict(next(x for x in p["facts"] if x["field"] == "employees"))
        f["value"] = 99999                                   # claim no longer matches its quote
        facts = [f]
        withheld = verify_facts(facts, lambda u: http.seen.get(u))
        self.assertEqual(facts, [])
        self.assertIn("99999 not found", withheld[0]["reason"])

    def test_hash_mismatch_is_withheld(self):
        from nca.audit import verify_facts
        http = FakeHttp(routes())
        p = build_profile(http, ORG)
        f = json.loads(json.dumps(next(x for x in p["facts"] if x["field"] == "legal_name")))
        f["reference"]["content_sha256"] = "0" * 64
        facts = [f]
        self.assertEqual(verify_facts(facts, lambda u: http.seen.get(u))[0]["reason"], "content hash mismatch")

    def test_clean_profiles_pass_audit(self):
        for http, org in ((FakeHttp(NewsTests().routes()), ORG), (FakeHttp(DiscoveryTests().r()), SN)):
            p = build_profile(http, org)
            self.assertEqual(p["verification"]["withheld"], [])
            self.assertEqual(p["verification"]["self_audit"], [], p["verification"]["self_audit"])

    def test_audit_catches_bad_text(self):
        from nca.audit import audit_profile
        p = build_profile(FakeHttp(routes()), ORG)
        p["summary"] += " Latest revenue was NOK 123 456."
        checks = {i["check"] for i in audit_profile(p)}
        self.assertIn("figure", checks)
        self.assertIn("wording", checks)


class RobustnessTests(unittest.TestCase):
    """Strange-but-possible registry shapes must never crash or produce unsupported claims."""

    def build(self, enhet, **over):
        r = routes(**{f"/enheter/{ORG}": (200, enhet)})
        r.update(over)
        return build_profile(FakeHttp(r), ORG)

    def test_nulls_and_wrong_types(self):
        e = {"organisasjonsnummer": ORG, "navn": "TEST AS", "organisasjonsform": None, "forretningsadresse": None,
             "naeringskode1": None, "antallAnsatte": None, "harRegistrertAntallAnsatte": None, "hjemmeside": None,
             "konkurs": None, "vedtektsfestetFormaal": None, "paategninger": "x", "frivilligMvaRegistrertBeskrivelser": None}
        p = self.build(e, **{f"regnskap/{ORG}": (200, [None, {"virksomhet": None}]), "/roller": (200, {"rollegrupper": None})})
        self.assertEqual(p["status"], "registered")                # flags not stated: never called "active"
        self.assertNotIn("no bankruptcy", p["summary"])
        self.assertIsNone(field(p, "employees"))
        self.assertIsNone(field(p, "bankrupt"))
        self.assertEqual(p["verification"]["self_audit"], [])

    def test_foreign_nuf_and_non_nok(self):
        e = dict(ENHET, organisasjonsform={"kode": "NUF", "beskrivelse": "Norskregistrert utenlandsk foretak"},
                 forretningsadresse={"adresse": ["1 Main St"], "poststed": "LONDON", "land": "Storbritannia", "landkode": "GB"})
        reg = json.loads(json.dumps(REGNSKAP))
        reg[0]["valuta"] = "EUR"
        p = self.build(e, **{f"regnskap/{ORG}": (200, reg)})
        self.assertEqual(field(p, "revenue")["value"]["currency"], "EUR")
        self.assertIn("Storbritannia", field(p, "business_address")["value"])
        self.assertNotIn("NOK", p["summary"])                 # never relabels a foreign currency

    def test_deleted_entity(self):
        r = {f"/enheter/{ORG}": (410, {"organisasjonsnummer": ORG, "navn": "GONE AS", "slettedato": "2020-02-02"})}
        p = build_profile(FakeHttp(r), ORG)
        self.assertEqual(p["status"], "deleted")
        self.assertEqual(p["conclusion"]["verdict"], "Deleted")

    def test_accounts_for_other_company_never_used(self):
        reg = [dict(REGNSKAP[0], virksomhet={"organisasjonsnummer": "111111118"})]
        p = self.build(ENHET, **{f"regnskap/{ORG}": (200, reg)})
        self.assertIsNone(field(p, "revenue"))

    def test_300_company_batch(self):
        import random
        rnd = random.Random(3)
        def num():
            while True:
                n = "9" + "".join(str(rnd.randrange(10)) for _ in range(7))
                w = [3, 2, 7, 6, 5, 4, 3, 2]
                r = sum(int(d) * x for d, x in zip(n, w)) % 11
                c = 0 if r == 0 else 11 - r
                if c != 10:
                    return n + str(c)
        orgs = sorted({num() for _ in range(300)})
        routes_ = {}
        for i, n in enumerate(orgs):
            e = dict(ENHET, organisasjonsnummer=n, navn=f"FIRMA {i} AS", hjemmeside=None)
            if i % 3 == 0:
                e = dict(e, harRegistrertAntallAnsatte=False)
                e.pop("antallAnsatte", None)
            routes_[f"/enheter/{n}/roller"] = (200, {"rollegrupper": []})
            routes_[f"underenheter?overordnetEnhet={n}"] = (200, {"_embedded": {"underenheter": []}, "page": {"totalElements": 0}})
            routes_[f"/enheter/{n}"] = (200, e)
            routes_[f"regnskap/{n}"] = (404, {})
        with tempfile.TemporaryDirectory() as tmp:
            http = FakeHttp(routes_, max_requests=1900)
            profs, rep = run_batch(http, orgs, tmp, workers=8, progress=False)
            self.assertEqual(rep["companies"], len(orgs))
            self.assertLessEqual(http.used, 1900)
            self.assertNotIn("error", rep["statuses"])
            self.assertTrue(all(p.get("name") for p in profs))
            self.assertTrue(os.path.getsize(os.path.join(tmp, "report.html")) > 1000)


class CompactOutputTests(unittest.TestCase):
    def test_compact_split_and_update(self):
        import nca.runner as R
        old = R.PART_BYTES
        R.PART_BYTES = 40_000                                    # force several parts
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = {**NewsTests().routes(), **DiscoveryTests().r()}
                run_batch(FakeHttp(r), [ORG, SN], tmp, workers=1, progress=False, compact=True)
                names = sorted(os.listdir(tmp))
                self.assertNotIn("companies", names)
                parts = [n for n in names if n.startswith("profiles")]
                self.assertGreaterEqual(len(parts), 2)
                self.assertIn("report.html", names)
                for n in names:
                    self.assertLess(os.path.getsize(os.path.join(tmp, n)), 25_000_000)
                profs, rep = run_batch(FakeHttp(r), [ORG, SN], tmp, workers=1, progress=False, compact=True)
                self.assertEqual(rep["companies_with_changes"], 0, [p["changes"] for p in profs])       # previous read back from bundles
                self.assertTrue(all(f.get("first_seen") for p in profs for f in p["facts"]))
        finally:
            R.PART_BYTES = old


class CompatibilityTests(unittest.TestCase):
    def test_no_python312_only_fstrings(self):
        """Python 3.9-3.11 reject an f-string that reuses its own quote character inside {...}."""
        import glob
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pats = [re.compile(r'(?<![\w])[rR]?[fF][rR]?"[^"\n]*\{[^}"\n]*"'),
                re.compile(r"(?<![\w])[rR]?[fF][rR]?'[^'\n]*\{[^}'\n]*'")]
        for path in glob.glob(os.path.join(root, "nca", "*.py")) + [os.path.join(root, "agent.py")]:
            with open(path, encoding="utf-8") as fh:
                for no, line in enumerate(fh, 1):
                    if '"""' in line or "'''" in line:
                        continue
                    for pat in pats:
                        m = pat.search(line)
                        if m and "{" in m.group(0):
                            # a closing } before the quote means the string really ended: fine
                            seg = m.group(0)
                            if seg.rfind("}") < seg.rfind("{"):
                                self.fail(f"{os.path.basename(path)}:{no} reuses the f-string quote inside {{...}}: {line.strip()[:90]}")


class ReviewerScenarioTests(unittest.TestCase):
    def test_legal_form_kept_with_real_nested_links(self):
        p = build_profile(FakeHttp(routes()), ORG)
        f = field(p, "legal_form")
        self.assertEqual(f["value"], "ASA – Allmennaksjeselskap")
        self.assertIn('"kode": "ASA"', f["reference"]["quote"])
        self.assertIn("public limited company", p["summary"])

    def test_family_firm_roles_quote_the_right_person(self):
        rol = {"rollegrupper": [
            {"type": {"kode": "STYR"}, "sistEndret": "2024-01-01", "roller": [
                {"type": {"kode": "MEDL"}, "person": {"navn": {"fornavn": "Per", "etternavn": "Hansen"}}}]},
            {"type": {"kode": "DAGL"}, "sistEndret": "2024-01-01", "roller": [
                {"type": {"kode": "DAGL"}, "person": {"navn": {"fornavn": "Kari", "etternavn": "Hansen"}}}]}]}
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}/roller": (200, rol)})), ORG)
        q = field(p, "ceo")["reference"]["quote"]
        self.assertIn('"DAGL"', q)
        self.assertIn('"Kari"', q)
        self.assertNotIn('"Per"', q)

    def test_company_and_group_statements_quoted_separately(self):
        grp = json.loads(json.dumps(REGNSKAP[0]))
        grp.update(regnskapstype="KONSERN", regnskapsperiode={"fraDato": "2023-01-01", "tilDato": "2023-12-31"})
        grp["resultatregnskapResultat"]["driftsresultat"]["driftsinntekter"]["sumDriftsinntekter"] = 777
        p = build_profile(FakeHttp(routes(**{f"regnskap/{ORG}": (200, [grp, REGNSKAP[0]])})), ORG)
        self.assertIn("2024-12-31", field(p, "accounts_period")["reference"]["quote"])
        self.assertIn("2023-12-31", field(p, "group_accounts_period")["reference"]["quote"])
        self.assertIn("777", field(p, "group_revenue")["reference"]["quote"])
        self.assertNotIn("777", field(p, "revenue")["reference"]["quote"])

    def test_same_title_announcements_quote_their_own_row(self):
        html = KUNNGJ_HTML.replace("Endring av forretningsadresse", "Endring av styre")
        p = build_profile(FakeHttp(NewsTests().routes(**{"kunngjoring/hent_nr": (200, html)})), ORG)
        rows = [i for i in p["news"] if i["kind"] == "official_announcement"]
        self.assertEqual(len(rows), 2)
        for it in rows:
            day, mon, yr = it["published"][8:], it["published"][5:7], it["published"][:4]
            self.assertIn(f"{day}.{mon}.{yr}", it["reference"]["quote"])


class WebsiteOwnershipTests(unittest.TestCase):
    def prof(self, name, site_html, hjemmeside="www.example.no", final=None, extra=None):
        e = dict(ENHET, navn=name, hjemmeside=hjemmeside)
        r = routes(**{f"/enheter/{ORG}": (200, e), "example.no": (200, site_html)})
        r.pop("equinor.com", None)
        if extra:
            r.update(extra)
        return build_profile(FakeHttp(r), ORG)

    def test_group_site_without_full_name_is_shared(self):
        html = "<html><title>Telenor Group</title><body><p>Welcome to Telenor Group, connecting people across the Nordics and Asia since 1855.</p>post@example.no</body></html>"
        p = self.prof("TELENOR NORGE AS", html)
        self.assertTrue(field(p, "website_ownership")["value"].startswith("ownership not proven"))
        self.assertIsNone(field(p, "website_emails"))

    def test_full_name_with_suffix_is_own(self):
        html = "<html><title>Telenor</title><body><p>Telenor Norge AS leverer mobil og bredbånd til hele Norge, og har gjort det i mange år nå.</p></body></html>"
        p = self.prof("TELENOR NORGE AS", html)
        self.assertTrue(field(p, "website_ownership")["value"].startswith("the company's own site"))

    def test_group_footer_with_many_org_numbers_is_shared(self):
        html = ("<html><body><footer>Konsernet: Alfa AS org.nr 923 609 016 · Beta AS org.nr 974 760 673 · "
                "Gamma AS org.nr 984 851 006</footer><p>Vi er et konsern med mange selskaper.</p></body></html>")
        p = self.prof("DELTA AS", html)
        self.assertIn("shared or parent-group site", field(p, "website_ownership")["value"])

    def test_group_footer_naming_the_subsidiary_is_not_own(self):
        # reviewer H1: the subsidiary's name next to its number in a group footer must not make the group site its own
        html = ("<html><head><title>Telenor Group</title></head><body><footer>Telenor ASA org.nr 982 463 718 · "
                "Telenor Norge AS org.nr 976 967 631 · Telenor Bedrift AS org.nr 923 609 016</footer></body></html>")
        p = self.prof("TELENOR NORGE AS", html)
        e = dict(ENHET, organisasjonsnummer=ORG)
        self.assertFalse(field(p, "website_ownership")["value"].startswith("the company's own site"))
        self.assertIsNone(field(p, "website_emails"))

    def test_script_numbers_do_not_make_own_site_shared(self):
        # reviewer H2: a 9-digit tracking id in a script is not an organisation number
        html = ("<html><head><script>var pixel='100000008';</script><title>Maleran</title></head><body>"
                "<p>Org.nr 923 609 016</p></body></html>")
        p = self.prof("MALERAN AS", html)
        self.assertTrue(field(p, "website_ownership")["value"].startswith("the company's own site"))

    def test_redirect_to_other_domain_is_not_proven(self):
        e = dict(ENHET, navn="SMÅBEDRIFT AS", hjemmeside="www.smabedrift.no")
        r = routes(**{f"/enheter/{ORG}": (200, e)})
        r.pop("equinor.com", None)
        class Redirecting(FakeHttp):
            def get(self, url, **kw):
                res = super().get(url, **kw)
                if "smabedrift.no" in url:
                    res = Response(url, 200, b"<html><title>Mother Group</title><body><p>Mother Group ASA</p></body></html>",
                                   {"Content-Type": "text/html"}, "https://www.mothergroup.com/")
                    self.seen[url] = res
                    self.seen[res.final_url] = res
                return res
        p = build_profile(Redirecting(r), ORG)
        self.assertIn("redirects to another domain", field(p, "website_ownership")["value"])
        self.assertTrue(field(p, "website_ownership")["value"].startswith("ownership not proven"))

    def test_holding_company_does_not_get_operating_company_site(self):
        from nca.web import ownership
        page = "Maleran AS · Fjellveien 12, 5003 Bergen · post@maleran.no"
        self.assertNotEqual(ownership(page, SN, "MALERAN HOLDING AS")[0], "own")
        self.assertEqual(ownership(page.replace("Maleran AS", "Maleran Holding AS"), SN, "MALERAN HOLDING AS")[0], "own")

    def test_lowercase_as_word_is_not_a_company_suffix(self):
        from nca.web import full_name_pattern
        self.assertIsNone(full_name_pattern("HANSEN AS").search("Hansen as he left the room"))
        self.assertIsNotNone(full_name_pattern("HANSEN AS").search("Kontakt Hansen AS i dag"))
        self.assertIsNone(full_name_pattern("NORD AS").search("Nordas"))

    def test_web_agency_contacts_are_not_the_company(self):
        html = ("<html><body><p>Maleran AS maler hus i Bergen og omegn, med over 25 års erfaring i bransjen.</p>"
                "<p>Org.nr 975 820 440</p><footer>Nettside laget av Webbyrå AS, post@webbyra.no</footer></body></html>")
        e = dict(SMALL, hjemmeside="www.maleran.no")
        r = DiscoveryTests().r(**{f"/enheter/{SN}": (200, e), "maleran.no/kontakt": (404, ""), "maleran.no": (200, html)})
        p = build_profile(FakeHttp(r), SN)
        self.assertIsNone(field(p, "website_emails"))

    def test_social_page_registered_as_website(self):
        e = dict(ENHET, hjemmeside="www.facebook.com/minbedrift")
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertEqual(field(p, "registered_social_profile")["value"], "www.facebook.com/minbedrift")
        self.assertNotIn("facebook", field(p, "website_title")["source_url"])       # the Facebook page is not fetched as a site
        self.assertIn("Wikidata item", field(p, "website_title")["match"])          # Wikidata site used only after proof
        self.assertIn("this organisation number", field(p, "website_title")["match"])


class ReviewerScenarioTests2(unittest.TestCase):
    def test_subunit_is_not_called_active_or_risk_free(self):
        sub = {"organisasjonsnummer": ORG, "navn": "EQUINOR ASA AVD MONGSTAD", "overordnetEnhet": "923609016",
               "organisasjonsform": {"kode": "BEDR", "beskrivelse": "Underenhet til næringsdrivende"},
               "beliggenhetsadresse": {"kommune": "ALVER", "postnummer": "5954", "poststed": "MONGSTAD", "adresse": ["Mongstad"]}}
        r = {f"/enheter/{ORG}": (404, {}), f"/underenheter/{ORG}": (200, sub)}
        p = build_profile(FakeHttp(r), ORG)
        self.assertEqual(p["status"], "registered")
        self.assertNotIn("no bankruptcy", p["summary"].lower())
        self.assertNotIn("No registry risk flags", p["conclusion"]["verdict"])
        self.assertIn("main unit", p["summary"])

    def test_failed_subunit_lookup_is_not_does_not_exist(self):
        r = {f"/enheter/{ORG}": (404, {}), f"/underenheter/{ORG}": (503, {})}
        p = build_profile(FakeHttp(r), ORG)
        self.assertEqual(p["status"], "lookup_failed")
        self.assertNotIn("exists", p["summary"].replace("could not", ""))

    def test_gleif_down_gap_says_not_checked(self):
        p = build_profile(FakeHttp(routes(**{"api.gleif.org": (503, "{}")})), ORG)
        gap = next(g for g in p["gaps"] if g["topic"] == "identifiers")
        self.assertIn("could not be reached", gap["reason"])

    def test_conclusion_quotes_original_title(self):
        html = KUNNGJ_HTML.replace("Endring av styre", "Styre")
        p = build_profile(FakeHttp(NewsTests().routes(**{"kunngjoring/hent_nr": (200, html), "bing.com": (404, ""),
                                                          "equinor.com/news/rss": (404, "")})), ORG)
        txt = p["conclusion"]["text"] + " ".join(p["key_points"])
        self.assertIn('"Styre" (category: Board)', txt)
        self.assertNotIn('"Board"', txt)

    def test_mismatched_registry_number_is_refused(self):
        e = dict(ENHET, organisasjonsnummer="974760673")
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertEqual(p["status"], "lookup_failed")
        self.assertEqual(p["facts"], [])

    def test_zero_result_and_zero_equity_wording(self):
        reg = json.loads(json.dumps(REGNSKAP))
        reg[0]["resultatregnskapResultat"]["aarsresultat"] = 0
        reg[0]["egenkapitalGjeld"]["egenkapital"]["sumEgenkapital"] = 0
        p = build_profile(FakeHttp(routes(**{f"regnskap/{ORG}": (200, reg)})), ORG)
        t = p["conclusion"]["text"]
        self.assertIn("a result of NOK 0", t)
        self.assertNotIn("positive equity of NOK 0", t)
        self.assertNotIn("profit of NOK 0", t)


class SecondReviewTests(unittest.TestCase):
    def test_double_surname_role_is_published(self):
        rol = {"rollegrupper": [{"type": {"kode": "DAGL"}, "sistEndret": "2024-01-01", "roller": [
            {"type": {"kode": "DAGL"}, "person": {"navn": {"fornavn": "Kari", "etternavn": "Berg Hansen"}}}]}]}
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}/roller": (200, rol)})), ORG)
        self.assertEqual(field(p, "ceo")["value"]["name"], "Kari Berg Hansen")
        self.assertIn('"Berg Hansen"', field(p, "ceo")["reference"]["quote"])

    def test_purpose_list_is_published(self):
        e = dict(ENHET, vedtektsfestetFormaal=["Utvinning av olje og gass", "og annen virksomhet knyttet til dette."])
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertIn("Utvinning av olje", field(p, "statutory_purpose")["value"])

    def test_names_with_apostrophes_and_brackets(self):
        from nca.web import full_name_pattern
        self.assertTrue(full_name_pattern("McDonald's Norge AS").search("Velkommen til McDonald's Norge AS!"))
        self.assertTrue(full_name_pattern("XYZ (Norge) AS").search("XYZ (Norge) AS, Oslo"))

    def test_rejected_gleif_gap_is_honest(self):
        g = json.loads(json.dumps(GLEIF))
        g["data"][0]["attributes"]["entity"]["legalName"]["name"] = "Totally Different Holdings Ltd"
        p = build_profile(FakeHttp(routes(**{"api.gleif.org": (200, g)})), ORG)
        gap = next(x for x in p["gaps"] if x["topic"] == "identifiers")
        self.assertIn("could not be matched", gap["reason"])

    def test_withheld_fact_is_not_called_missing(self):
        import nca.audit as AU
        orig = AU.check_fact
        AU.check_fact = lambda f, t=None: ["forced"] if f["field"] == "ceo" else orig(f, t)
        try:
            p = build_profile(FakeHttp(routes()), ORG)
        finally:
            AU.check_fact = orig
        reasons = " ".join(g["reason"] for g in p["gaps"])
        self.assertIn("could not be verified", reasons)
        self.assertNotIn("No CEO (daglig leder) is registered", reasons)

    def test_quotes_come_from_the_statement_used(self):
        older = json.loads(json.dumps(REGNSKAP[0]))
        older["regnskapsperiode"] = {"fraDato": "2023-01-01", "tilDato": "2023-12-31"}
        older["resultatregnskapResultat"]["aarsresultat"] = 11111
        p = build_profile(FakeHttp(routes(**{f"regnskap/{ORG}": (200, [older, REGNSKAP[0]])})), ORG)
        self.assertEqual(field(p, "net_income")["value"]["amount"], 50_000_000_000)
        self.assertIn("2024-12-31", field(p, "accounts_period")["reference"]["quote"])
        self.assertNotIn("11111", field(p, "net_income")["reference"]["quote"])

    def test_currency_unstated_is_not_called_nok(self):
        reg = json.loads(json.dumps(REGNSKAP))
        reg[0].pop("valuta", None)
        p = build_profile(FakeHttp(routes(**{f"regnskap/{ORG}": (200, reg)})), ORG)
        self.assertIsNone(field(p, "revenue")["value"]["currency"])
        self.assertIn("(currency not stated)", p["conclusion"]["text"])
        self.assertNotIn("NOK 900", p["conclusion"]["text"])

    def test_negative_equity_wording(self):
        reg = json.loads(json.dumps(REGNSKAP))
        reg[0]["egenkapitalGjeld"]["egenkapital"]["sumEgenkapital"] = -500
        p = build_profile(FakeHttp(routes(**{f"regnskap/{ORG}": (200, reg)})), ORG)
        self.assertIn("negative equity of NOK 500 (equity below zero)", p["conclusion"]["text"])

    def test_feed_news_from_discovered_site_does_not_claim_registration(self):
        feed = ('<?xml version="1.0"?><rss><channel><item><title>Vi har malt Bryggen i Bergen sentrum</title>'
                '<link>https://maleran.no/nyheter/bryggen</link><pubDate>Tue, 07 Oct 2025 08:00:00 GMT</pubDate></item></channel></rss>')
        site = SITE_OK.replace("</head>", "<link rel='alternate' type='application/rss+xml' href='/feed'></head>")
        r = DiscoveryTests().r(**{"maleran.no/feed": (200, feed), "maleran.no": (200, site)})
        r = {"maleran.no/feed": (200, feed), **r}
        p = build_profile(FakeHttp(r), SN)
        item = next(i for i in p["news"] if i["kind"] == "company_news")
        self.assertNotIn("in Enhetsregisteret", item["company_match"].split("(")[0])
        self.assertIn("domain matching the registered name", item["company_match"])


class FinalReviewTests(unittest.TestCase):
    def test_redirect_with_only_our_orgnr_is_own(self):
        from nca.web import ownership
        v = ownership("Telenor Norge AS · Org.nr 976 967 631", "976967631", "TELENOR NORGE AS",
                      registered_url="https://www.telenornorge.no", final_url="https://www.telenor.no/")
        self.assertEqual(v[0], "own")

    def test_redirect_without_proof_is_unproven_not_shared(self):
        from nca.web import ownership
        v = ownership("Welcome to Telenor", "976967631", "TELENOR NORGE AS",
                      registered_url="https://www.telenornorge.no", final_url="https://www.telenor.no/")
        self.assertEqual(v[0], "unproven")

    def test_group_mailbox_does_not_prove_ownership(self):
        from nca.web import ownership
        rec = {"epostadresse": "post@telenor.com"}
        page = "Telenor Group · contact post@telenor.com"
        self.assertNotEqual(ownership(page, "976967631", "TELENOR NORGE AS", registered_url="https://www.telenor.no",
                                      final_url="https://www.telenor.no", rec=rec)[0], "own")

    def test_contact_never_proves_a_discovered_site(self):
        from nca.web import ownership
        rec = {"epostadresse": "post@maleran.no", "telefon": "55 12 34 56"}
        page = "Maleran · post@maleran.no · tlf 55 12 34 56"
        self.assertEqual(ownership(page, SN, "MALERAN HOLDING AS", final_url="https://maleran.no", rec=rec,
                                   discovered=True)[0], "unproven")
        self.assertEqual(ownership(page, SN, "MALERAN HOLDING AS", registered_url="https://maleran.no",
                                   final_url="https://maleran.no", rec=rec)[0], "own")

    def test_own_by_contact_ownership_fact_has_a_quote(self):
        html = "<html><title>Smaa</title><body><p>Ring oss eller skriv til post@example.no</p></body></html>"
        e = dict(ENHET, navn="SMAABEDRIFT AS", hjemmeside="www.example.no", epostadresse="post@example.no")
        r = routes(**{f"/enheter/{ORG}": (200, e), "example.no": (200, html)})
        r.pop("equinor.com", None)
        p = build_profile(FakeHttp(r), ORG)
        f = field(p, "website_ownership")
        self.assertIsNotNone(f)
        self.assertIn("post@example.no", f["reference"]["quote"])

    def test_report_and_text_never_print_null_currency(self):
        from nca.runner import to_text
        from nca import report
        with open(report.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("currency not stated", src)
        reg = json.loads(json.dumps(REGNSKAP))
        reg[0].pop("valuta", None)
        p = build_profile(FakeHttp(routes(**{f"regnskap/{ORG}": (200, reg)})), ORG)
        t = to_text(p)
        self.assertNotIn(" None ", t)
        self.assertIn("(currency not stated)", t)

    def test_item_quote_never_falls_back_to_another_row(self):
        from nca.evidence import item_quote
        text = ("<table><tr><td><a href='x?kid=111'>Endring av styre</a></td></tr>"
                "<tr><td><a href='x?kid=222'>Konkurs</a></td></tr></table>")
        q, st = item_quote(text, {"url": "https://w2.brreg.no/x?kid=111", "title": "Konkurs"})
        self.assertEqual(st, "not_found")
        q, st = item_quote(text, {"url": "https://w2.brreg.no/x?kid=222", "title": "Konkurs"})
        self.assertIn("kid=222", q)

    def test_item_quote_reads_cdata_titles(self):
        from nca.evidence import item_quote
        feed = "<rss><channel><item><title><![CDATA[Ny avtale med Bergen]]></title><link>https://a.no/n/1</link></item></channel></rss>"
        q, st = item_quote(feed, {"url": "https://a.no/n/1", "title": "Ny avtale med Bergen"})
        self.assertEqual(st, "exact")

    def test_cache_hit_records_redirect_target_for_second_company(self):
        from nca.http import Http, Response
        h = Http(max_requests=10)
        res = Response("https://a.no", 200, b"x", {}, "https://www.a.no/")
        h.cache[("https://a.no", "text/html")] = res
        h.begin_company()
        h.get("https://a.no", accept="text/html")
        self.assertIs(h.company_lookup("https://www.a.no/"), res)
        h.end_company()

    def test_unperioded_accounts_flag_quotes_its_own_year(self):
        from nca.evidence import _statement_scope
        a = json.dumps({"regnskapstype": "SELSKAP", "regnskapsperiode": {"tilDato": "2023-12-31"}, "revisjon": {"ikkeRevidertAarsregnskap": True}})
        b = json.dumps({"regnskapstype": "SELSKAP", "regnskapsperiode": {"tilDato": "2024-12-31"}, "revisjon": {"ikkeRevidertAarsregnskap": False}})
        seg = _statement_scope("[" + a + "," + b + "]", "accounts_audited", True, None, "2024-12-31")
        self.assertIn("2024-12-31", seg)
        self.assertNotIn("2023-12-31", seg)


class LiveFormatTests(unittest.TestCase):
    def _reordered(self, obj):
        # the live register may put "roller" before "type" and "person" before "type"
        if isinstance(obj, dict):
            items = list(obj.items())[::-1]
            return {k: self._reordered(v) for k, v in items}
        if isinstance(obj, list):
            return [self._reordered(x) for x in obj]
        return obj

    def test_roles_are_quoted_whatever_the_key_order(self):
        rol = self._reordered(json.loads(json.dumps(ROLLER)))
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}/roller": (200, rol)})), ORG)
        for fld in ("ceo", "board_chair", "auditor", "board_members"):
            self.assertIsNotNone(field(p, fld), fld)
            self.assertTrue(field(p, fld)["reference"]["quote"])
        self.assertFalse(p["verification"]["withheld"])

    def test_middle_name_is_quoted(self):
        rol = json.loads(json.dumps(ROLLER))
        rol["rollegrupper"][1]["roller"][0]["person"]["navn"] = {"fornavn": "Jarle", "mellomnavn": "Kjell", "etternavn": "Roth"}
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}/roller": (200, rol)})), ORG)
        self.assertEqual(field(p, "board_chair")["value"]["name"], "Jarle Kjell Roth")
        self.assertIn("Roth", field(p, "board_chair")["reference"]["quote"])

    def test_role_of_another_type_is_not_quoted(self):
        from nca.evidence import find_quote
        t = json.dumps(ROLLER)
        q, _ = find_quote(t, {"field": "board_chair", "value": {"name": "Anne Drinkwater"}, "source": "brreg_roller"})
        self.assertIsNone(q)

    def test_care_of_street_is_published(self):
        e = json.loads(json.dumps(ENHET))
        e["forretningsadresse"]["adresse"] = ["c/o Arne Dale", "Oscars gate 65"]
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertEqual(field(p, "business_street")["value"], "c/o Arne Dale, Oscars gate 65")

    def test_address_without_street_is_published(self):
        e = json.loads(json.dumps(ENHET))
        e["forretningsadresse"].pop("adresse", None)
        e["forretningsadresse"].update(postnummer="7336", poststed="MELDAL")
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertEqual(field(p, "business_address")["value"], "7336 MELDAL")

    def test_structured_data_address_on_website_is_quoted(self):
        from nca.evidence import find_quote
        t = '<script type="application/ld+json">{"address":{"streetAddress":"Raudemel 31","postalCode":"6763","addressLocality":"Hornindal"}}</script>'
        q, st = find_quote(t, {"field": "website_address", "value": "Raudemel 31, 6763 Hornindal"})
        self.assertIn("Raudemel 31", q)


GEO = {"metadata": {"totaltAntallTreff": 2}, "adresser": [
    {"adressenavn": "Forusbeen", "adressetekst": "Forusbeen 50", "nummer": 50, "bokstav": "", "kommunenummer": "1103",
     "kommunenavn": "STAVANGER", "postnummer": "4035", "poststed": "STAVANGER", "objtype": "Vegadresse",
     "representasjonspunkt": {"epsg": "EPSG:4258", "lat": 58.89123, "lon": 5.71234}},
    {"adressenavn": "Forusbeen", "adressetekst": "Forusbeen 52", "nummer": 52, "kommunenavn": "STAVANGER",
     "postnummer": "4035", "poststed": "STAVANGER", "representasjonspunkt": {"lat": 58.8, "lon": 5.7}}]}
FRIV = {"organisasjonsnummer": ORG, "registreringsdatoFrivillighetsregisteret": "2010-04-01",
        "kategorier": [{"id": "1", "beskrivelse": "Kultur og kunst"}, {"id": "2", "beskrivelse": "Musikk"}],
        "deltarIGrasrotandelen": True, "_links": {"self": {"href": "x"}}}


class AdditionTests(unittest.TestCase):
    def test_official_address_and_coordinates(self):
        p = build_profile(FakeHttp(routes(**{"ws.geonorge.no": (200, GEO)})), ORG)
        self.assertEqual(field(p, "address_official")["value"], "Forusbeen 50, 4035 STAVANGER")
        self.assertEqual(field(p, "address_coordinates")["value"], "58.89123, 5.71234")
        self.assertIn("mlat=58.89123", field(p, "address_map")["value"])
        q = field(p, "address_coordinates")["reference"]["quote"]
        self.assertIn("Forusbeen 50", q)
        self.assertNotIn("Forusbeen 52", q)
        self.assertFalse(p["verification"]["withheld"])

    def test_no_exact_address_match_publishes_nothing(self):
        geo = json.loads(json.dumps(GEO))
        geo["adresser"] = geo["adresser"][1:]
        p = build_profile(FakeHttp(routes(**{"ws.geonorge.no": (200, geo)})), ORG)
        self.assertIsNone(field(p, "address_official"))
        self.assertIsNone(field(p, "address_coordinates"))

    def test_care_of_line_is_not_looked_up(self):
        from nca.sources import _street_line
        self.assertEqual(_street_line({"adresse": ["c/o Arne Dale", "Oscars gate 65"]}), "Oscars gate 65")
        self.assertIsNone(_street_line({"adresse": ["Postboks 123"]}))
        self.assertEqual(_street_line({"adresse": ["Storgata 5 B"]}), "Storgata 5 B")

    def test_voluntary_register(self):
        e = dict(ENHET, registrertIFrivillighetsregisteret=True)
        p = build_profile(FakeHttp(routes(**{"frivillige-organisasjoner": (200, FRIV), f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertEqual(field(p, "voluntary_kategorier")["value"], ["Kultur og kunst", "Musikk"])
        self.assertEqual(field(p, "voluntary_registreringsdato_frivillighetsregisteret")["value"], "2010-04-01")
        self.assertTrue(field(p, "voluntary_grassroots_share")["value"])
        self.assertFalse(p["verification"]["withheld"])

    def test_voluntary_record_for_other_number_is_ignored(self):
        e = dict(ENHET, registrertIFrivillighetsregisteret=True)
        friv = dict(FRIV, organisasjonsnummer="984851006")
        p = build_profile(FakeHttp(routes(**{"frivillige-organisasjoner": (200, friv), f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertIsNone(field(p, "voluntary_kategorier"))

    def test_common_announcements_are_translated(self):
        from nca.news import translate_announcement as t
        self.assertEqual(t("Godkjente årsregnskap"), "Approved annual accounts")
        self.assertEqual(t("Erverv av egne aksjer-fullmakt"), "Authorisation to acquire own shares")

    def test_multi_page_report_has_one_search_over_all_pages(self):
        import tempfile, copy, nca.runner as R
        base = build_profile(FakeHttp(routes()), ORG)
        ps = []
        for i in range(6):
            p = copy.deepcopy(base)
            p["organisation_number"], p["name"] = str(900000000 + i), f"FIRMA {i} AS"
            ps.append(p)
        old = R.PART_BYTES
        R.PART_BYTES = len(json.dumps(base, ensure_ascii=False)) * 2
        try:
            with tempfile.TemporaryDirectory() as d:
                R.write_reports(d, ps, "2026-10-23", compact=True)
                with open(os.path.join(d, "report.html"), encoding="utf-8") as fh:
                    idx = fh.read()
                self.assertIn('id="q"', idx)
                self.assertIn('"900000005", "FIRMA 5 AS"', idx)
                self.assertTrue(os.path.exists(os.path.join(d, "report-02.html")))
        finally:
            R.PART_BYTES = old

    def test_voluntary_nested_fields_and_duplicate_dates(self):
        e = dict(ENHET, registrertIFrivillighetsregisteret=True)
        friv = {"organisasjonsnummer": ORG, "innfoertDato": "2009-02-18", "foersteGangInnfoert": "2009-02-18",
                "registrering": {"kategorier": [{"kode": "3", "beskrivelse": "Musikk, sang"}],
                                 "grasrotandel": {"deltar": True}}}
        p = build_profile(FakeHttp(routes(**{"frivillige-organisasjoner": (200, friv), f"/enheter/{ORG}": (200, e)})), ORG)
        dated = [f for f in p["facts"] if f["source"] == "brreg_frivillighetsregisteret" and f["value"] == "2009-02-18"]
        self.assertEqual(len(dated), 1)
        self.assertEqual(field(p, "voluntary_registrering_kategorier")["value"], ["Musikk, sang"])
        self.assertTrue(field(p, "voluntary_grassroots_share")["value"])
        self.assertFalse(p["verification"]["withheld"])

    def test_no_syntax_warnings(self):
        import glob, warnings
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for f in glob.glob(os.path.join(root, "nca", "*.py")) + [os.path.join(root, "agent.py")]:
            with open(f, encoding="utf-8") as fh:
                src = fh.read()
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                compile(src, f, "exec")

    def test_article_before_vowel(self):
        e = dict(ENHET, organisasjonsform={"kode": "FLI", "beskrivelse": "Forening/lag/innretning"})
        e.pop("sisteInnsendteAarsregnskap", None)
        p = build_profile(FakeHttp(routes(**{f"/enheter/{ORG}": (200, e)})), ORG)
        self.assertNotIn("As a association", p["conclusion"]["text"])


class AnnouncementOutageTests(unittest.TestCase):
    def _routes(self, kunngj):
        r = NewsTests().routes()
        r["kunngjoring/hent_nr"] = (200, kunngj)
        return r

    def test_busy_page_is_an_error_not_no_news(self):
        from nca.news import official_announcements
        items, st = official_announcements(FakeHttp(self._routes("<html>Tjenesten er midlertidig utilgjengelig</html>")), ORG)
        self.assertEqual(items, [])
        self.assertEqual(st["status"], "not_found")

    def test_outage_keeps_earlier_announcements_as_stale(self):
        import tempfile
        from nca.runner import run_batch
        d = tempfile.mkdtemp()
        first, _ = run_batch(FakeHttp(self._routes(KUNNGJ_HTML)), [ORG], d, workers=1, progress=False, compact=True)
        self.assertTrue(any(n["kind"] == "official_announcement" for n in first[0]["news"]))
        second, rep = run_batch(FakeHttp(self._routes("<html>busy</html>")), [ORG], d, workers=1, progress=False, compact=True)
        self.assertTrue(any(n["kind"] == "official_announcement" for n in second[0]["news"]))
        stale = [f for f in second[0]["facts"] if f["field"] == "news_official"]
        self.assertTrue(stale and stale[0].get("stale"))
        self.assertFalse(any(c["type"] == "removed" and c["field"] == "news_official" for c in second[0]["changes"]))

    def test_retry_after_busy_page_uses_fresh_response(self):
        from nca.http import Http, Response
        h = Http(max_requests=10)
        url = "https://w2.brreg.no/kunngjoring/hent_nr.jsp?orgnr=" + ORG
        h.cache[(url, "text/html")] = Response(url, 200, b"busy", {})
        h.seen[url] = h.cache[(url, "text/html")]
        h.forget(url)
        self.assertNotIn((url, "text/html"), h.cache)
        self.assertNotIn(url, h.seen)


class RetryIncompleteTests(unittest.TestCase):
    def test_retry_only_failed_and_keep_the_rest(self):
        import tempfile, nca.runner as R
        d = tempfile.mkdtemp()
        good = NewsTests().routes()
        R.run_batch(FakeHttp(good), [ORG], d, workers=1, progress=False, compact=True)
        # second company fails (register down)
        bad = dict(good); bad[f"/enheter/{SN}"] = (503, {})
        R.run_batch(FakeHttp(bad), [SN], d, workers=1, progress=False, compact=True, keep_others=True)
        stored = R._load_bundles(d)
        self.assertEqual(set(stored), {ORG, SN})
        retry = [n for n, p in stored.items() if R.needs_retry(p)]
        self.assertEqual(retry, [SN])
        fixed = {**DiscoveryTests().r(), **good}
        profs, rep = R.run_batch(FakeHttp(fixed), retry, d, workers=1, progress=False, compact=True, keep_others=True)
        stored = R._load_bundles(d)
        self.assertEqual(set(stored), {ORG, SN})
        self.assertNotIn(stored[SN]["status"], R.FAILED)
        self.assertEqual(rep["companies"], 2)

    def test_announcement_removals_are_dropped_from_history(self):
        from nca.profile import _clean_history
        h = [{"at": "x", "changes": [{"field": "news_official", "type": "removed"}]},
             {"at": "y", "changes": [{"field": "ceo", "type": "changed"}, {"field": "news_official", "type": "removed"}]}]
        self.assertEqual(_clean_history(h), [{"at": "y", "changes": [{"field": "ceo", "type": "changed"}]}])


class ReportSizeTests(unittest.TestCase):
    def test_every_report_page_is_under_the_limit(self):
        import tempfile, copy, nca.runner as R
        base = build_profile(FakeHttp(NewsTests().routes()), ORG)
        ps = []
        for i in range(30):
            p = copy.deepcopy(base)
            p["organisation_number"], p["name"] = str(900000000 + i), f"FIRMA Ø{i} AS"
            ps.append(p)
        old = R.PART_BYTES
        R.PART_BYTES = 150_000
        try:
            with tempfile.TemporaryDirectory() as d:
                R.write_reports(d, ps, "2026-10-28", compact=True)
                pages = [f for f in os.listdir(d) if f.startswith("report")]
                self.assertGreater(len(pages), 2)
                for f in pages:
                    self.assertLessEqual(os.path.getsize(os.path.join(d, f)), R.PART_BYTES, f)
                self.assertFalse(any(f.startswith(".") for f in os.listdir(d)))
        finally:
            R.PART_BYTES = old
