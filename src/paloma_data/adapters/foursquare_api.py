from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any

import httpx

from paloma_data.adapters.foursquare import (
    _category_tokens,
    _consumer_identity_is_supported,
    _datetime_or_none,
    _first_text,
    _hours,
    _objective_settings,
    _price_level,
    _quality_flags,
    _string_list,
    _text,
)
from paloma_data.models import SourceRecord
from paloma_data.taxonomy import classify_overture


PLACES_API_VERSION = "2025-06-17"
DEFAULT_FIELDS = (
    "fsq_place_id",
    "name",
    "latitude",
    "longitude",
    "location",
    "categories",
    "date_closed",
    "unresolved_flags",
    "tel",
    "website",
    "hours",
    "price",
    "attributes",
    "veracity_rating",
)


class FoursquarePlaceUnusableError(RuntimeError):
    """A successful provider response that cannot identify a consumer place."""


class FoursquarePlacesAPI:
    """Targeted current-place verifier, never a bulk discovery crawler.

    Foursquare's self-service retention rules prohibit server caching of attributes other than
    IDs. API content is persisted only under a written agreement that expressly overrides that
    rule. Otherwise callers may use results for a bounded in-memory trial, but must not stage or
    materialize the returned fields.
    """

    source = "fsq_premium"

    def __init__(
        self,
        service_key: str,
        *,
        storage_policy: str = "ephemeral",
        base_url: str = "https://places-api.foursquare.com",
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        sleeper=time.sleep,
    ) -> None:
        if not service_key:
            raise ValueError("FSQ_PLACES_API_KEY is required")
        if storage_policy not in {"contract", "ephemeral"}:
            raise ValueError("Foursquare API storage_policy must be contract or ephemeral")
        self.storage_policy = storage_policy
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.max_attempts = max_attempts
        self._sleep = sleeper
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "Authorization": f"Bearer {service_key}",
                "X-Places-Api-Version": PLACES_API_VERSION,
                "Accept": "application/json",
                "User-Agent": "paloma-data/0.4",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "FoursquarePlacesAPI":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def details(self, fsq_place_id: str) -> SourceRecord | None:
        response = self._get(
            f"/places/{fsq_place_id}",
            params={"fields": ",".join(DEFAULT_FIELDS), "tel_format": "E164"},
        )
        if response.status_code == 404:
            return None
        _raise_for_status(response)
        record = self._to_record(_unwrap_place(response.json()))
        if record is None:
            raise FoursquarePlaceUnusableError(
                f"Foursquare details for {fsq_place_id} lacked required identity fields"
            )
        return record

    def search(
        self,
        *,
        query: str,
        latitude: float,
        longitude: float,
        radius_m: int = 100,
        limit: int = 5,
    ) -> list[SourceRecord]:
        response = self._get(
            "/places/search",
            params={
                "query": query,
                "ll": f"{latitude},{longitude}",
                "radius": max(1, min(radius_m, 100_000)),
                "limit": max(1, min(limit, 50)),
                "fields": ",".join(DEFAULT_FIELDS),
                "tel_format": "E164",
            },
        )
        _raise_for_status(response)
        payload = response.json()
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        return [record for row in rows if (record := self._to_record(row)) is not None]

    def _get(self, path: str, *, params: dict[str, Any]) -> httpx.Response:
        """Retry only transport errors, throttling, and transient server failures."""
        retryable_statuses = {429, 500, 502, 503, 504}
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= self.max_attempts:
                    raise
                self._sleep(min(2 ** (attempt - 1), 10))
                continue
            if response.status_code not in retryable_statuses or attempt >= self.max_attempts:
                return response
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after is not None else 2 ** (attempt - 1)
            except ValueError:
                delay = 2 ** (attempt - 1)
            self._sleep(max(0.0, min(delay, 30.0)))
        raise AssertionError("retry loop did not return")

    def _to_record(self, payload: dict[str, Any]) -> SourceRecord | None:
        source_id = _text(payload.get("fsq_place_id") or payload.get("fsq_id"))
        name = _text(payload.get("name"))
        location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
        latitude, longitude = _coordinates(payload)
        address = _text(
            payload.get("address")
            or location.get("address")
            or location.get("formatted_address")
        )
        city = _text(payload.get("locality") or location.get("locality"))
        if not all((source_id, name, address, city)) or latitude is None or longitude is None:
            return None

        category_ids, category_labels = _categories(payload)
        classification = classify_overture(name, _category_tokens(category_labels), 0.99)
        if not classification.eligible:
            return None

        flags = _quality_flags(payload.get("unresolved_flags"))
        closed_at = _datetime_or_none(payload.get("date_closed"))
        hard_closed = bool(
            {"closed", "delete", "doesnt_exist", "does_not_exist"} & set(flags)
        )
        consumer_facing = _consumer_identity_is_supported(
            name,
            _category_tokens(category_labels),
            classification.primary_type_slug,
            classification.reason,
        )
        private = bool({"privatevenue", "private_venue"} & set(flags))
        attributes = payload.get("attributes")
        hours = _api_hours(payload.get("hours"))
        price = _price_level(payload.get("price"))
        provider_veracity = _int_or_none(payload.get("veracity_rating"))
        observed_at = datetime.now(timezone.utc)

        settings = set(_objective_settings({}, category_labels))
        attribute_values = _attribute_values(attributes)
        if _truthy(
            payload.get("outdoorseating")
            if "outdoorseating" in payload
            else attribute_values.get("outdoorseating")
        ):
            settings.add("outdoor_patio")
        labels_text = " ".join(category_labels).casefold()
        if "restaurant" in labels_text and classification.primary_type_slug in {
            "bar",
            "cocktail_bar",
            "wine_bar",
            "beer_bar",
            "sports_bar",
            "pub",
            "lounge",
        }:
            settings.add("restaurant_attached")
        if classification.primary_type_slug in {"brewery", "winery", "distillery"}:
            settings.add("production_premises")
        if "vineyard" in labels_text:
            settings.add("vineyard")
        if "beer garden" in labels_text or "garden bar" in labels_text:
            settings.add("garden")

        return SourceRecord(
            source=self.source,
            source_record_id=source_id,
            name=name,
            address=address,
            city=city,
            region=_text(payload.get("region") or location.get("region")),
            postal_code=_text(payload.get("postcode") or location.get("postcode")),
            country_code=(
                _text(payload.get("country") or location.get("country")) or "US"
            ).upper(),
            latitude=latitude,
            longitude=longitude,
            phone=_first_text(payload.get("tel")),
            website_url=_first_text(payload.get("website")),
            neighborhood=_first_text(
                payload.get("neighborhoods") or location.get("neighborhood")
            ),
            hours=hours,
            price_level=price,
            setting_slugs=tuple(sorted(settings)),
            source_status="closed" if closed_at or hard_closed else "open",
            source_updated_at=observed_at,
            primary_type_slug=classification.primary_type_slug,
            classification_confidence=classification.confidence,
            source_family="consumer_poi",
            consumer_facing=consumer_facing,
            public_access=(
                "members_or_private"
                if private
                else "walk_in"
                if consumer_facing
                else "unknown"
            ),
            quality_flags=flags,
            origin_keys=("foursquare",),
            data_license=(
                "Foursquare-contract"
                if self.storage_policy == "contract"
                else "Foursquare-API-ephemeral"
            ),
            storage_scope=self.storage_policy,
            provider_veracity=provider_veracity,
            category_evidence={
                "reason": classification.reason.replace("overture_", "fsq_api_"),
                "category_ids": category_ids,
                "category_labels": category_labels,
            },
            permitted_metadata={
                "api_observed_at": observed_at.isoformat(),
                "api_version": PLACES_API_VERSION,
                "storage_authorized": self.storage_policy == "contract",
                "date_closed": closed_at.isoformat() if closed_at else None,
            },
        )


