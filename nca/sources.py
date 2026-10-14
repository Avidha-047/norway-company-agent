"""Source adapters. Each one fetches by the exact organisation number and returns
facts plus a status record. Nothing is ever guessed: a value is only published
when it is present in a source record that is keyed to the same org number.
"""
import re
import urllib.parse

from .util import dig, fmt_address, fmt_orgnr, is_number, name_similarity, orgnr_pattern

BRREG = "https://data.brreg.no/enhetsregisteret/api"
REGNSKAP = "https://data.brreg.no/regnskapsregisteret/regnskap"
GLEIF = "https://api.gleif.org/api/v1/lei-records"


def brreg_page(orgnr, sub=False):
    kind = "underenheter" if sub else "enheter"
    return f"https://virksomhet.brreg.no/nb/oppslag/{kind}/{orgnr}"


class SourceStatus(dict):
    @classmethod
    def make(cls, source, url, status, http_status=None, note=None):
        return cls(source=source, url=url, status=status, http_status=http_status, **({"note": note} if note else {}))


# --------------------------------------------------------------------------- #
# Enhetsregisteret (main entity / sub-unit)
# --------------------------------------------------------------------------- #

def fetch_entity(http, orgnr):
    """Returns (kind, record, api_url, status). kind in enhet|underenhet|deleted|None."""
    url = f"{BRREG}/enheter/{orgnr}"
    r = http.get(url)
    if r.status == 200 and r.json():
        return "enhet", r.json(), url, SourceStatus.make("brreg_enhetsregisteret", url, "ok", 200)
    if r.status == 410:
        return "deleted", r.json() or {}, url, SourceStatus.make("brreg_enhetsregisteret", url, "ok", 410, "entity deleted")
    if r.status in (404, 400):
        url2 = f"{BRREG}/underenheter/{orgnr}"
        r2 = http.get(url2)
        if r2.status == 200 and r2.json():
            return "underenhet", r2.json(), url2, SourceStatus.make("brreg_enhetsregisteret", url2, "ok", 200, "sub-unit")
        if r2.status == 410:
            return "deleted_underenhet", r2.json() or {}, url2, SourceStatus.make("brreg_enhetsregisteret", url2, "ok", 410)
        return None, None, url, SourceStatus.make("brreg_enhetsregisteret", url, "not_found", r.status)
    return None, None, url, SourceStatus.make("brreg_enhetsregisteret", url, "error", r.status, r.error)


