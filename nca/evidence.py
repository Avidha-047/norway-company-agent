"""Evidence receipts: for every fact, the SHA-256 hash of the exact content that was retrieved and the
exact text in that content that supports the fact.

A fact's `reference` looks like:
  {"url": ..., "retrieved_at": ..., "content_sha256": ..., "quote": "...exact substring of the content...",
   "quote_status": "exact" | "derived" | "not_found", "basis": "..."}

`quote` is always a verbatim substring of the retrieved content (decoded as text). When a fact is
computed from other values (e.g. an English sector name looked up from a NACE code), the quote is the
source text it was computed from and quote_status is "derived", with `basis` explaining the step.
"""
import hashlib
import html as htmllib
import json
import re

# JSON keys in Enhetsregisteret / Regnskapsregisteret responses that hold each fact.
# A tuple means "this key inside that object".
FIELD_KEYS = {
    "organisation_number": "organisasjonsnummer", "legal_name": "navn", "legal_form": "organisasjonsform",
    "registered_date": "registreringsdatoEnhetsregisteret", "founded_date": "stiftelsesdato",
    "start_date": "oppstartsdato", "business_address": "forretningsadresse", "municipality": "kommune",
    "municipality_number": "kommunenummer", "country": "land", "postal_address": "postadresse",
    "industry_code_1": "naeringskode1", "industry_code_2": "naeringskode2", "industry_code_3": "naeringskode3",
    "industry_sector_en": "naeringskode1", "employees": "antallAnsatte",
    "employee_count_registered": "harRegistrertAntallAnsatte", "website": "hjemmeside", "email": "epostadresse",
    "phone": "telefon", "mobile": "mobil", "institutional_sector": "institusjonellSektorkode",
    "vat_registered": "registrertIMvaregisteret", "in_business_register": "registrertIForetaksregisteret",
    "in_foundation_register": "registrertIStiftelsesregisteret",
    "in_voluntary_register": "registrertIFrivillighetsregisteret",
    "last_annual_accounts_year": "sisteInnsendteAarsregnskap", "parent_unit": "overordnetEnhet",
    "statutory_purpose": "vedtektsfestetFormaal", "activity_description": "aktivitet", "articles_date": "vedtektsdato",
    "written_language": "maalform", "bankrupt": "konkurs", "bankruptcy_date": "konkursdato",
    "under_liquidation": "underAvvikling", "liquidation_date": "underAvviklingDato",
    "compulsory_dissolution": "underTvangsavviklingEllerTvangsopplosning", "deleted_date": "slettedato",
    "registered_business_register_date": "registreringsdatoForetaksregisteret",
    "vat_registered_date": "registreringsdatoMerverdiavgiftsregisteret",
    "voluntary_vat_registered_date": "registreringsdatoFrivilligMerverdiavgiftsregisteret",
    "voluntary_register_date": "registreringsdatoFrivillighetsregisteret",
    "party_register_date": "registreringsdatoPartiregisteret",
    "employee_count_date": "registreringsdatoAntallAnsatteEnhetsregisteret",
    "employee_count_aa_date": "registreringsdatoAntallAnsatteNAVAaregisteret",
    "foundation_register_date": "registreringsdatoStiftelsesregisteret", "in_party_register": "registrertIPartiregisteret",
    "voluntary_vat_descriptions": "frivilligMvaRegistrertBeskrivelser", "auxiliary_unit_code": "hjelpeenhetskode",
    "register_notes": "paategninger", "business_street": ("forretningsadresse", "adresse"),
    "business_postcode": ("forretningsadresse", "postnummer"), "business_city": ("forretningsadresse", "poststed"),
    "postal_postcode": ("postadresse", "postnummer"), "postal_city": ("postadresse", "poststed"),
    "subunit_count": "totalElements", "accounts_currency_note": "sisteInnsendteAarsregnskap",
    "accounts_period": "regnskapsperiode", "group_accounts_period": "regnskapsperiode",
    "accounts_currency_unstated": "regnskapsperiode", "group_accounts_currency_unstated": "regnskapsperiode",
    "accounts_audited": "ikkeRevidertAarsregnskap", "group_accounts_audited": "ikkeRevidertAarsregnskap",
    "lei_registration_status": "status", "lei_next_renewal": "nextRenewalDate",
}
# Facts that are computed rather than read verbatim, and what they are computed from.
DERIVED = {
    "address_map": "OpenStreetMap link built from the latitude and longitude in the quoted Kartverket address record",
    "industry_sector_en": "English NACE section looked up from the registered industry code quoted here",
    "accounts_audited": "true when the filed statement's ikkeRevidertAarsregnskap flag (quoted) is false",
    "accounts_currency_unstated": "the filed statement (period quoted) carries no 'valuta' field",
    "group_accounts_currency_unstated": "the filed statement (period quoted) carries no 'valuta' field",
    "group_accounts_audited": "true when the filed statement's ikkeRevidertAarsregnskap flag (quoted) is false",
    "accounts_period": "period start/end dates as filed (quoted)",
    "group_accounts_period": "period start/end dates as filed (quoted)",
    "website_discovered": "the page content proving ownership (quoted)",
    "accounts_currency_note": "Enhetsregisteret's last submitted accounts year (quoted) is later than the period returned by the accounts register",
    "social_profiles": "links found in the page content (first quoted)",
    "subunits": "establishments listed in the response (first quoted)",
    "employee_count_registered": "the register's flag for whether an employee count is held (quoted)",
    "website_final_url": "the address the page content was retrieved from (after redirects); quote is the page title",
    "website_contact_page": "the contact link found on the homepage (quoted)",
    "logo": "logo file name as stated on the Wikidata item (quoted)",
    "website_ownership": "the hashed page content was checked for this company's organisation number and registered name; "
                         "the quote is the matching text when found, otherwise the page title identifying the checked page",
    "website_shows_orgnr": "the hashed page content was checked for the organisation number in any spacing; the quote is the "
                           "match when found, otherwise the page title identifying the checked page",
}
# Findings about what a page does NOT contain: the receipt is the hash of the checked content plus an
# identifying excerpt (its title); the basis states what was checked.
ABSENCE_FIELDS = {"website_shows_orgnr", "website_ownership"}
_SCALAR = r'(?:"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)'


