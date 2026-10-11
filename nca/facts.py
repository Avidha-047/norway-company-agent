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
            category="identity", confidence="high", match=None, note=None):
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
        })

    def get(self, field):
        for f in self.items:
            if f["field"] == field:
                return f["value"]
        return None
