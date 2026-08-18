from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from paloma_data.adapters.overture import _validate_bbox


@dataclass(frozen=True, slots=True)
class OSMAttributeObservation:
    source_record_id: str
    name: str
    latitude: float
    longitude: float
    phone: str | None
    website_url: str | None
    hours: str | None
    setting_slugs: tuple[str, ...]
    source_updated_at: datetime | None


class OSMAttributeAdapter:
    """Fetch one bounded OpenStreetMap attribute snapshot without creating venues."""

    source = "osm"
    # The OSM wiki lists these as global public instances. One weekly bounded query is far below
    # their published usage limits; fail over instead of turning a transient 504 into zero hours.
    _FALLBACK_ENDPOINTS = (
        "https://overpass.private.coffee/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    )

    def __init__(
        self,
        bbox: str,
        endpoint: str = "https://overpass-api.de/api/interpreter",
    ) -> None:
        self.bbox = _validate_bbox(bbox)
        self.endpoint = endpoint

    def observations(self) -> Iterator[OSMAttributeObservation]:
        with httpx.Client(
            timeout=httpx.Timeout(240.0, connect=20.0),
            headers={
                "User-Agent": "paloma-data/0.3 (weekly catalog attributes; github.com/snehith01001110/paloma-data)"
            },
        ) as client:
            payload = self._fetch_payload(client)
        for element in payload.get("elements") or []:
            observation = self._to_observation(element)
            if observation is not None:
                yield observation

    def _fetch_payload(self, client: httpx.Client) -> dict[str, Any]:
        errors: list[tuple[str, Exception]] = []
        for endpoint in dict.fromkeys((self.endpoint, *self._FALLBACK_ENDPOINTS)):
            try:
                response = client.post(endpoint, data={"data": self._query()})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Overpass response is not a JSON object")
                return payload
            except (httpx.HTTPError, ValueError) as exc:
                errors.append((endpoint, exc))
        detail = "; ".join(f"{endpoint}: {error}" for endpoint, error in errors)
        raise RuntimeError(f"all configured Overpass instances failed: {detail}")

    def _query(self) -> str:
        west, south, east, north = self.bbox.split(",")
        box = f"{south},{west},{north},{east}"
        return f"""[out:json][timeout:180];
(
  nwr[\"amenity\"~\"^(bar|pub|biergarten|nightclub)$\"]({box});
  nwr[\"craft\"~\"^(brewery|winery|distillery)$\"]({box});
  nwr[\"industrial\"~\"^(brewery|winery|distillery)$\"]({box});
  nwr[\"microbrewery\"=\"yes\"]({box});
);
out center tags meta qt;"""

    def _to_observation(self, element: dict[str, Any]) -> OSMAttributeObservation | None:
        tags = element.get("tags") or {}
        name = _text(tags.get("name"))
        element_type = _text(element.get("type"))
        element_id = element.get("id")
        if not name or not element_type or element_id is None:
            return None

        center = element.get("center") or element
        try:
            latitude = float(center["lat"])
            longitude = float(center["lon"])
        except (KeyError, TypeError, ValueError):
            return None

        return OSMAttributeObservation(
            source_record_id=f"{element_type}/{element_id}",
            name=name,
            latitude=latitude,
            longitude=longitude,
            phone=_text(tags.get("contact:phone") or tags.get("phone")),
            website_url=_text(tags.get("contact:website") or tags.get("website")),
            hours=_text(tags.get("opening_hours")),
            setting_slugs=_objective_settings(tags),
            source_updated_at=_timestamp(element.get("timestamp")),
        )


def _objective_settings(tags: dict[str, Any]) -> tuple[str, ...]:
    settings: set[str] = set()
    outdoor = _text(tags.get("outdoor_seating"))
    if outdoor and outdoor.casefold() not in {"no", "none", "false", "0"}:
        settings.add("outdoor_patio")
        if "garden" in outdoor.casefold():
            settings.add("garden")

    location = " ".join(
        filter(
            None,
            (
                _text(tags.get("location")),
                _text(tags.get("level")),
                _text(tags.get("description")),
            ),
        )
    ).casefold()
    if "roof" in location:
        settings.add("rooftop")
    if "basement" in location or "underground" in location:
        settings.add("basement")
    if tags.get("historic") not in (None, "", "no"):
        settings.add("historic")
    if tags.get("tourism") in {"hotel", "hostel", "guest_house"}:
        settings.add("hotel")
    if tags.get("craft") in {"brewery", "winery", "distillery"} or tags.get(
        "industrial"
    ) in {"brewery", "winery", "distillery"}:
        settings.add("production_premises")
    return tuple(sorted(settings))


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
