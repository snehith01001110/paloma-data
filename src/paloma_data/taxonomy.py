from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class Classification:
    primary_type_slug: str | None
    confidence: float
    eligible: bool
    reason: str


# ABC types describe licensed privileges, not the consumer experience. Manufacturer types retain
# their legal facet for matching, while public-premises types resolve only to generic `bar`.
# Neither class is allowed to prove that a walk-in venue currently exists on its own.
ABC_STRONG_TYPES = {
    "2": "winery",
    "02": "winery",
    "23": "brewery",
    "74": "distillery",
}
# Eating-place licenses cannot classify a bar, but they can validate lawful on-premise service
# after a high-quality consumer source independently identifies the place as a Paloma venue.
ABC_BAR_CANDIDATE_TYPES = {"40", "41", "42", "47", "48", "61", "87"}
ABC_BREWPUB_CANDIDATE_TYPES = {"75"}

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
    "hotel_bar": "bar",
    "tiki_bar": "cocktail_bar",
    "speakeasy": "cocktail_bar",
    "beer_garden": "beer_bar",
    "irish_pub": "pub",
    "gastropub": "pub",
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

BAR_TYPES = frozenset(
    {
        "bar",
        "cocktail_bar",
        "dive_bar",
        "wine_bar",
        "beer_bar",
        "sports_bar",
        "pub",
        "lounge",
        "nightclub",
    }
)
ACCESS_SPECIFIC_TYPES = frozenset({*BAR_TYPES, "taproom", "tasting_room", "brewpub"})
GENERIC_MANUFACTURER_TYPES = frozenset({"brewery", "winery", "distillery"})
CONSUMER_VENUE_TYPES = frozenset({*ACCESS_SPECIFIC_TYPES, *GENERIC_MANUFACTURER_TYPES})

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
    (re.compile(r"\bsaloon\b", re.I), "bar", 0.92),
    (re.compile(r"\bbar\b", re.I), "bar", 0.88),
]


def classify_abc(name: str, license_type: str) -> Classification:
    code = str(license_type).strip().lstrip("0") or "0"
    original = str(license_type).strip()
    if original in ABC_STRONG_TYPES or code in ABC_STRONG_TYPES:
        slug = ABC_STRONG_TYPES.get(original) or ABC_STRONG_TYPES[code]
        return Classification(slug, 0.99, True, f"abc_license_type:{original}")
    if original in ABC_BAR_CANDIDATE_TYPES or code in ABC_BAR_CANDIDATE_TYPES:
        # A legal/DBA string cannot safely provide a consumer subtype. The consumer POI source
        # may later refine this generic bar into cocktail_bar, wine_bar, pub, and so on.
        return Classification("bar", 0.90, True, f"abc_public_premises:{original}")
    if original in ABC_BREWPUB_CANDIDATE_TYPES or code in ABC_BREWPUB_CANDIDATE_TYPES:
        return Classification("brewpub", 0.93, True, f"abc_brewpub_license:{original}")
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
    existence = existence_confidence if existence_confidence is not None else 0.90

    # A producer category plus a consumer drinking category is materially different from a bare
    # manufacturer POI.  Preserve that access-specific candidate type so ABC Type 02/23/74 can
    # be matched correctly, while the catalog gate still requires current hours or a manual
    # public-access attestation before publication.
    if "winery" in normalized_tokens and "wine_bar" in normalized_tokens:
        return Classification(
            "tasting_room",
            min(0.94, max(0.0, existence)),
            True,
            "overture_taxonomy:winery+wine_bar",
        )
    if "distillery" in normalized_tokens and normalized_tokens & {
        "bar",
        "cocktail_bar",
        "lounge",
        "tasting_room",
    }:
        return Classification(
            "tasting_room",
            min(0.94, max(0.0, existence)),
            True,
            "overture_taxonomy:distillery+bar",
        )
    if "brewery" in normalized_tokens and normalized_tokens & {
        "bar",
        "beer_bar",
        "pub",
        "taproom",
        "tap_room",
    }:
        return Classification(
            "taproom",
            min(0.94, max(0.0, existence)),
            True,
            "overture_taxonomy:brewery+bar",
        )
    if "brewery" in normalized_tokens and "restaurant" in normalized_tokens:
        return Classification(
            "brewpub",
            min(0.94, max(0.0, existence)),
            True,
            "overture_taxonomy:brewery+restaurant",
        )

    for token, slug in _OVERTURE_EXACT_TYPES.items():
        if token in normalized_tokens:
            # Overture is a strong corroboration source, but v1 deliberately caps it below the
            # single-source auto-create threshold. A regulator/local source can push the combined
            # evidence over the creation threshold without letting one POI row define truth alone.
            return Classification(slug, min(0.94, max(0.0, existence)), True, f"overture_taxonomy:{token}")

    if normalized_tokens & _OVERTURE_GENERIC_BAR:
        return Classification("bar", 0.86, True, "overture_generic_bar")

    return Classification(None, 0.0, False, "overture_not_paloma_scope")


def classify_name(name: str) -> Classification:
    for pattern, slug, confidence in _NAME_PATTERNS:
        if pattern.search(name or ""):
            return Classification(slug, confidence, True, f"name_pattern:{slug}")
    return Classification(None, 0.0, False, "no_strong_name_signal")


def is_consumer_facing_type(primary_type_slug: str | None) -> bool:
    """True for consumer POI categories Paloma can verify, never for ABC by itself.

    A consumer-source ``brewery``/``winery``/``distillery`` remains only a candidate. It still
    needs current hours (or a manual public-access attestation) before publication.
    """
    return primary_type_slug in CONSUMER_VENUE_TYPES
