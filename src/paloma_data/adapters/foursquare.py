from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timezone
import json
import re
from typing import Any

from paloma_data.adapters.overture import _validate_bbox
from paloma_data.models import SourceRecord
from paloma_data.taxonomy import classify_overture, is_consumer_facing_type


_FSQ_FIELDS = (
    "fsq_place_id",
    "name",
    "latitude",
    "longitude",
    "address",
    "locality",
    "region",
    "postcode",
    "country",
    "date_created",
    "date_refreshed",
    "date_closed",
    "tel",
    "website",
    "fsq_category_ids",
    "fsq_category_labels",
    "unresolved_flags",
)

_FSQ_RICH_FIELDS = (
    "hours",
    "price",
    "outdoorseating",
)


class FoursquareAdapter:
    """Read the open FSQ Places Iceberg table with portal-issued credentials.

    Foursquare publishes the data in bulk, so this is one bounded Bay Area scan per monthly
    release—not a per-venue API call. Stable place IDs and payload hashes make repeat scans
    incremental at Paloma's database boundary.
    """

    source = "fsq"

    def __init__(
        self,
        *,
        catalog_uri: str,
        catalog_token: str,
        table_name: str,
        bbox: str,
        warehouse: str | None = None,
    ) -> None:
        if not catalog_uri or not catalog_token or not table_name:
            raise ValueError(
                "FSQ_CATALOG_URI, FSQ_CATALOG_TOKEN, and FSQ_PLACES_TABLE are required"
            )
        self.catalog_uri = catalog_uri
        self.catalog_token = catalog_token
        self.table_name = table_name
        self.warehouse = warehouse
        self.bbox = _validate_bbox(bbox)

    def backfill(self) -> Iterator[SourceRecord]:
        yield from self._scan()

    def incremental(self, cursor: str | None = None) -> Iterator[SourceRecord]:
        # FSQ OS refreshes monthly. Scanning only the Bay Area columns/rows is cheap and avoids
        # coupling catalog correctness to optional delta-table naming in the Places Portal.
        yield from self._scan()

    def _scan(self) -> Iterator[SourceRecord]:
        try:
            from pyiceberg.catalog import load_catalog
            from pyiceberg.expressions import And, GreaterThanOrEqual, LessThanOrEqual
        except ImportError as exc:  # pragma: no cover - exercised only in an operator environment
            raise RuntimeError("Install paloma-data[fsq] to read FSQ Open Source Places") from exc

        west, south, east, north = (float(part) for part in self.bbox.split(","))
        catalog_options: dict[str, str] = {
            "type": "rest",
            "uri": self.catalog_uri,
            "token": self.catalog_token,
        }
        if self.warehouse:
            catalog_options["warehouse"] = self.warehouse

        catalog = load_catalog("fsq", **catalog_options)
        table = catalog.load_table(self.table_name)
        available = {field.name for field in table.schema().fields}
        selected_fields = tuple(
            field for field in (*_FSQ_FIELDS, *_FSQ_RICH_FIELDS) if field in available
        )
        row_filter = And(
            And(GreaterThanOrEqual("longitude", west), LessThanOrEqual("longitude", east)),
            And(GreaterThanOrEqual("latitude", south), LessThanOrEqual("latitude", north)),
        )
        scan = table.scan(row_filter=row_filter, selected_fields=selected_fields)
        for batch in scan.to_arrow_batch_reader():
            for row in batch.to_pylist():
                record = self._to_record(row)
                if record is not None:
                    yield record

    def _to_record(self, row: dict[str, Any]) -> SourceRecord | None:
        source_id = row.get("fsq_place_id")
        name = _text(row.get("name"))
        address = _text(row.get("address"))
        city = _text(row.get("locality"))
        latitude = _float_or_none(row.get("latitude"))
        longitude = _float_or_none(row.get("longitude"))
        if not source_id or not name or not address or not city:
            return None
        if latitude is None or longitude is None:
            return None

        category_labels = _string_list(row.get("fsq_category_labels"))
        category_tokens = _category_tokens(category_labels)
        classification = classify_overture(name, category_tokens, 0.94)
        if not classification.eligible:
            return None

        quality_flags = _quality_flags(row.get("unresolved_flags"))
        closed_at = _datetime_or_none(row.get("date_closed"))
        hard_closed = bool(
            {"closed", "delete", "doesnt_exist", "does_not_exist"} & set(quality_flags)
        )
        status = "closed" if closed_at or hard_closed else "open"
        consumer_facing = is_consumer_facing_type(classification.primary_type_slug)
        private = "privatevenue" in quality_flags or "private_venue" in quality_flags
        hours = _hours(row.get("hours"))
        price_level = _price_level(row.get("price"))
        setting_slugs = _objective_settings(row, category_labels)

        return SourceRecord(
            source=self.source,
            source_record_id=str(source_id),
            name=name,
            address=address,
            city=city,
            region=_text(row.get("region")),
            postal_code=_text(row.get("postcode")),
            country_code=(_text(row.get("country")) or "US").upper(),
            latitude=latitude,
            longitude=longitude,
            phone=_first_text(row.get("tel")),
            website_url=_first_text(row.get("website")),
            hours=hours,
            price_level=price_level,
            setting_slugs=setting_slugs,
            source_status=status,
            source_updated_at=_datetime_or_none(row.get("date_refreshed")),
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
            quality_flags=quality_flags,
            category_evidence={
                "reason": classification.reason.replace("overture_", "fsq_"),
                "category_labels": category_labels,
                "category_ids": _string_list(row.get("fsq_category_ids")),
            },
            permitted_metadata={
                "date_created": _iso_or_none(row.get("date_created")),
                "date_refreshed": _iso_or_none(row.get("date_refreshed")),
                "date_closed": _iso_or_none(row.get("date_closed")),
            },
        )


