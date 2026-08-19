from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any, Literal

import httpx

from paloma_data.normalizers import (
    haversine_meters,
    normalize_address,
    normalize_name,
    normalize_phone,
    similarity,
)


YELP_API_BASE = "https://api.yelp.com/v3"
YELP_MATCH_METHOD = "api_business_search_verified_v2"
YELP_RESPONSE_LIMIT_BYTES = 512 * 1_024
YELP_BUSINESS_ID = re.compile(r"^[A-Za-z0-9_-]{1,255}$")

_ALCOHOL_CATEGORY_ALIASES = frozenset(
    {
        "bars",
        "beerbar",
        "beergardens",
        "breweries",
        "brewpubs",
        "cocktailbars",
        "distilleries",
        "divebars",
        "gaybars",
        "hookah_bars",
        "lounges",
        "pubs",
        "speakeasies",
        "sportsbars",
        "tastingrooms",
        "whiskeybars",
        "wine_bars",
        "wineries",
    }
)
_ALCOHOL_CATEGORY_TERMS = (
    "bar",
    "beer",
    "brewery",
    "brewpub",
    "cocktail",
    "distillery",
    "lounge",
    "pub",
    "speakeasy",
    "tasting room",
    "whiskey",
    "wine",
    "winery",
)
_IGNORED_NAME_TOKENS = frozenset({"the", "and", "at", "sf"})


@dataclass(frozen=True, slots=True)
class YelpMatchInput:
    establishment_id: str
    name: str
    address: str
    city: str
    region: str | None
    postal_code: str | None
    country_code: str
    latitude: float
    longitude: float
    phone_e164: str | None


@dataclass(frozen=True, slots=True)
class YelpMatchSelection:
    outcome: Literal["matched", "not_found", "ambiguous", "rejected"]
    reason: str
    provider_place_id: str | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class YelpDetailsAudit:
    """Non-persistent projection of a Yelp business-details response."""

    identity_compatible: bool
    identity_reason: str
    currently_operating: bool
    has_phone: bool
    has_hours: bool
    has_price: bool