def entity_facts(fs, orgnr, kind, rec, api_url):
    src = "brreg_enhetsregisteret"
    page = brreg_page(orgnr, sub=kind in ("underenhet", "deleted_underenhet"))
    m = "Record retrieved from Enhetsregisteret by exact organisation number"
    if rec.get("organisasjonsnummer") and rec["organisasjonsnummer"] != orgnr:
        raise ValueError("registry returned a different organisation number")

    def add(field, label, value, cat="identity", as_of=None, note=None):
        fs.add(field, label, value, src, api_url, evidence_url=page, as_of=as_of, category=cat, match=m, note=note)

    add("organisation_number", "Organisation number", orgnr)
    add("legal_name", "Registered name", rec.get("navn"))
    form = rec.get("organisasjonsform") or {}
    if form.get("kode"):
        add("legal_form", "Legal form", f"{form['kode']} – {form.get('beskrivelse', '')}".strip(" –"))
    add("registered_date", "Registered in Enhetsregisteret", rec.get("registreringsdatoEnhetsregisteret"), as_of=rec.get("registreringsdatoEnhetsregisteret"))
    add("founded_date", "Founded (stiftelsesdato)", rec.get("stiftelsesdato"), as_of=rec.get("stiftelsesdato"))
    add("start_date", "Start date (sub-unit)", rec.get("oppstartsdato"), as_of=rec.get("oppstartsdato"))
    addr = rec.get("forretningsadresse") or rec.get("beliggenhetsadresse")
    add("business_address", "Business address", fmt_address(addr), "location")
    if isinstance(addr, dict):
        add("municipality", "Municipality", addr.get("kommune"), "location")
        add("municipality_number", "Municipality number", addr.get("kommunenummer"), "location")
        add("country", "Country", addr.get("land"), "location")
    add("postal_address", "Postal address", fmt_address(rec.get("postadresse")), "location")
    for i, key in enumerate(("naeringskode1", "naeringskode2", "naeringskode3"), 1):
        nk = rec.get(key) or {}
        if nk.get("kode"):
            add(f"industry_code_{i}", f"Industry code (NACE) {'primary' if i == 1 else i}",
                f"{nk['kode']} – {nk.get('beskrivelse', '')}".strip(" –"), "activity")
    nk1 = (rec.get("naeringskode1") or {}).get("kode")
    if nk1:
        from .profile import nace_sector
        add("industry_sector_en", "Industry sector (English, NACE section)", nace_sector(nk1), "activity",
            note="NACE section looked up from the registered industry code")
    if is_number(rec.get("antallAnsatte")) and rec.get("harRegistrertAntallAnsatte", True) is not False:
        add("employees", "Registered number of employees", rec["antallAnsatte"], "size")
    elif rec.get("harRegistrertAntallAnsatte") is False:
        add("employees", "Registered number of employees", 0, "size", note="No employees registered in the Aa-register")
    add("website", "Website (registered)", rec.get("hjemmeside"), "contact")
    add("email", "Email (registered)", rec.get("epostadresse"), "contact")
    add("phone", "Phone (registered)", rec.get("telefon"), "contact")
    add("mobile", "Mobile (registered)", rec.get("mobil"), "contact")
    sector = rec.get("institusjonellSektorkode") or {}
    if sector.get("kode"):
        add("institutional_sector", "Institutional sector", f"{sector['kode']} – {sector.get('beskrivelse', '')}".strip(" –"), "activity")
    for key, field, label in (
        ("registrertIMvaregisteret", "vat_registered", "Registered for VAT"),
        ("registrertIForetaksregisteret", "in_business_register", "Registered in the Register of Business Enterprises"),
        ("registrertIStiftelsesregisteret", "in_foundation_register", "Registered in the Register of Foundations"),
        ("registrertIFrivillighetsregisteret", "in_voluntary_register", "Registered in the Register of Non-Profit Organisations"),
    ):
        if isinstance(rec.get(key), bool):
            add(field, label, rec[key], "registrations")
    add("last_annual_accounts_year", "Last submitted annual accounts (year)", rec.get("sisteInnsendteAarsregnskap"), "financials")
    add("parent_unit", "Parent unit (overordnet enhet)", rec.get("overordnetEnhet") or rec.get("parentOrganisasjonsnummer"), "ownership")
    purpose = rec.get("vedtektsfestetFormaal") or rec.get("formaal")
    if isinstance(purpose, list):
        purpose = " ".join(p for p in purpose if p)
    add("statutory_purpose", "Statutory purpose", purpose, "activity")
    activity = rec.get("aktivitet")
    if isinstance(activity, list):
        activity = " ".join(a for a in activity if a)
    add("activity_description", "Registered activity", activity, "activity")
    add("articles_date", "Articles of association dated", rec.get("vedtektsdato"), "registrations", as_of=rec.get("vedtektsdato"))
    add("written_language", "Written language form", rec.get("maalform"), "registrations")

    # Status flags (always published when present: they matter most for "is this company alive?")
    if isinstance(rec.get("konkurs"), bool):
        add("bankrupt", "Bankrupt", rec["konkurs"], "status", as_of=rec.get("konkursdato"))
    add("bankruptcy_date", "Bankruptcy date", rec.get("konkursdato"), "status", as_of=rec.get("konkursdato"))
    if isinstance(rec.get("underAvvikling"), bool):
        add("under_liquidation", "Under liquidation", rec["underAvvikling"], "status", as_of=rec.get("underAvviklingDato"))
    add("liquidation_date", "Liquidation date", rec.get("underAvviklingDato"), "status")
    if isinstance(rec.get("underTvangsavviklingEllerTvangsopplosning"), bool):
        add("compulsory_dissolution", "Under compulsory liquidation/dissolution",
            rec["underTvangsavviklingEllerTvangsopplosning"], "status",
            as_of=rec.get("tvangsopplostPgaManglendeDagligLederDato") or rec.get("tvangsavvikletPgaManglendeSlettingDato"))
    add("deleted_date", "Deleted from register", rec.get("slettedato") or rec.get("nedleggelsesdato"), "status",
        as_of=rec.get("slettedato") or rec.get("nedleggelsesdato"))