def sha256(body):
    return hashlib.sha256(body or b"").hexdigest()


def _text(res):
    try:
        return res.text()
    except Exception:
        return (res.body or b"").decode("utf-8", errors="replace")


def _window(text, start, end, pad=60, limit=320):
    a = max(0, start - pad)
    b = min(len(text), end + pad)
    q = text[a:b]
    if len(q) > limit:
        q = text[start:min(end, start + limit)]
    return q.strip()


def _obj_span(text, start):
    """End index of the JSON object/array that opens at text[start] (balanced braces, strings respected)."""

    depth, i, n, in_str = 0, start, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _json_obj(text, key):
    """(start, end) of `"key": {...}` / `"key": [...]` including nested content, or None."""
    m = re.search(r'"%s"\s*:\s*(?=[\{\[])' % re.escape(key), text)
    if not m:
        return None
    return m.start(), _obj_span(text, m.end())


def _scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


def _json_key(text, key, value=None):
    """Exact `"key": value` text in a JSON body. When a value is given, ONLY an exact match is returned."""
    if isinstance(key, tuple):
        span = _json_obj(text, key[0])
        if not span:
            return None
        sub = text[span[0]:span[1]]
        return _json_key(sub, key[1], value)
    if value is not None and not isinstance(value, (dict, list)):
        for jv in dict.fromkeys([_scalar(value), json.dumps(value) if isinstance(value, str) else _scalar(value)]):
            m = re.search(r'"%s"\s*:\s*%s' % (re.escape(key), re.escape(jv)), text)
            if m:
                return m.group(0)
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):     # timestamp written in full
            m = re.search(r'"%s"\s*:\s*"%sT[^"]*"' % (re.escape(key), re.escape(value)), text)
            if m:
                return m.group(0)
        if isinstance(value, str):                                                  # value inside a JSON array
            m = re.search(r'"%s"\s*:\s*\[[^\[\]]*?%s[^\[\]]*\]' % (re.escape(key), re.escape(_scalar(value))), text)
            if m:
                return m.group(0)[:400]
        if isinstance(value, str) and " – " in value:          # "AS – Aksjeselskap": code inside the object
            code = value.split(" – ")[0]
            span = _json_obj(text, key)
            if span:
                sub = text[span[0]:span[1]]
                m = re.search(r'"kode"\s*:\s*%s' % re.escape(_scalar(code)), sub)
                if m:
                    return sub[:m.end()]
        span = _json_obj(text, key)
        if isinstance(value, str) and span and text[span[0]:span[1]].rstrip().endswith("]"):
            # list values published joined (e.g. vedtektsfestetFormaal, aktivitet): every element must be in the value
            sub = text[span[0]:span[1]]
            try:
                elems = json.loads(sub[sub.index("["):])
            except ValueError:
                elems = []
            if elems and all(isinstance(e, str) and e in value for e in elems):
                return sub if len(sub) <= 600 else sub[:600]
        if isinstance(value, str) and key in ("forretningsadresse", "beliggenhetsadresse", "postadresse", "regnskapsperiode",
                                              "paategninger", "frivilligMvaRegistrertBeskrivelser"):
            span = _json_obj(text, key)
            if span:
                sub = text[span[0]:span[1]]
                parts = [x.strip() for x in re.split(r",|/", value) if x.strip()]
                if parts and all(p.split()[0].lower() in sub.lower() for p in parts):
                    return sub if len(sub) <= 400 else sub[:400]
        return None
    span = _json_obj(text, key)
    if span:
        sub = text[span[0]:span[1]]
        return sub if len(sub) <= 400 else sub[:400]
    m = re.search(r'"%s"\s*:\s*%s' % (re.escape(key), _SCALAR), text)
    return m.group(0) if m else None


