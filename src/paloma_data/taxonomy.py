from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class Classification:
    primary_type_slug: str | None
    confidence: float
    eligible: bool
    reason: str


# Accuracy-first: only license types with a strong venue/manufacturer interpretation are
# allowed to drive an automatic Paloma type. Public-premises bar licenses remain eligible
# candidates but require a second source / human classification.
ABC_STRONG_TYPES = {
    "2": "winery",   # Winegrower
    "02": "winery",
    "23": "brewery", # Small Beer Manufacturer
    "74": "distillery", # Craft Distiller
}
ABC_BAR_CANDIDATE_TYPES = {"42", "48"}

DATASF_STRONG_NAICS = {
    "312120": "brewery",
    "312130": "winery",
    "312140": "distillery",
}
DATASF_BAR_NAICS = {"722410"}

_NAME_PATTERNS: list[tuple[re.Pattern[str], str, float]] = [
    (re.compile(r"\bbrewpub\b", re.I), "brewpub", 0.98),
    (re.compile(r"\btap\s*room\b", re.I), "taproom", 0.97),
    (re.compile(r"\bwinery\b|\bvineyards?\b", re.I), "winery", 0.97),
    (re.compile(r"\bdistiller(?:y|ies)\b", re.I), "distillery", 0.98),
    (re.compile(r"\bbrewery\b|\bbrewing\b", re.I), "brewery", 0.96),
    (re.compile(r"\bwine\s+bar\b", re.I), "wine_bar", 0.96),
]


def classify_abc(name: str, license_type: str) -> Classification:
    code = str(license_type).strip().lstrip("0") or "0"
    original = str(license_type).strip()
    if original in ABC_STRONG_TYPES or code in ABC_STRONG_TYPES:
        slug = ABC_STRONG_TYPES.get(original) or ABC_STRONG_TYPES[code]
        return Classification(slug, 0.99, True, f"abc_license_type:{original}")
    if original in ABC_BAR_CANDIDATE_TYPES or code in ABC_BAR_CANDIDATE_TYPES:
        by_name = classify_name(name)
        if by_name.primary_type_slug:
            return by_name
        return Classification(None, 0.80, True, f"abc_public_premises:{original}")
    return Classification(None, 0.0, False, f"abc_license_type_not_in_scope:{original}")


def classify_datasf(name: str, naics_code: str | None) -> Classification:
    code = (naics_code or "").strip()
    if code in DATASF_STRONG_NAICS:
        return Classification(DATASF_STRONG_NAICS[code], 0.94, True, f"datasf_naics:{code}")
    if code in DATASF_BAR_NAICS:
        by_name = classify_name(name)
        if by_name.primary_type_slug:
            return by_name
        return Classification(None, 0.78, True, f"datasf_drinking_place_naics:{code}")
    by_name = classify_name(name)
    if by_name.primary_type_slug:
        # Name-only classification is discovery evidence, not enough by itself for creation.
        return Classification(by_name.primary_type_slug, min(by_name.confidence, 0.90), True, by_name.reason)
    return Classification(None, 0.0, False, "datasf_not_paloma_scope")


def classify_name(name: str) -> Classification:
    for pattern, slug, confidence in _NAME_PATTERNS:
        if pattern.search(name or ""):
            return Classification(slug, confidence, True, f"name_pattern:{slug}")
    return Classification(None, 0.0, False, "no_strong_name_signal")