def status_of(kind, rec):
    rec = rec or {}
    if kind in ("deleted", "deleted_underenhet") or rec.get("slettedato") or rec.get("nedleggelsesdato"):
        return "deleted"
    if rec.get("konkurs"):
        return "bankrupt"
    if rec.get("underTvangsavviklingEllerTvangsopplosning"):
        return "compulsory_liquidation"
    if rec.get("underAvvikling"):
        return "under_liquidation"
    return "active"


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #

ROLE_FIELDS = {
    "DAGL": ("ceo", "CEO / managing director (daglig leder)"),
    "LEDE": ("board_chair", "Chair of the board"),
    "NEST": ("board_deputy_chair", "Deputy chair"),
    "MEDL": ("board_members", "Board members"),
    "VARA": ("board_deputies", "Deputy board members"),
    "OBS": ("board_observers", "Board observers"),
    "REVI": ("auditor", "Auditor"),
    "REGN": ("accountant", "External accountant"),
    "KONT": ("contact_person", "Contact person"),
    "INNH": ("owner", "Owner (sole proprietorship)"),
    "DTPR": ("partners", "Partners (pro-rata liability)"),
    "DTSO": ("partners", "Partners (joint liability)"),
    "KOMP": ("general_partner", "General partner"),
    "REPR": ("norwegian_representative", "Norwegian representative"),
    "FFØR": ("business_manager", "Business manager (forretningsfører)"),
    "HFOR": ("foreign_head_office", "Foreign head office"),
}
SINGLE = {"ceo", "board_chair", "board_deputy_chair", "auditor", "accountant", "contact_person", "owner",
          "general_partner", "business_manager", "foreign_head_office", "norwegian_representative"}


def _role_holder(role):
    p = role.get("person")
    if p:
        n = p.get("navn") or {}
        name = " ".join(x for x in (n.get("fornavn"), n.get("mellomnavn"), n.get("etternavn")) if x)
        return name or None, None
    e = role.get("enhet")
    if e:
        nm = e.get("navn")
        if isinstance(nm, list):
            nm = " ".join(x for x in nm if x)
        return nm, e.get("organisasjonsnummer")
    return None, None


def roles_facts(http, fs, orgnr):
    url = f"{BRREG}/enheter/{orgnr}/roller"
    r = http.get(url)
    if r.status != 200 or not r.json():
        return SourceStatus.make("brreg_roller", url, "not_found" if r.status == 404 else "error", r.status, r.error)
    data = r.json()
    grouped = {}
    for g in data.get("rollegrupper", []) or []:
        changed = g.get("sistEndret")
        for role in g.get("roller", []) or []:
            if role.get("fratraadt") or role.get("avregistrert"):
                continue
            if dig(role, "person", "erDoed"):
                continue
            code = dig(role, "type", "kode")
            name, holder_org = _role_holder(role)
            if not name:
                continue
            field, label = ROLE_FIELDS.get(code, (None, None))
            if not field:
                field = "other_roles"
                label = "Other registered roles"
                name = f"{dig(role, 'type', 'beskrivelse') or code}: {name}"
            val = {"name": name}
            if holder_org:
                val["organisation_number"] = holder_org
            slot = grouped.setdefault(field, {"label": label, "values": [], "as_of": changed})
            if val not in slot["values"]:
                slot["values"].append(val)
            if changed and (not slot["as_of"] or changed > slot["as_of"]):
                slot["as_of"] = changed
    m = "Roles listed under this exact organisation number in Enhetsregisteret"
    for field, slot in grouped.items():
        vals = slot["values"]
        value = vals[0] if field in SINGLE and len(vals) == 1 else vals
        fs.add(field, slot["label"], value, "brreg_roller", url, evidence_url=brreg_page(orgnr),
               as_of=slot["as_of"], category="people", match=m)
    return SourceStatus.make("brreg_roller", url, "ok", 200)