SUFFIXES = {"as", "asa", "sa", "ans", "da", "ba", "nuf", "enk", "ks", "ltd", "inc", "plc", "ab", "gmbh"}


def _needles(value):
    """Strings that would show the value verbatim in the content (never single generic words or legal suffixes)."""
    out = []
    if isinstance(value, bool) or value is None:
        return out
    if isinstance(value, (int, float)):
        out.append(str(value))
    elif isinstance(value, str):
        out += [value, value.rstrip("/"), re.sub(r"^https?://(www\.)?", "", value).rstrip("/")]
        if re.match(r"https?://", value):
            from urllib.parse import unquote, urlsplit
            path = urlsplit(value).path.rstrip("/")
            if path and len(path) > 3:
                out += [path, unquote(path.rsplit("/", 1)[-1]).replace("_", " ")]
        elif "," in value:
            out.append(value.split(",")[0].strip())
    elif isinstance(value, dict):
        if "amount" in value:
            out.append(str(value["amount"]))
        for k in ("lei", "ticker", "wikidata", "name", "title", "registered_as"):
            if isinstance(value.get(k), str):
                out.append(value[k])
        if not out:
            for v in value.values():
                if isinstance(v, str) and re.match(r"https?://", v):
                    out += [v, re.sub(r"^https?://(www\.)?", "", v)]
    elif isinstance(value, list) and value:
        out += _needles(value[0])
    return [n for n in out if n and len(str(n)) >= 3 and str(n).lower() not in SUFFIXES]


def _find_word(text, needle):
    """Case-insensitive match of the whole needle with word boundaries; (start, end) or None."""
    pat = re.compile(r"(?i)(?<![\w])" + re.escape(str(needle)) + r"(?![\w])")
    m = pat.search(text)
    return (m.start(), m.end()) if m else None


