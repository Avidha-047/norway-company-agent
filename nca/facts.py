"""Fact model and the catalogue of permitted public sources."""
from .util import now_iso

SOURCES = {
    "brreg_enhetsregisteret": {
        "name": "Brønnøysund Register Centre – Central Coordinating Register for Legal Entities (Enhetsregisteret)",
        "licence": "Norwegian Licence for Open Government Data (NLOD 2.0)",
        "authority": "official",
    },
    "brreg_roller": {
        "name": "Brønnøysund Register Centre – registered roles (board, CEO, auditor)",
        "licence": "NLOD 2.0",
        "authority": "official",
    },
    "brreg_regnskap": {
        "name": "Brønnøysund Register Centre – Register of Company Accounts (Regnskapsregisteret)",
        "licence": "NLOD 2.0",
        "authority": "official",
    },
    "gleif": {
        "name": "Global Legal Entity Identifier Foundation (GLEIF) LEI records",
        "licence": "CC0 1.0",
        "authority": "official",
    },
    "wikidata": {
        "name": "Wikidata (matched on property P2333, Norwegian organisation number)",
        "licence": "CC0 1.0",
        "authority": "community",
    },
    "brreg_kunngjoringer": {
        "name": "Brønnøysund Register Centre – official announcements (kunngjøringer)",
        "licence": "public register announcements",
        "authority": "official",
    },
    "company_website_feed": {
        "name": "Company's own website news feed (RSS/Atom)",
        "licence": "public web feed; titles, dates and links only",
        "authority": "self-published",
    },
    "press": {
        "name": "Press coverage found via Bing News RSS (accepted only with an exact company-name match)",
        "licence": "public news headlines; titles, dates and links only",
        "authority": "press",
    },
    "website_discovery": {
        "name": "Company website not registered in Enhetsregisteret (name-matching domain or Wikidata), used only after its pages proved ownership (this org number, or full registered name + registered address)",
        "licence": "public web page; only titles, descriptions, contact details and links extracted",
        "authority": "self-published",
    },
    "press_gdelt": {
        "name": "Press coverage found via the GDELT news index (accepted only with an exact company-name match)",
        "licence": "public news metadata; titles, dates and links only",
        "authority": "press",
    },
    "kartverket_adresser": {
        "name": "Norwegian Mapping Authority (Kartverket) – official address register (Matrikkelen), via the Geonorge address API",
        "licence": "CC BY 4.0 (Kartverket)",
        "authority": "official",
    },
    "brreg_frivillighetsregisteret": {
        "name": "Brønnøysund Register Centre – Register of Non-Profit Organisations (Frivillighetsregisteret)",
        "licence": "NLOD 2.0",
        "authority": "official",
    },
    "company_website": {
        "name": "Company's own website (as registered in Enhetsregisteret)",
        "licence": "public web page; only titles, descriptions and links extracted",
        "authority": "self-published",
    },
}


class FactSet:
    """Collects facts for one company. Empty values are never stored."""

    def __init__(self):
        self.items = []

    def add(self, field, label, value, source, source_url, *, evidence_url=None, as_of=None,
            category="identity", confidence="high", match=None, note=None, key=None):
        if value is None or value == "" or value == [] or value == {}:
            return
        self.items.append({
            "field": field,
            "label": label,
            "category": category,
            "value": value,
            "source": source,
            "source_name": SOURCES[source]["name"],
            "source_url": source_url,
            "evidence_url": evidence_url or source_url,
            "as_of": as_of,
            "retrieved_at": now_iso(),
            "confidence": confidence,
            "match": match,
            **({"note": note} if note else {}),
            **({"_key": key} if key else {}),
        })

    def get(self, field):
        for f in self.items:
            if f["field"] == field:
                return f["value"]
        return None