class YelpAPIError(RuntimeError):
    """A bounded error classification that never includes Yelp response content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Yelp API error: {code}")


class YelpPlacesAPI:
    """Resolve durable Yelp IDs without retaining search response attributes.

    Yelp permits business IDs to be retained indefinitely. Search payloads are
    deliberately kept in memory only; rich attributes remain the responsibility
    of the policy-bounded live-details cache.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = YELP_API_BASE,
        client: httpx.Client | None = None,
        max_attempts: int = 2,
        sleeper=time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("YELP_API_KEY is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.max_attempts = max_attempts
        self._sleep = sleeper
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(6.0, connect=3.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "paloma-data/0.4",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "YelpPlacesAPI":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def match(self, expected: YelpMatchInput) -> YelpMatchSelection:
        response = self._get(
            "/businesses/search",
            params={
                "term": expected.name.strip(),
                "latitude": expected.latitude,
                "longitude": expected.longitude,
                "radius": 500,
                "limit": 5,
            },
        )
        _raise_for_status(response)
        if len(response.content) > YELP_RESPONSE_LIMIT_BYTES:
            raise YelpAPIError("invalid_payload")
        try:
            payload = response.json()
        except ValueError as exc:
            raise YelpAPIError("invalid_payload") from exc
        if not isinstance(payload, dict):
            raise YelpAPIError("invalid_payload")
        return select_yelp_business_match(payload, expected)

    def audit_details(
        self,
        provider_place_id: str,
        expected: YelpMatchInput,
    ) -> YelpDetailsAudit:
        """Audit live identity/status/coverage without returning provider attributes."""
        if not YELP_BUSINESS_ID.fullmatch(provider_place_id):
            raise YelpAPIError("invalid_request")
        response = self._get(f"/businesses/{provider_place_id}", params={})
        _raise_for_status(response)
        if len(response.content) > YELP_RESPONSE_LIMIT_BYTES:
            raise YelpAPIError("invalid_payload")
        try:
            payload = response.json()
        except ValueError as exc:
            raise YelpAPIError("invalid_payload") from exc
        if not isinstance(payload, dict):
            raise YelpAPIError("invalid_payload")
        accepted, reason, _, _ = _validate_candidate(payload, expected)
        return YelpDetailsAudit(
            identity_compatible=accepted,
            identity_reason=reason,
            currently_operating=payload.get("is_closed") is False,
            has_phone=bool(_text(payload.get("phone") or payload.get("display_phone"))),
            has_hours=isinstance(payload.get("hours"), list) and bool(payload["hours"]),
            has_price=bool(_text(payload.get("price"))),
        )

    def _get(self, path: str, *, params: dict[str, Any]) -> httpx.Response:
        retryable_statuses = {429, 500, 502, 503, 504}
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_attempts:
                    raise YelpAPIError("timeout") from exc
                self._sleep(min(2 ** (attempt - 1), 5))
                continue
            if response.status_code not in retryable_statuses or attempt >= self.max_attempts:
                return response
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after is not None else 2 ** (attempt - 1)
            except ValueError:
                delay = 2 ** (attempt - 1)
            self._sleep(max(0.0, min(delay, 15.0)))
        raise AssertionError("retry loop did not return")


def select_yelp_business_match(
    payload: dict[str, Any], expected: YelpMatchInput
) -> YelpMatchSelection:
    raw_businesses = payload.get("businesses")
    businesses = raw_businesses if isinstance(raw_businesses, list) else []
    if not businesses:
        return YelpMatchSelection("not_found", "not_found")

    valid: list[tuple[dict[str, Any], float, float]] = []
    rejected_reasons: list[str] = []
    for value in businesses:
        if not isinstance(value, dict):
            rejected_reasons.append("invalid_candidate")
            continue
        accepted, reason, distance, confidence = _validate_candidate(value, expected)
        if accepted:
            valid.append((value, distance, confidence))
        else:
            rejected_reasons.append(reason)

    if len(valid) > 1:
        return YelpMatchSelection("ambiguous", "ambiguous_multiple_candidates")
    if not valid:
        reason = _dominant_rejection_reason(rejected_reasons)
        return YelpMatchSelection("rejected", f"rejected_{reason}")

    business, _, confidence = valid[0]
    business_id = _text(business.get("id"))
    if not business_id or not YELP_BUSINESS_ID.fullmatch(business_id):
        return YelpMatchSelection("rejected", "rejected_invalid_identity")
    return YelpMatchSelection(
        "matched",
        "matched",
        provider_place_id=business_id,
        confidence=confidence,
    )


def _validate_candidate(
    candidate: dict[str, Any], expected: YelpMatchInput
) -> tuple[bool, str, float, float]:
    business_id = _text(candidate.get("id"))
    if not business_id or not YELP_BUSINESS_ID.fullmatch(business_id):
        return False, "invalid_identity", float("inf"), 0.0
    if candidate.get("is_closed") is True:
        return False, "closed", float("inf"), 0.0

    coordinates = candidate.get("coordinates")
    coordinates = coordinates if isinstance(coordinates, dict) else {}
    latitude = _finite_float(coordinates.get("latitude"))
    longitude = _finite_float(coordinates.get("longitude"))
    if latitude is None or longitude is None:
        return False, "missing_coordinates", float("inf"), 0.0
    distance = haversine_meters(
        expected.latitude,
        expected.longitude,
        latitude,
        longitude,
    )
    if distance > 100:
        return False, "location_mismatch", distance, 0.0

    provider_name = _text(candidate.get("name"))
    if not provider_name:
        return False, "name_mismatch", distance, 0.0
    name_score = similarity(normalize_name(expected.name), normalize_name(provider_name))
    name_compatible = _names_are_compatible(expected.name, provider_name)

    location = candidate.get("location")
    location = location if isinstance(location, dict) else {}
    provider_address = _text(location.get("address1"))
    address_score = similarity(
        normalize_address(expected.address),
        normalize_address(provider_address),
    )
    provider_phone = normalize_phone(_text(candidate.get("phone")), expected.country_code)
    exact_phone = bool(
        expected.phone_e164
        and provider_phone
        and expected.phone_e164 == provider_phone
    )

    # Exact/near-exact names are sufficient only at a tight physical location.
    # Looser brand-name compatibility requires an independent address or phone
    # signal. This intentionally trades coverage for false-positive resistance.
    strong_name = name_score >= 0.90
    corroborated_name = name_compatible and (address_score >= 0.86 or exact_phone)
    if not ((strong_name and distance <= 75) or corroborated_name):
        return False, "name_mismatch", distance, 0.0
    if not _has_supported_category(candidate.get("categories")):
        return False, "type_mismatch", distance, 0.0

    if exact_phone:
        confidence = 0.995
    elif address_score >= 0.96 and distance <= 50:
        confidence = 0.99
    elif strong_name and distance <= 50:
        confidence = 0.98
    else:
        confidence = 0.97
    return True, "matched", distance, confidence


def _names_are_compatible(left: str, right: str) -> bool:
    left_tokens = _name_tokens(left)
    right_tokens = _name_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    left_joined = " ".join(left_tokens)
    right_joined = " ".join(right_tokens)
    if (
        left_joined == right_joined
        or left_joined in right_joined
        or right_joined in left_joined
    ):
        return True
    overlap = len(set(left_tokens) & set(right_tokens))
    return overlap / min(len(left_tokens), len(right_tokens)) >= 0.5


def _name_tokens(value: str) -> list[str]:
    return [
        token
        for token in normalize_name(value).split()
        if token and token not in _IGNORED_NAME_TOKENS
    ]


def _has_supported_category(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    aliases: list[str] = []
    titles: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        alias = _text(item.get("alias"))
        title = _text(item.get("title"))
        if alias:
            aliases.append(alias.casefold())
        if title:
            titles.append(title.casefold())
    if any(alias in _ALCOHOL_CATEGORY_ALIASES for alias in aliases):
        return True
    return any(term in title for title in titles for term in _ALCOHOL_CATEGORY_TERMS)


def _dominant_rejection_reason(reasons: list[str]) -> str:
    priority = (
        "ambiguous",
        "name_mismatch",
        "location_mismatch",
        "type_mismatch",
        "closed",
        "invalid_identity",
        "missing_coordinates",
        "invalid_candidate",
    )
    return next((reason for reason in priority if reason in reasons), "unusable_candidate")


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    if status == 400:
        raise YelpAPIError("invalid_request")
    if status == 401:
        raise YelpAPIError("unauthorized")
    if status == 403:
        raise YelpAPIError("forbidden")
    if status == 404:
        raise YelpAPIError("not_found")
    if status == 429:
        raise YelpAPIError("rate_limited")
    raise YelpAPIError("unavailable")


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None
