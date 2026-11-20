"""Version 10 tests: more websites proven, never a weaker proof.

- a privacy-policy / terms page is read to find the organisation number (the most common place on Norwegian sites)
- the registered phone / e-mail proves a site found from the company's own registered data, if the site also shows
  the company's name; never a site found by web search, and never without the name
- a registered website that no longer answers leads to the same careful discovery
- other companies' organisation numbers on the proof page still block the site

Run:  python -m unittest tests.test_v10 -v
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from nca import sources as S  # noqa: E402
from nca.facts import FactSet  # noqa: E402
from nca.http import Response  # noqa: E402
from nca.profile import build_profile  # noqa: E402
from tests.test_offline import SMALL, SN, FakeHttp, field  # noqa: E402

REC = {"navn": "MALERAN AS", "organisasjonsnummer": SN, "organisasjonsform": {"kode": "AS"}, "telefon": "77 12 34 56",
       "forretningsadresse": {"kommune": "TROMSØ", "poststed": "TROMSØ", "adresse": ["Storgata 1"], "postnummer": "9008"}}

HOME_BRAND = ("<html><head><title>Maleran – maling i Tromsø</title></head><body><p>Velkommen til Maleran.</p>"
              '<a href="/personvern">Personvernerklæring</a> <a href="/kontakt">Kontakt</a></body></html>')
PRIVACY_ORGNR = "<html><body><h1>Personvern</h1><p>Behandlingsansvarlig er Maleran, org.nr. 975 820 440.</p></body></html>"
PRIVACY_OTHER = "<html><body><p>Nettstedet driftes av Webbyrå AS, org.nr. 923 609 016.</p></body></html>"
CONTACT_PHONE = "<html><body><p>Ring oss på 77 12 34 56</p></body></html>"
HOME_PHONE_NO_NAME = "<html><head><title>Velkommen</title></head><body><p>Ring 77 12 34 56</p></body></html>"


def routes(**over):
    r = {"maleran.no/personvern": (404, ""), "maleran.no/kontakt": (404, ""), "maleran.no": (200, HOME_BRAND)}
    r.update(over)
    return r


class ProofPage(unittest.TestCase):
    def test_privacy_page_with_orgnr_proves_the_site(self):
        fs = FactSet()
        h = FakeHttp(routes(**{"maleran.no/personvern": (200, PRIVACY_ORGNR)}))
        st, _ = S.discover_website(h, fs, SN, REC, [])
        self.assertIsNotNone(st)
        d = next(f for f in fs.items if f["field"] == "website_discovered")
        self.assertEqual(d["value"], "https://maleran.no/")
        self.assertEqual(d["confidence"], "high")
        self.assertIn("personvern", d.get("evidence_url") or "")

    def test_brand_only_without_proof_page_is_rejected(self):
        rej = []
        st, _ = S.discover_website(FakeHttp(routes()), FactSet(), SN, dict(REC, telefon=None), rej)
        self.assertIsNone(st)
        self.assertTrue(rej)

    def test_other_orgnr_on_proof_page_blocks(self):
        rec = dict(REC, telefon=None)
        st, _ = S.discover_website(FakeHttp(routes(**{"maleran.no/personvern": (200, PRIVACY_OTHER)})),
                                   FactSet(), SN, rec, [])
        self.assertIsNone(st)

    def test_proof_page_choice(self):
        from nca.web import proof_page
        html = ('<a href="/om-oss">Om oss</a><a href="https://other.no/personvern">x</a>'
                '<a href="/salgsbetingelser">Salgsbetingelser</a><a href="/personvern">Personvern</a>')
        self.assertEqual(proof_page(html, "https://maleran.no/", "maleran.no"), "https://maleran.no/personvern")
        self.assertIsNone(proof_page('<a href="/produkter">Produkter</a>', "https://maleran.no/", "maleran.no"))


class RegisteredContactProof(unittest.TestCase):
    def test_registered_phone_and_name_prove_a_name_matching_domain(self):
        fs = FactSet()
        st, _ = S.discover_website(FakeHttp(routes(**{"maleran.no/kontakt": (200, CONTACT_PHONE)})), fs, SN, REC, [])
        self.assertIsNotNone(st)
        d = next(f for f in fs.items if f["field"] == "website_discovered")
        self.assertEqual(d["confidence"], "medium")

    def test_registered_phone_without_the_name_is_not_enough(self):
        st, _ = S.discover_website(FakeHttp({"maleran.no": (200, HOME_PHONE_NO_NAME)}), FactSet(), SN, REC, [])
        self.assertIsNone(st)

    def test_search_candidates_never_proven_by_contact_details(self):
        old = os.environ.get("BRAVE_SEARCH_API_KEY")

        class H(FakeHttp):
            def get(self, url, **kw):
                if "api.search.brave.com" in url:
                    self.used += 1
                    body = {"web": {"results": [{"url": "https://www.tromso-maling.no/"}]}}
                    return Response(url, 200, json.dumps(body).encode(), {"Content-Type": "application/json"}, url)
                return FakeHttp.get(self, url, **kw)
        os.environ["BRAVE_SEARCH_API_KEY"] = "k"
        try:
            page = "<html><head><title>Maleran</title></head><body>Maleran – tlf 77 12 34 56</body></html>"
            st, _ = S.discover_website(H({"tromso-maling.no": (200, page)}), FactSet(), SN, REC, [])
            self.assertIsNone(st)
        finally:
            if old is None:
                os.environ.pop("BRAVE_SEARCH_API_KEY", None)
            else:
                os.environ["BRAVE_SEARCH_API_KEY"] = old


class WrongCompanyGuards(unittest.TestCase):
    """Found in the live 100-company test: BKRAFT HOLDING AS's registered e-mail domain bkraft.no is the site of
    'Bærekraftig Investering AS', which shows the holding's registered phone. That must not be published."""
    BK = {"navn": "BKRAFT HOLDING AS", "organisasjonsnummer": "919072571", "organisasjonsform": {"kode": "AS"},
          "telefon": "92828197", "epostadresse": "brian@bkraft.no",
          "forretningsadresse": {"kommune": "OSLO", "poststed": "OSLO", "adresse": ["Gata 1"], "postnummer": "0150"}}
    PAGE = ("<html><head><title>Bærekraftig Investering AS</title></head><body><p>Vi investerer bærekraftig.</p>"
            "<div class=footer>Kontakt oss: Tlf: (+47) 92828197, E-post: brian@bkraft.no</div></body></html>")

    def test_holding_company_never_proven_by_contact_details(self):
        rej = []
        st, _ = S.discover_website(FakeHttp({"bkraft.no": (200, self.PAGE)}), FactSet(), "919072571", self.BK, rej)
        self.assertIsNone(st)
        self.assertTrue(rej)

    def test_site_naming_another_company_is_rejected(self):
        rec = dict(REC, navn="MALERAN AS")
        page = ("<html><head><title>Malermester Olsen AS</title></head><body>Maleran - ring 77 12 34 56</body></html>")
        st, _ = S.discover_website(FakeHttp({"maleran.no": (200, page)}), FactSet(), SN, rec, [])
        self.assertIsNone(st)

    def test_name_only_inside_an_email_address_does_not_count(self):
        page = "<html><head><title>Velkommen</title></head><body>Ring 77 12 34 56, e-post post@maleran.no</body></html>"
        st, _ = S.discover_website(FakeHttp({"maleran.no": (200, page)}), FactSet(), SN, REC, [])
        self.assertIsNone(st)

    def test_holding_company_still_proven_by_its_org_number(self):
        page = self.PAGE.replace("Vi investerer", "BKraft Holding AS, org.nr 919 072 571. Vi investerer")
        page = page.replace("<title>Bærekraftig Investering AS</title>", "<title>BKraft</title>")
        st, _ = S.discover_website(FakeHttp({"bkraft.no": (200, page)}), FactSet(), "919072571", self.BK, [])
        self.assertIsNotNone(st)


class DeadRegisteredSite(unittest.TestCase):
    def test_dead_registered_site_leads_to_discovery(self):
        rec = dict(SMALL, hjemmeside="www.gammel-maleran.no")
        r = {f"/enheter/{SN}/roller": (200, {"rollegrupper": []}),
             f"underenheter?overordnetEnhet={SN}": (200, {"_embedded": {"underenheter": []}, "page": {"totalElements": 0}}),
             f"/enheter/{SN}": (200, rec),
             "maleran.no/personvern": (200, PRIVACY_ORGNR), "maleran.no/kontakt": (404, ""),
             "gammel-maleran.no": (0, ""), "maleran.no": (200, HOME_BRAND)}
        p = build_profile(FakeHttp(r), SN)
        self.assertEqual(field(p, "website_discovered")["value"], "https://maleran.no/")


if __name__ == "__main__":
    unittest.main()