def _category_tokens(labels: list[str]) -> set[str]:
    tokens: set[str] = set()
    for label in labels:
        for part in re.split(r"\s*(?:>|/|›)\s*", label):
            token = re.sub(r"[^a-z0-9]+", "_", part.casefold()).strip("_")
            if token:
                tokens.add(token)
    return tokens


def _quality_flags(value: Any) -> tuple[str, ...]:
    if isinstance(value, dict):
        values = [key for key, enabled in value.items() if enabled]
    else:
        values = _string_list(value)
    aliases = {
        "doesntexist": "doesnt_exist",
        "doesnotexist": "does_not_exist",
        "privatevenue": "privatevenue",
    }
    normalized: set[str] = set()
    for raw in values:
        token = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")
        compact = token.replace("_", "")
        normalized.add(aliases.get(compact, token))
    return tuple(sorted(normalized))


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_text(value: Any) -> str | None:
    values = _string_list(value)
    return values[0] if values else None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _hours(value: Any) -> dict[str, Any] | list[Any] | str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    return parsed if isinstance(parsed, (dict, list, str)) else text


def _price_level(value: Any) -> int | None:
    aliases = {
        "cheap": 1,
        "moderate": 2,
        "expensive": 3,
        "very expensive": 4,
    }
    if value is None:
        return None
    if isinstance(value, (int, float)) and int(value) in range(1, 5):
        return int(value)
    return aliases.get(str(value).strip().casefold())


def _objective_settings(row: dict[str, Any], category_labels: list[str]) -> tuple[str, ...]:
    settings: set[str] = set()
    if row.get("outdoorseating") is True:
        settings.add("outdoor_patio")
    labels = " ".join(category_labels).casefold()
    if "hotel bar" in labels:
        settings.add("hotel")
    if "rooftop" in labels:
        settings.add("rooftop")
    return tuple(sorted(settings))


def _datetime_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_or_none(value: Any) -> str | None:
    parsed = _datetime_or_none(value)
    return parsed.isoformat() if parsed else None
