from __future__ import annotations

import math
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rapidfuzz.fuzz import ratio

_LEGAL_SUFFIX = re.compile(r"\b(?:llc|inc|incorporated|corp|corporation|ltd|limited|lp|llp)\.?$", re.I)
_DISPLAY_LEGAL_SUFFIX = re.compile(
    r"(?:,\s*|\s+)(?:llc|inc|incorporated|corp|corporation|ltd|limited|lp|llp)\.?$",
    re.I,
)
_NON_ALNUM = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")
_TRACKING = re.compile(r"^(?:utm_|y_source$|fbclid$|gclid$)", re.I)


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = value.replace("&", " and ")
    value = _NON_ALNUM.sub(" ", value)
    return _SPACE.sub(" ", value).strip()


def normalize_name(value: str | None) -> str:
    cleaned = _clean_text(value)
    cleaned = _LEGAL_SUFFIX.sub("", cleaned).strip()
    return _SPACE.sub(" ", cleaned)


def consumer_display_name(value: str | None) -> str:
    """Remove a terminal corporate suffix without rewriting source capitalization."""
    original = str(value or "").strip()
    cleaned = _DISPLAY_LEGAL_SUFFIX.sub("", original).rstrip(" ,")
    return cleaned or original


def normalize_address(value: str | None) -> str:
    # Preserve a commercial unit number while making common designators equivalent. ``#1206``
    # otherwise loses the hash during punctuation cleanup and fails to match ``Suite 1206``.
    if value:
        value = re.sub(r"#\s*(?=\w)", " unit ", value)
    cleaned = _clean_text(value)
    replacements = {
        r"\bstreet\b": "st",
        r"\bavenue\b": "ave",
        r"\bboulevard\b": "blvd",
        r"\broad\b": "rd",
        r"\bdrive\b": "dr",
        r"\blane\b": "ln",
        r"\bhighway\b": "hwy",
        r"\bnorth\b": "n",
        r"\bsouth\b": "s",
        r"\beast\b": "e",
        r"\bwest\b": "w",
        r"\b(?:suite|ste|unit)\b": "unit",
        r"\bapartment\b": "apt",
    }
    for pattern, replacement in replacements.items():
        cleaned = re.sub(pattern, replacement, cleaned)
    return _SPACE.sub(" ", cleaned).strip()


def normalize_phone(value: str | None, country_code: str = "US") -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if country_code == "US":
        if len(digits) == 10:
            return f"+1{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
    if 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    host = (parts.hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    port = parts.port
    netloc = host
    if port and not ((parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)])
    path = parts.path.rstrip("/") or ""
    return urlunsplit((parts.scheme or "https", netloc, path, query, ""))


def website_host(value: str | None) -> str | None:
    normalized = normalize_url(value)
    if not normalized:
        return None
    return urlsplit(normalized).hostname


def similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return ratio(a, b) / 100.0


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(h), math.sqrt(1 - h))