def _phone_pattern(p):
    d = re.sub(r"\D", "", p)[-8:]
    return re.compile(r"(?<!\d)" + r"[\s.\-]?".join(re.escape(c) for c in d) + r"(?!\d)") if len(d) == 8 else None


ROLE_CODES = {"ceo": ["DAGL"], "board_chair": ["LEDE"], "board_deputy_chair": ["NEST"], "board_members": ["MEDL"],
              "board_deputies": ["VARA"], "board_observers": ["OBS"], "auditor": ["REVI"], "accountant": ["REGN"],
              "contact_person": ["KONT"], "owner": ["INNH"], "partners": ["DTPR", "DTSO"], "general_partner": ["KOMP"],
              "norwegian_representative": ["REPR"], "business_manager": ["FFØR"], "foreign_head_office": ["HFOR"]}


def _object_spans(text):
    """(start, end) of every JSON object in text, string-aware."""
    spans, stack, i, n, in_str = [], [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            stack.append(i)
        elif c == "}" and stack:
            spans.append((stack.pop(), i + 1))
        i += 1
    return spans


def _role_quote(text, field, value):
    """Quote the role entry (the object holding both the role type and the person/entity) for this exact holder.
    Works whatever the key order in the register's JSON."""
    holder = value[0] if isinstance(value, list) and value else value
    if not isinstance(holder, dict) or not holder.get("name"):
        return None
    codes = set(ROLE_CODES.get(field) or [])
    toks = holder["name"].split()
    first = None
    if holder.get("organisation_number"):
        pat = r'"organisasjonsnummer"\s*:\s*"%s"' % re.escape(holder["organisation_number"])
    elif len(toks) >= 2:
        pat = r'"etternavn"\s*:\s*"(?:[^"]*\s)?%s"' % re.escape(toks[-1])
        first = re.compile(r'"fornavn"\s*:\s*"%s(?:\s[^"]*)?"' % re.escape(toks[0]))
    else:
        pat = r'"etternavn"\s*:\s*%s' % re.escape(_scalar(toks[0]))
    spans = None
    for m in re.finditer(pat, text, re.S):
        spans = spans or _object_spans(text)
        enclosing = sorted((sp for sp in spans if sp[0] < m.start() and sp[1] >= m.end()), key=lambda sp: -sp[0])
        if first is not None and not (enclosing and first.search(text, enclosing[0][0], enclosing[0][1])):
            continue                    # the name object must hold this first name too
        for s0, e0 in enclosing:
            seg = text[s0:e0]
            t = re.search(r'"type"\s*:\s*\{', seg)
            if not t:
                continue
            # this is the role entry: its own "type" object names the role code
            ts = s0 + t.end() - 1
            te = next((e for s, e in spans if s == ts), None)
            k = re.search(r'"kode"\s*:\s*"([^"]+)"', text[ts:te]) if te else None
            if k and (not codes or k.group(1) in codes):
                if e0 - s0 <= 700:
                    return seg
                a, b = min(ts, m.start()), max(te, m.end())
                return text[a:b] if b - a <= 900 else None
            break                       # the holder's own role entry has another role code
    return None


def _statement_scope(text, field, value=None, orgnr=None, as_of=None):
    """For a filed-accounts body (a JSON array of statements), the exact text of the statement this fact is from:
    right type (company/group), right period end, right organisation number."""
    if not text.lstrip().startswith("["):
        return text
    want = "KONSERN" if field.startswith("group_") else "SELSKAP"
    period = value.get("period") if isinstance(value, dict) else (value if isinstance(value, str) and "/" in value else None)
    end = str(period).split("/")[-1] if period else (str(as_of)[:10] if as_of else None)
    i, segs = text.index("["), []
    while True:
        j = text.find("{", i + 1)
        if j < 0:
            break
        k = _obj_span(text, j)
        segs.append(text[j:k])
        i = k
    def ok(seg, strict):
        m = re.search(r'"regnskapstype"\s*:\s*"(\w+)"', seg)
        if (m.group(1) if m else "SELSKAP") != want:
            return False
        if strict and end and not re.search(r'"tilDato"\s*:\s*"%s' % re.escape(end), seg):
            return False
        if strict and orgnr and not re.search(r'"organisasjonsnummer"\s*:\s*"%s"' % re.escape(orgnr), seg):
            return False
        return True
    for strict in (True, False):
        for seg in segs:
            if ok(seg, strict):
                return seg if strict else ""        # never quote from a statement that is not the one used
    return ""


def find_quote(text, fact):
    """(quote, status) where quote is a verbatim substring of `text` supporting the fact."""
    field = fact["field"]
    base = field[len("group_"):] if field.startswith("group_") and field not in FIELD_KEYS else field
    key = fact.get("_key") or FIELD_KEYS.get(field) or FIELD_KEYS.get(base)
    value = fact["value"]
    if isinstance(value, dict) and "filed_as" in value:
        key = value["filed_as"].split(".")[-1]
    status = "derived" if field in DERIVED else "exact"
    if fact.get("source") == "brreg_regnskap":
        m_org = re.search(r"/regnskap/(\d{9})", fact.get("source_url") or "")
        text = _statement_scope(text, field, value, m_org.group(1) if m_org else None, fact.get("as_of"))
        if not text:
            return None, "not_found"
    if isinstance(key, str) and key.startswith("orgnr:"):
        n = key.split(":", 1)[1]
        m = re.search(rf"(?<!\d){n[:3]}[\s.\u00a0]?{n[3:6]}[\s.\u00a0]?{n[6:]}(?!\d)", text)
        if m:
            return _window(text, m.start(), m.end()), status
        key = None
    if isinstance(key, str) and key.startswith("text:"):
        m = re.search(key.split(":", 1)[1], text)
        if m and fact.get("source") == "kartverket_adresser":
            # quote the whole matched address record (street, postcode, municipality and coordinates)
            inner = [sp for sp in _object_spans(text) if sp[0] < m.start() and sp[1] >= m.end()]
            if inner:
                s0, e0 = max(inner, key=lambda sp: sp[0])
                if e0 - s0 <= 2000:
                    return text[s0:e0], status
        if m:
            return _window(text, m.start(), m.end()), status
        key = None
    if field in ABSENCE_FIELDS and (value is False or str(value).startswith(("shared", "ownership not proven"))):
        m = re.search(r"(?is)<title[^>]*>.*?</title>", text)
        if m:
            return m.group(0)[:300], "derived"
        return text[:200].strip(), "derived"
    if field == "website_final_url":
        m = re.search(r"(?is)<title[^>]*>.*?</title>", text)
        if m:
            return m.group(0)[:300], status
    if field in ROLE_CODES and text.lstrip()[:1] in "{[":
        q = _role_quote(text, field, value)
        return (q, status) if q else (None, "not_found")
    if field == "inception" and isinstance(value, str):
        m = re.search(r'"time"\s*:\s*"\+%s' % re.escape(value), text)
        if m:
            return _window(text, m.start(), m.end(), pad=20), status
    if field == "accounts_period" or field == "group_accounts_period":
        span = _json_obj(text, "regnskapsperiode")
        if span and all(d in text[span[0]:span[1]] for d in str(value).split("/")):
            return text[span[0]:span[1]], status
        return None, "not_found"
    if key and field in DERIVED and isinstance(key, (str, tuple)) and text.lstrip()[:1] in "{[":
        q = _json_key(text, key, None)
        if q:
            return q, status
    if key and text.lstrip()[:1] in "{[":
        v = value["amount"] if isinstance(value, dict) and "amount" in value else value
        q = _json_key(text, key, v if not isinstance(v, (dict, list)) else None)
        if q:
            return q, status
        if not isinstance(v, (dict, list)) and v is not None and field not in DERIVED:
            return None, "not_found"                          # never fall back to some other value of that key
    if field == "website_address" and isinstance(value, str):
        for part in value.split(", "):           # structured data stores street and postcode/place separately
            hit = _find_word(text, part)
            if hit:
                return _window(text, *hit), status
            break
    if field in ("website_phones",) and isinstance(value, list):
        for p in value:
            pat = _phone_pattern(p)
            m = pat.search(text) if pat else None
            if m:
                return _window(text, m.start(), m.end()), "exact"
    for n in _needles(value):
        hit = _find_word(text, n)
        if hit:
            return _window(text, *hit), status
    if text and ("<" in text[:200]):
        plain = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text)))
        for n in _needles(value):
            hit = _find_word(plain, n)
            if hit:
                return _window(plain, *hit), status + "_page_text"
    return None, "not_found"


