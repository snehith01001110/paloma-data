from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx

from paloma_data.models import SourceRecord
from paloma_data.taxonomy import classify_datasf


class DataSFAdapter:
    source = "datasf"

    def __init__(self, dataset_id: str = "g8m3-pdis", page_size: int = 5000) -> None:
        self.dataset_id = dataset_id
        self.page_size = page_size
        self.base_url = f"https://data.sfgov.org/resource/{dataset_id}.json"

    def backfill(self) -> Iterator[SourceRecord]:
        offset = 0
        with httpx.Client(timeout=60.0, headers={"User-Agent": "paloma-data/0.1"}) as client:
            while True:
                response = client.get(
                    self.base_url,
                    params={"$limit": self.page_size, "$offset": offset, "$order": "uniqueid"},
                )
                response.raise_for_status()
                rows: list[dict[str, Any]] = response.json()
                if not rows:
                    break
                for row in rows:
                    record = self._to_record(row)
                    if record is not None:
                        yield record
                if len(rows) < self.page_size:
                    break
                offset += self.page_size

    def incremental(self, cursor: str | None = None) -> Iterator[SourceRecord]:
        # DataSF is updated daily but does not expose a reliable per-row modification watermark
        # in the documented schema. Re-read stable IDs and let ingest.source_records.payload_hash
        # discard unchanged rows. This is a free bulk source, not a paid per-place API.
        yield from self.backfill()

    def _to_record(self, row: dict[str, Any]) -> SourceRecord | None:
        name = _first(row, "dba_name", "ownership_name")
        address = _first(row, "full_business_address", "business_address")
        city = _first(row, "city") or "San Francisco"
        source_id = _first(row, "uniqueid", "ttxid")
        if not name or not address or not source_id:
            return None

        naics = _first(row, "naics_code", "naic_code", "naics")
        classification = classify_datasf(name, naics)
        if not classification.eligible:
            return None

        location = row.get("location") or row.get("business_location") or {}
        latitude, longitude = _coordinates(location)

        end_date = _first(row, "location_end_date", "dba_end_date")
        administratively_closed = str(row.get("administratively_closed", "")).strip().lower()
        status = "closed" if end_date or administratively_closed in {"true", "yes", "y", "1"} else "open"

        permitted = {
            "certificate_number": row.get("certificate_number"),
            "ttxid": row.get("ttxid"),
            "naics_code": naics,
            "location_start_date": row.get("location_start_date"),
            "location_end_date": row.get("location_end_date"),
            "administratively_closed": row.get("administratively_closed"),
            "data_as_of": row.get("data_as_of"),
        }

        return SourceRecord(
            source=self.source,
            source_record_id=str(source_id),
            name=name.strip(),
            address=address.strip(),
            city=city.strip(),
            region=(_first(row, "state") or "CA").strip(),
            postal_code=_first(row, "business_zip", "source_zipcode", "zip"),
            country_code="US",
            latitude=latitude,
            longitude=longitude,
            neighborhood=_first(
                row,
                "neighborhoods_analysis_boundaries",
                "analysis_neighborhood",
                "sf_find_neighborhoods",
            ),
            source_status=status,
            source_updated_at=_parse_date(
                _first(row, "data_as_of", "location_end_date", "location_start_date")
            ),
            primary_type_slug=classification.primary_type_slug,
            classification_confidence=classification.confidence,
            source_family="government_registry",
            consumer_facing=False,
            public_access="unknown",
            origin_keys=("datasf",),
            data_license="DataSF-open-data",
            category_evidence={"reason": classification.reason, "naics_code": naics},
            permitted_metadata=permitted,
        )


def _first(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _coordinates(value: Any) -> tuple[float | None, float | None]:
    if not isinstance(value, dict):
        return None, None
    coords = value.get("coordinates")
    if isinstance(coords, list) and len(coords) >= 2:
        try:
            return float(coords[1]), float(coords[0])
        except (TypeError, ValueError):
            pass
    lat = value.get("latitude")
    lon = value.get("longitude")
    try:
        return (float(lat), float(lon)) if lat is not None and lon is not None else (None, None)
    except (TypeError, ValueError):
        return None, None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