# --------------------------------------------------------------------------- #
# Sub-units (branches / establishments)
# --------------------------------------------------------------------------- #

def subunits_facts(http, fs, orgnr):
    url = f"{BRREG}/underenheter?overordnetEnhet={orgnr}&size=50"
    r = http.get(url)
    data = r.json() if r.status == 200 else None
    if not data:
        return SourceStatus.make("brreg_enhetsregisteret", url, "error", r.status, r.error)
    total = dig(data, "page", "totalElements")
    units = dig(data, "_embedded", "underenheter", default=[]) or []
    units = [u for u in units if u.get("overordnetEnhet") in (None, orgnr)]
    m = "Sub-units whose registered parent (overordnetEnhet) is this organisation number"
    if is_number(total):
        fs.add("subunit_count", "Number of registered establishments (sub-units)", total, "brreg_enhetsregisteret",
               url, evidence_url=brreg_page(orgnr), category="size", match=m)
    listed = []
    for u in units[:15]:
        item = {"organisation_number": u.get("organisasjonsnummer"), "name": u.get("navn")}
        place = dig(u, "beliggenhetsadresse", "kommune") or dig(u, "beliggenhetsadresse", "poststed")
        if place:
            item["municipality"] = place
        if is_number(u.get("antallAnsatte")):
            item["employees"] = u["antallAnsatte"]
        listed.append(item)
    fs.add("subunits", "Establishments (first 15)", listed, "brreg_enhetsregisteret", url,
           evidence_url=brreg_page(orgnr), category="size", match=m)
    return SourceStatus.make("brreg_enhetsregisteret", url, "ok", 200, "sub-units")


# --------------------------------------------------------------------------- #
# Annual accounts (Regnskapsregisteret)
# --------------------------------------------------------------------------- #

FIN_FIELDS = [
    ("revenue", "Operating revenue", [("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter")]),
    ("operating_expenses", "Operating expenses", [("resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad")]),
    ("operating_profit", "Operating profit (EBIT)", [("resultatregnskapResultat", "driftsresultat", "driftsresultat")]),
    ("financial_result", "Net financial items", [("resultatregnskapResultat", "finansresultat", "nettoFinans")]),
    ("profit_before_tax", "Profit before tax", [("resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad")]),
    ("net_income", "Net income (årsresultat)", [("resultatregnskapResultat", "aarsresultat")]),
    ("total_assets", "Total assets", [("eiendeler", "sumEiendeler")]),
    ("current_assets", "Current assets", [("eiendeler", "omloepsmidler", "sumOmloepsmidler")]),
    ("fixed_assets", "Non-current assets", [("eiendeler", "anleggsmidler", "sumAnleggsmidler")]),
    ("equity", "Total equity", [("egenkapitalGjeld", "egenkapital", "sumEgenkapital")]),
    ("total_liabilities", "Total liabilities", [("egenkapitalGjeld", "gjeldOversikt", "sumGjeld")]),
    ("current_liabilities", "Current liabilities", [("egenkapitalGjeld", "gjeldOversikt", "kortsiktigGjeld", "sumKortsiktigGjeld")]),
    ("long_term_liabilities", "Long-term liabilities", [("egenkapitalGjeld", "gjeldOversikt", "langsiktigGjeld", "sumLangsiktigGjeld")]),
]