def item_quote(text, item):
    """Quote the exact row/entry of a news item on the page or feed it was read from."""
    url = item.get("url") or ""
    kid = re.search(r"kid=(\d+)", url)
    i = -1
    if kid:
        m = re.search(r"kid=%s(?!\d)" % kid.group(1), text)
        i = m.start() if m else -1
    if i < 0 and url:
        for cand in (url, url.replace("&", "&amp;"), re.sub(r"^https?://[^/]+", "", url)):
            if len(cand) > 4:
                i = text.find(cand)
                if i >= 0:
                    break
    if i >= 0:
        a = max(text.rfind("<tr", 0, i), text.rfind("<item", 0, i), text.rfind("<entry", 0, i))
        ends = [x for x in (text.find("</tr>", i), text.find("</item>", i), text.find("</entry>", i)) if x >= 0]
        b = min(ends) + 6 if ends else i + 200
        if a < 0 or i - a > 800:
            a = max(0, i - 200)
        q = text[a:min(b, a + 900)]
        norm = lambda x: re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ",
                                 re.sub(r"(?s)<!\[CDATA\[(.*?)\]\]>", r"\1", x)))).strip().lower()
        if item.get("title") and norm(item["title"]) and norm(item["title"]) in norm(q):
            return q, "exact"
        return None, "not_found"          # the item's own row was found but does not carry its title
    hit = _find_word(text, item.get("title") or "") if item.get("title") else None
    if hit:
        return _window(text, *hit), "exact"
    return None, "not_found"


