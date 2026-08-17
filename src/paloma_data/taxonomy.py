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
    "2": "winery",
    "02": "winery",
    "23": "brewery",
    "74": "distillery",
}
ABC_BAR_CANDIDATE_TYPES = {"42", "48"}

DATASF_STRONG_NAICS = {
    "312120": "brewery",
    "312130": "winery",
    "312140": "distillery",
}
DATASF_BAR_NAICS = {"722410"}

_OVERTURE_EXACT_TYPES = {
    "cocktail_bar": "cocktail_bar",
    "dive_bar": "dive_bar",
    "wine_bar": "wine_bar",
    "beer_bar": "beer_bar",
    "sports_bar": "sports_bar",
    "pub": "pub",
    "lounge": "lounge",
    "nightclub": "nightclub",
    "night_club": "nightclub",
    "brewery": "brewery",
    "taproom": "taproom",
    "tap_room": "taproom",
    "brewpub": "brewpub",
    "brew_pub": "brewpub",
    "winery": "winery",
    "tasting_room": "tasting_room",
    "distillery": "distillery",
}
_OVERTURE_GENERIC_BAR = {"bar", "drinking_place", "drinking_places"}

_NAME_PATTERNS: list[tuple[re.Pattern[str], str, float]] = [
    (re.compile(r"\bcocktail\s+bar\b", re.I), "cocktail_bar", 0.98),
    (re.compile(r"\bdive\s+bar\b", re.I), "dive_bar", 0.98),
    (re.compile(r"\bsports?\s+bar\b", re.I), "sports_bar", 0.98),
    (re.compile(r"\bbeer\s+bar\b", re.I), "beer_bar", 0.97),
    (re.compile(r"\bbrewpub\b|\bbrew\s+pub\b", re.I), "brewpub", 0.98),
    (re.compile(r"\btap\s*room\b", re.I), "taproom", 0.97),
    (re.compile(r"\btasting\s+room\b", re.I), "tasting_room", 0.96),
    (re.compile(r"\bwinery\b|\bvineyards?\b", re.I), "winery", 0.97),
    (re.compile(r"\bdistiller(?:y|ies)\b", re.I), "distillery", 0.98),
    (re.compile(r"\bbrewery\b|\bbrewing\b", re.I), "brewery", 0.96),
    (re.compile(r"\bwine\s+bar\b", re.I), "wine_bar", 0.96),
    (re.compile(r"\bnight\s*club\b", re.I), "nightclub", 0.97),
    (re.compile(r"\blounge\b", re.I), "lounge", 0.91),
    (re.compile(r"\bpub\b", re.I), "pub", 0.93),
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
        return Classification(by_name.primary_type_slug, min(by_name.confidence, 0.90), True, by_name.reason)
    return Classification(None, 0.0, False, "datasf_not_paloma_scope")


def classify_overture(
    name: str,
    category_tokens: set[str],
    existence_confidence: float | None,
) -> Classification:
    normalized_tokens = {token.casefold().strip() for token in category_tokens if token}
    for token, slug in _OVERTURE_EXACT_TYPES.items():
        if token in normalized_tokens:
            # Overture is a strong corroboration source, but v1 deliberately caps it below the
            # single-source auto-create threshold. A regulator/local source can push the combined
            # evidence over the creation threshold without letting one POI row define truth alone.
            existence = existence_confidence if existence_confidence is not None else 0.90
            return Classification(slug, min(0.94, max(0.0, existence)), True, f"overture_taxonomy:{token}")

    by_name = classify_name(name)
    if normalized_tokens & _OVERTURE_GENERIC_BAR:
        if by_name.primary_type_slug:
            return Classification(
                by_name.primary_type_slug,
                min(0.92, by_name.confidence),
                True,
                f"overture_generic_bar+{by_name.reason}",
            )
        return Classification(None, 0.86, True, "overture_generic_bar")

    return Classification(None, 0.0, False, "overture_not_paloma_scope")


def classify_name(name: str) -> Classification:
    for pattern, slug, confidence in _NAME_PATTERNS:
        if pattern.search(name or ""):
            return Classification(slug, confidence, True, f"name_pattern:{slug}")
    return Classification(None, 0.0, False, "no_strong_name_signal")
