"""Small shared helpers: org-number validation, dates, name matching, formatting."""
import datetime
import difflib
import re
import unicodedata

_WEIGHTS = [3, 2, 7, 6, 5, 4, 3, 2]
ORGNR_RE = re.compile(r"(?<!\d)(\d{3})[\s. ]?(\d{3})[\s. ]?(\d{3})(?!\d)")


def normalize_orgnr(value):
    """'NO 923 609 016 MVA' -> '923609016'. Returns '' when no 9-digit number is present."""
    m = ORGNR_RE.search(str(value))
    return "".join(m.groups()) if m else ""


def valid_orgnr(n):
    """Norwegian organisation numbers carry a modulus-11 check digit."""
    if not re.fullmatch(r"\d{9}", n or ""):
        return False
    rest = sum(int(d) * w for d, w in zip(n[:8], _WEIGHTS)) % 11
    check = 0 if rest == 0 else 11 - rest
    return check != 10 and check == int(n[8])


def fmt_orgnr(n):
    return f"{n[:3]} {n[3:6]} {n[6:]}" if len(n) == 9 else n


def orgnr_pattern(n):
    """Regex that finds the number on a web page in any common spacing."""
    return re.compile(rf"(?<!\d){n[:3]}[\s. ]?{n[3:6]}[\s. ]?{n[6:]}(?!\d)")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dig(obj, *path, default=None):
    for p in path:
        if isinstance(obj, dict) and p in obj:
            obj = obj[p]
        elif isinstance(obj, list) and isinstance(p, int) and -len(obj) <= p < len(obj):
            obj = obj[p]
        else:
            return default
    return obj


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_SUFFIXES = {
    "as", "asa", "sa", "ans", "da", "ba", "nuf", "enk", "ks", "ltd", "limited", "inc", "plc",
    "ab", "gmbh", "group", "holding", "the", "og", "and", "co", "company", "konsern", "norge", "norway",
}


def norm_name(s):
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    s = re.sub(r"[^\w]+", " ", s)
    toks = [t for t in s.split() if t not in _SUFFIXES]
    return " ".join(toks)


def name_similarity(a, b):
    """0..1 similarity of two company names, ignoring legal-form suffixes."""
    A, B = norm_name(a), norm_name(b)
    if not A or not B:
        return 0.0
    if A == B:
        return 1.0
    ta, tb = set(A.split()), set(B.split())
    jacc = len(ta & tb) / len(ta | tb)
    seq = difflib.SequenceMatcher(None, A, B).ratio()
    contain = 0.85 if (A in B or B in A) and min(len(A), len(B)) >= 3 else 0.0
    return round(max(jacc, seq, contain), 3)


def fmt_money(amount, currency="NOK"):
    if not is_number(amount):
        return str(amount)
    a = abs(amount)
    sign = "-" if amount < 0 else ""
    if a >= 1e9:
        return f"{sign}{currency} {a / 1e9:.2f} bn"
    if a >= 1e6:
        return f"{sign}{currency} {a / 1e6:.1f} m"
    return f"{sign}{currency} {a:,.0f}".replace(",", " ")


def fmt_address(addr):
    if not isinstance(addr, dict):
        return None
    lines = [l for l in (addr.get("adresse") or []) if l]
    place = " ".join(x for x in (addr.get("postnummer"), addr.get("poststed")) if x)
    parts = lines + ([place] if place else [])
    if addr.get("land") and addr.get("landkode") not in (None, "NO"):
        parts.append(addr["land"])
    return ", ".join(parts) or None


def fmt_exact(amount, currency="NOK"):
    """Exact amount as filed, e.g. 'NOK 376 040 030' (no rounding). With no currency: '376 040 030 (currency not stated)'."""
    if not is_number(amount):
        return str(amount)
    if not currency:
        return fmt_exact(amount, "§").replace("§ ", "") + " (currency not stated)"
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    if isinstance(a, float) and not a.is_integer():
        whole, frac = repr(a).split(".") if "e" not in repr(a) else (f"{a:.6f}".split("."))
        return f"{currency} {sign}{int(whole):,}".replace(",", " ") + "." + frac
    return f"{currency} {sign}{int(a):,}".replace(",", " ")