def attach(facts, lookup):
    """Add a `reference` (content hash + exact quote) to every fact and to every news item."""
    for f in facts:
        if f.get("stale") and f.get("reference"):
            continue
        ref = None
        for url in dict.fromkeys([f.get("evidence_url"), f.get("source_url")]):
            res = lookup(url) if url else None
            if res is None or not getattr(res, "body", None):
                continue
            text = _text(res)
            quote, status = find_quote(text, f)
            cand = {"url": url, "retrieved_at": f.get("retrieved_at"), "content_sha256": sha256(res.body),
                    "content_bytes": len(res.body), "quote": quote, "quote_status": status}
            if getattr(res, "truncated", False):
                cand["content_truncated"] = True
            if f["field"] in DERIVED:
                cand["basis"] = DERIVED[f["field"]]
            if quote:
                ref = cand
                break
            ref = ref or cand
        if ref is None:
            ref = {"url": f.get("source_url"), "retrieved_at": f.get("retrieved_at"), "content_sha256": None,
                   "quote": None, "quote_status": "content_not_retained"}
        f["reference"] = ref
        f.pop("_key", None)
        if isinstance(f.get("value"), list):
            for it in f["value"]:
                if isinstance(it, dict) and it.get("found_on") and "reference" not in it:
                    res = lookup(it["found_on"])
                    if res is not None and res.body:
                        text = _text(res)
                        q, st = item_quote(text, it)
                        it["reference"] = {"url": it["found_on"], "retrieved_at": f.get("retrieved_at"),
                                           "content_sha256": sha256(res.body), "quote": q, "quote_status": st}
    return facts