def _raise_for_status(response: httpx.Response) -> None:
    """Raise a useful error without logging request URLs, keys, or place content."""
    if response.is_success:
        return
    message: str | None = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = _text(error.get("message") or error.get("detail"))
        elif isinstance(error, str):
            message = _text(error)
        message = message or _text(payload.get("message") or payload.get("detail"))
        errors = payload.get("errors")
        if not message and isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                message = _text(first.get("message") or first.get("detail"))
            elif isinstance(first, str):
                message = _text(first)
    suffix = f": {message}" if message else ""
    raise RuntimeError(f"Foursquare Places API returned HTTP {response.status_code}{suffix}")


def _unwrap_place(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("place"), dict):
        return payload["place"]
    return payload


def _coordinates(payload: dict[str, Any]) -> tuple[float | None, float | None]:
    latitude = _float(payload.get("latitude"))
    longitude = _float(payload.get("longitude"))
    if latitude is not None and longitude is not None:
        return latitude, longitude
    geocodes = payload.get("geocodes") if isinstance(payload.get("geocodes"), dict) else {}
    point = geocodes.get("main") if isinstance(geocodes.get("main"), dict) else {}
    return _float(point.get("latitude")), _float(point.get("longitude"))


def _categories(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    ids = _string_list(payload.get("fsq_category_ids"))
    labels = _string_list(payload.get("fsq_category_labels"))
    values = payload.get("categories")
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, dict):
                continue
            category_id = _text(value.get("id") or value.get("fsq_category_id"))
            label = _text(value.get("label") or value.get("name") or value.get("short_name"))
            if category_id:
                ids.append(category_id)
            if label:
                labels.append(label)
    return list(dict.fromkeys(ids)), list(dict.fromkeys(labels))


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 1 <= parsed <= 5 else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "t"}


def _attribute_values(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, str):
                items.append((item, True))
            elif isinstance(item, dict):
                key = item.get("id") or item.get("name") or item.get("key")
                if key:
                    items.append((str(key), item.get("value", True)))
    else:
        items = []
    for key, item in items:
        normalized = "".join(
            character for character in str(key).casefold() if character.isalnum()
        )
        result[normalized] = item
    return result


def _api_hours(value: Any) -> dict[str, list[list[str]]] | str | None:
    parsed = _hours(value)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("regular"), list):
        return parsed
    days = {
        1: "monday",
        2: "tuesday",
        3: "wednesday",
        4: "thursday",
        5: "friday",
        6: "saturday",
        7: "sunday",
    }
    schedule: dict[str, list[list[str]]] = {}
    for item in parsed["regular"]:
        if not isinstance(item, dict):
            continue
        try:
            day = days[int(item.get("day"))]
        except (KeyError, TypeError, ValueError):
            continue
        opened = _clock(item.get("open"))
        closed = _clock(item.get("close"))
        if opened and closed:
            schedule.setdefault(day, []).append([opened, closed])
    return schedule or None


def _clock(value: Any) -> str | None:
    text = str(value or "").strip()
    prefix = "+" if text.startswith("+") else ""
    digits = text.lstrip("+").replace(":", "")
    if len(digits) != 4 or not digits.isdigit():
        return None
    return f"{prefix}{digits[:2]}:{digits[2:]}"