def find_number(obj, key):
    """Depth-first search for a numeric value stored under `key` (schema-drift safety net)."""
    if isinstance(obj, dict):
        v = obj.get(key)
        if is_number(v):
            return v
        for child in obj.values():
            r = find_number(child, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for child in obj:
            r = find_number(child, key)
            if r is not None:
                return r
    return None


def accounts_facts(http, fs, orgnr):
    url = f"{REGNSKAP}/{orgnr}"
    r = http.get(url)
    if r.status == 404:
        return SourceStatus.make("brreg_regnskap", url, "not_found", 404)
    data = r.json() if r.status == 200 else None
    if isinstance(data, dict):
        data = [data]
    if data is None:
        # The combined endpoint sometimes fails for banks/insurers; ask per statement type instead.
        data, last = [], r
        for typ in ("SELSKAP", "KONSERN"):
            alt = f"{url}?regnskapstype={typ}"
            ra = http.get(alt)
            last = ra
            js = ra.json() if ra.status == 200 else None
            if isinstance(js, dict):
                js = [js]
            if js:
                data.extend(js)
                url = alt if typ == "SELSKAP" else url
        if not data:
            if last.status == 404:
                return SourceStatus.make("brreg_regnskap", url, "not_found", 404)
            return SourceStatus.make("brreg_regnskap", url, "error", r.status,
                                     r.error or "accounts service returned an error; no figures published")
    rejected = []
    by_type = {}
    for st in data:
        if not isinstance(st, dict):
            continue
        st_org = dig(st, "virksomhet", "organisasjonsnummer")
        if st_org != orgnr:   # never attach another company's numbers
            rejected.append(st_org)
            continue
        typ = (st.get("regnskapstype") or "SELSKAP").upper()
        end = dig(st, "regnskapsperiode", "tilDato") or ""
        if typ not in by_type or end > (dig(by_type[typ], "regnskapsperiode", "tilDato") or ""):
            by_type[typ] = st
    if not by_type:
        return SourceStatus.make("brreg_regnskap", url, "not_found", 200,
                                 "no statements for this organisation number" + (" (mismatched records rejected)" if rejected else ""))
    evidence = f"https://virksomhet.brreg.no/nb/oppslag/enheter/{orgnr}"
    m = "Annual statement filed under this exact organisation number (virksomhet.organisasjonsnummer verified)"
    for typ, st in by_type.items():
        prefix = "" if typ == "SELSKAP" else "group_"
        scope = "company" if typ == "SELSKAP" else "group (consolidated)"
        start = dig(st, "regnskapsperiode", "fraDato")
        end = dig(st, "regnskapsperiode", "tilDato")
        cur = st.get("valuta") or "NOK"
        period = f"{start}/{end}" if start and end else end
        fs.add(prefix + "accounts_period", f"Accounting period ({scope})", period, "brreg_regnskap", url,
               evidence_url=evidence, as_of=end, category="financials", match=m)
        if st.get("avviklingsregnskap") is True:
            fs.add(prefix + "accounts_note", "Accounts note", "Statement prepared on a liquidation basis",
                   "brreg_regnskap", url, evidence_url=evidence, as_of=end, category="financials", match=m)
        for field, label, paths in FIN_FIELDS:
            leaf = paths[0][-1]
            cands = [dig(st, *p) for p in paths] + [find_number(st, leaf)]  # path first, then key-name fallback
            for v in cands:
                if is_number(v):
                    fs.add(prefix + field, f"{label} ({scope})",
                           {"amount": v, "currency": cur, "period": period, "statement": scope},
                           "brreg_regnskap", url, evidence_url=evidence, as_of=end, category="financials", match=m,
                           note="Exact figure as filed; not estimated or recomputed")
                    break
        aud = st.get("revisjon") or {}
        if isinstance(aud.get("ikkeRevidertAarsregnskap"), bool):
            fs.add(prefix + "accounts_audited", f"Accounts audited ({scope})", not aud["ikkeRevidertAarsregnskap"],
                   "brreg_regnskap", url, evidence_url=evidence, as_of=end, category="financials", match=m)
    return SourceStatus.make("brreg_regnskap", url, "ok", 200)


# --------------------------------------------------------------------------- #
# GLEIF (LEI + parent relationships)
# --------------------------------------------------------------------------- #

def _gleif_entity_summary(rec):
    a = rec.get("attributes") or {}
    return {
        "lei": a.get("lei") or rec.get("id"),
        "name": dig(a, "entity", "legalName", "name"),
        "country": dig(a, "entity", "legalAddress", "country"),
        **({"registered_as": dig(a, "entity", "registeredAs")} if dig(a, "entity", "registeredAs") else {}),
    }


def gleif_facts(http, fs, orgnr, legal_name, rejections):
    queries = [
        {"filter[entity.registeredAs]": orgnr},
        {"filter[entity.registeredAs]": fmt_orgnr(orgnr)},
        {"filter[fulltext]": orgnr, "filter[entity.legalAddress.country]": "NO"},
    ]
    records, url, r = [], GLEIF, None
    for q in queries:
        url = f"{GLEIF}?{urllib.parse.urlencode(dict(q, **{'page[size]': 10}))}"
        r = http.get(url, accept="application/vnd.api+json")
        data = r.json() if r.status == 200 else None
        if data and data.get("data"):
            records = data["data"]
            break
    if r is not None and r.status != 200 and not records:
        return SourceStatus.make("gleif", url, "error", r.status, r.error), None
    good = []
    for rec in records:
        a = rec.get("attributes") or {}
        reg_as = re.sub(r"\D", "", dig(a, "entity", "registeredAs") or "")
        country = (dig(a, "entity", "legalAddress", "country") or "")[:2]
        juris = (dig(a, "entity", "jurisdiction") or "")[:2]
        name = dig(a, "entity", "legalName", "name") or ""
        sim = name_similarity(name, legal_name) if legal_name else 1.0
        if reg_as != orgnr or "NO" not in (country, juris):
            rejections.append({"source": "gleif", "candidate": a.get("lei"), "reason": f"registeredAs/country mismatch ({reg_as}, {country})"})
            continue
        if sim < 0.5:
            rejections.append({"source": "gleif", "candidate": a.get("lei"), "reason": f"legal name '{name}' does not match registry name (similarity {sim})"})
            continue
        good.append(rec)
    if not good:
        return SourceStatus.make("gleif", url, "not_found", 200), None
    rec = good[0]
    a = rec["attributes"]
    lei = a.get("lei") or rec.get("id")
    page = f"https://search.gleif.org/#/record/{lei}"
    m = "GLEIF record whose registeredAs equals this organisation number and whose legal name matches the registry"
    add = lambda f, l, v, as_of=None: fs.add(f, l, v, "gleif", url, evidence_url=page, as_of=as_of, category="identifiers", match=m)
    upd = dig(a, "registration", "lastUpdateDate")
    add("lei", "Legal Entity Identifier (LEI)", lei, upd)
    add("lei_registration_status", "LEI registration status", dig(a, "registration", "status"), upd)
    add("lei_next_renewal", "LEI next renewal date", (dig(a, "registration", "nextRenewalDate") or "")[:10] or None, upd)
    for rel, field, label in (("direct-parent", "direct_parent", "Direct parent (GLEIF)"),
                              ("ultimate-parent", "ultimate_parent", "Ultimate parent (GLEIF)")):
        link = dig(rec, "relationships", rel, "links", "lei-record")
        if not link or http.remaining() < 50:
            continue
        pr = http.get(link, accept="application/vnd.api+json")
        pdata = dig(pr.json() or {}, "data") if pr.status == 200 else None
        if isinstance(pdata, dict):
            summ = _gleif_entity_summary(pdata)
            if summ.get("lei") and summ["lei"] != lei:
                fs.add(field, label, summ, "gleif", link, evidence_url=f"https://search.gleif.org/#/record/{summ['lei']}",
                       as_of=upd, category="ownership", match="Parent relationship reported to GLEIF by this LEI")
    return SourceStatus.make("gleif", url, "ok", 200), lei


# --------------------------------------------------------------------------- #
# Wikidata
# --------------------------------------------------------------------------- #

WD_API = "https://www.wikidata.org/w/api.php"


def _fmt_wd_time(t, prec):
    if not t:
        return None
    d = t.lstrip("+")[:10]
    try:
        prec = int(prec)
    except (TypeError, ValueError):
        prec = 11
    return d[:4] if prec <= 9 else d[:7] if prec == 10 else d


def _claims(ent, pid):
    """Non-deprecated claim values for a property, preferred rank first."""
    out = []
    for c in sorted(dig(ent, "claims", pid, default=[]) or [], key=lambda c: c.get("rank") != "preferred"):
        if c.get("rank") == "deprecated":
            continue
        v = dig(c, "mainsnak", "datavalue", "value")
        if v is not None:
            out.append((v, c))
    return out


def _label(ent):
    for lang in ("en", "nb", "no", "nn"):
        v = dig(ent, "labels", lang, "value")
        if v:
            return v
    return None


def wikidata_facts(http, fs, orgnr, legal_name, gleif_lei, rejections):
    q = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": f"haswbstatement:P2333={orgnr}",
                                "srlimit": 5, "format": "json"})
    search_url = f"{WD_API}?{q}"
    r = http.get(search_url, retries=3)
    data = r.json() if r.status == 200 else None
    if data is None:
        return SourceStatus.make("wikidata", search_url, "error", r.status, r.error), None
    qids = [h.get("title") for h in dig(data, "query", "search", default=[]) if str(h.get("title", "")).startswith("Q")]
    if not qids:
        return SourceStatus.make("wikidata", search_url, "not_found", 200), None
    q = urllib.parse.urlencode({"action": "wbgetentities", "ids": "|".join(qids[:5]), "format": "json",
                                "props": "labels|descriptions|claims|sitelinks/urls", "languages": "en|nb|no|nn"})
    ent_url = f"{WD_API}?{q}"
    r = http.get(ent_url, retries=3)
    ents = dig(r.json() or {}, "entities", default={}) if r.status == 200 else {}
    if not ents:
        return SourceStatus.make("wikidata", ent_url, "error", r.status, r.error), None
    accepted = []
    for qid, ent in ents.items():
        nums = {re.sub(r"\D", "", str(v)) for v, _ in _claims(ent, "P2333")}
        if orgnr not in nums:      # re-verify on the entity itself, not just the search index
            continue
        label = _label(ent) or ""
        leis = {str(v) for v, _ in _claims(ent, "P1278")}
        sim = name_similarity(label, legal_name or "")
        if gleif_lei and leis and gleif_lei not in leis:
            rejections.append({"source": "wikidata", "candidate": qid, "reason": "Wikidata LEI conflicts with GLEIF LEI"})
        elif (gleif_lei and gleif_lei in leis) or sim >= 0.6:
            accepted.append((qid, ent, sim, bool(gleif_lei and gleif_lei in leis)))
        else:
            rejections.append({"source": "wikidata", "candidate": qid,
                               "reason": f"label '{label}' does not match registry name (similarity {sim})"})
    if len(accepted) != 1:
        if len(accepted) > 1:
            rejections.append({"source": "wikidata", "candidate": [a[0] for a in accepted], "reason": "ambiguous: several items claim this number"})
        return SourceStatus.make("wikidata", ent_url, "not_found", 200, "no unambiguous verified item"), None
    qid, ent, sim, lei_ok = accepted[0]

    # labels for referenced items (stock exchanges, parents) in one extra request
    exchanges = []
    for v, c in _claims(ent, "P414"):
        if dig(c, "qualifiers", "P582"):        # listing ended
            continue
        ticker = dig(c, "qualifiers", "P249", 0, "datavalue", "value")
        exchanges.append((v.get("id"), ticker))
    parents = [v.get("id") for v, _ in _claims(ent, "P749")]
    ref_ids = sorted({x for x, _ in exchanges if x} | {x for x in parents if x})
    names = {}
    if ref_ids and http.remaining() > 50:
        q = urllib.parse.urlencode({"action": "wbgetentities", "ids": "|".join(ref_ids[:20]), "props": "labels",
                                    "languages": "en|nb|no", "format": "json"})
        rl = http.get(f"{WD_API}?{q}", retries=2)
        for k, e in (dig(rl.json() or {}, "entities", default={}) if rl.status == 200 else {}).items():
            names[k] = _label(e)

    page = f"https://www.wikidata.org/wiki/{qid}"
    m = (f"Wikidata item {qid} states organisation number {orgnr} (P2333); "
         + ("LEI agrees with GLEIF" if lei_ok else f"label matches registry name (similarity {sim})"))
    add = lambda f, l, v, cat="identifiers", conf="medium": fs.add(f, l, v, "wikidata", ent_url, evidence_url=page,
                                                                     category=cat, confidence=conf, match=m)
    add("wikidata_id", "Wikidata item", qid, conf="high")
    desc = dig(ent, "descriptions", "en", "value") or dig(ent, "descriptions", "nb", "value")
    add("description", "Short description (Wikidata)", desc, "activity")
    inc = _claims(ent, "P571")
    if inc and isinstance(inc[0][0], dict):
        add("inception", "Inception (Wikidata)", _fmt_wd_time(inc[0][0].get("time"), inc[0][0].get("precision")), "identity")
    sites = [v for v, _ in _claims(ent, "P856") if isinstance(v, str)]
    add("official_website", "Official website (Wikidata)", sites[0] if sites else None, "contact")
    add("isin", "ISIN", sorted({v for v, _ in _claims(ent, "P946") if isinstance(v, str)}) or None)
    listings = [{"exchange": names.get(x) or x, **({"ticker": t} if t else {})} for x, t in exchanges if x]
    add("stock_listings", "Stock listings", listings or None)
    add("parent_organization", "Parent organisation (Wikidata)",
        [{"name": names.get(x) or x, "wikidata": x} for x in parents if x] or None, "ownership")
    logos = [v for v, _ in _claims(ent, "P154") if isinstance(v, str)]
    if logos:
        add("logo", "Logo (Wikimedia Commons)",
            "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(logos[0].replace(" ", "_")), "identity")
    add("wikipedia_en", "Wikipedia (English)", dig(ent, "sitelinks", "enwiki", "url"), "identity")
    add("wikipedia_no", "Wikipedia (Norwegian)", dig(ent, "sitelinks", "nowiki", "url"), "identity")
    return SourceStatus.make("wikidata", ent_url, "ok", 200), qid


# --------------------------------------------------------------------------- #
# Company website
# --------------------------------------------------------------------------- #

SOCIAL = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?(?:www\.)?(linkedin\.com/company/[\w\-%.]+|facebook\.com/(?!sharer|share|dialog|plugins|tr\b)[\w\-.%]+"
    r"|instagram\.com/(?!p/|share)[\w\-.]+|(?:twitter|x)\.com/(?!intent|share|home)[\w]+|youtube\.com/(?:@|c/|channel/|user/)[\w\-.]+)",
    re.I)


def _meta(html, name):
    for pat in (rf'<meta[^>]+(?:name|property)=["\']{name}["\'][^>]*content=["\']([^"\']*)["\']',
                rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\']{name}["\']'):
        m = re.search(pat, html, re.I | re.S)
        if m and m.group(1).strip():
            return _clean(m.group(1))
    return None


def _clean(s):
    import html as h
    return re.sub(r"\s+", " ", h.unescape(s)).strip()[:400] or None


def website_facts(http, fs, orgnr, site):
    if not site:
        return None
    url = site.strip()
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    r = http.get(url, accept="text/html,application/xhtml+xml", max_bytes=1_500_000, retries=1, timeout=12)
    if r.status != 200 and url.startswith("https://"):
        alt = "http://" + url[len("https://"):]
        r2 = http.get(alt, accept="text/html", max_bytes=1_500_000, retries=0, timeout=12)
        if r2.status == 200:
            r, url = r2, alt
    if r.status != 200:
        return SourceStatus.make("company_website", url, "error", r.status, r.error or "website not reachable")
    html = r.text()
    mentions = bool(orgnr_pattern(orgnr).search(html))
    conf = "high" if mentions else "medium"
    m = ("Website registered for this organisation number in Enhetsregisteret"
         + ("; the page itself shows the organisation number" if mentions else ""))
    add = lambda f, l, v, cat="web": fs.add(f, l, v, "company_website", r.final_url, category=cat, confidence=conf, match=m)
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    add("website_final_url", "Website (resolved URL)", r.final_url)
    add("website_title", "Website title", (_clean(title.group(1)) if title else None)
        or _meta(html, "og:title") or _meta(html, "twitter:title"))
    add("website_description", "Website description", _meta(html, "description") or _meta(html, "og:description"))
    add("website_site_name", "Website brand name", _meta(html, "og:site_name"))
    add("website_shows_orgnr", "Website shows the organisation number", mentions)
    socials = {}
    for mm in SOCIAL.finditer(html):
        link = mm.group(0).rstrip("/.")
        platform = mm.group(1).split(".")[0].lower().replace("twitter", "x")
        socials.setdefault(platform, link)
    add("social_profiles", "Social media profiles (linked from website)", socials or None, "contact")
    from .news import find_feed
    st = SourceStatus.make("company_website", r.final_url, "ok", 200)
    st["feed_url"] = find_feed(html, r.final_url)
    return st
