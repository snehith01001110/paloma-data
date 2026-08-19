from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any
from xml.etree import ElementTree

import httpx
from overturemaps.core import record_batch_reader
from overturemaps.writers import copy, get_writer

from paloma_data.models import SourceRecord
from paloma_data.taxonomy import ACCESS_SPECIFIC_TYPES, classify_overture


_RELEASE_INDEX_URL = (
    "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
    "?list-type=2&delimiter=%2F&prefix=release%2F"
)
_RELEASE_PREFIX = re.compile(r"release/(\d{4}-\d{2}-\d{2})\.(\d+)/")


class OvertureAdapter:
    source = "overture"

    def __init__(self, bbox: str) -> None:
        self.bbox = _validate_bbox(bbox)

    def backfill(self) -> Iterator[SourceRecord]:
        # Discover releases from Overture's public bucket rather than its optional STAC index.
        # The latter has had breaking URL transitions. The official client still streams and
        # filters the requested bbox directly from the same Overture release bucket.
        with TemporaryDirectory(prefix="paloma-overture-") as directory:
            output = Path(directory) / "places.geojsonseq"
            release = _latest_release()
            _download_release(output, self.bbox, release)
            with output.open("r", encoding="utf-8") as handle:
                for line in handle:
                    payload = line.lstrip("\x1e").strip()
                    if not payload:
                        continue
                    feature = json.loads(payload)
                    record = self._to_record(feature)
                    if record is not None:
                        yield record

    def incremental(self, cursor: str | None = None) -> Iterator[SourceRecord]:
        # Overture releases monthly and increments feature version when geometry/attributes change.
        # Re-downloading only the Bay Area bbox is still cheap; stable IDs + version/payload hashes
        # make the database side incremental without scanning or rewriting unchanged canonicals.
        yield from self.backfill()

    def _to_record(self, feature: dict[str, Any]) -> SourceRecord | None:
        properties = feature.get("properties") or {}
        source_id = properties.get("id") or feature.get("id")
        names = properties.get("names") or {}
        name = names.get("primary") if isinstance(names, dict) else None
        if not source_id or not name:
            return None

        taxonomy = properties.get("taxonomy") or {}
        categories = properties.get("categories") or {}
        category_tokens = _category_tokens(
            properties.get("basic_category"), taxonomy, categories
        )
        existence_confidence = _float_or_none(properties.get("confidence"))
        classification = classify_overture(name, category_tokens, existence_confidence)
        if not classification.eligible:
            return None

        address = _best_address(properties.get("addresses"))
        if address is None:
            return None
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            return None
        try:
            longitude = float(coordinates[0])
            latitude = float(coordinates[1])
        except (TypeError, ValueError):
            return None

        sources = properties.get("sources") or []
        source_datasets = sorted(
            {
                str(item.get("dataset"))
                for item in sources
                if isinstance(item, dict) and item.get("dataset")
            }
        )
        origin_keys = _origin_keys(source_datasets)
        field_provenance = _field_provenance(sources)
        websites = properties.get("websites") or []
        phones = properties.get("phones") or []
        status = _canonical_status(properties.get("operating_status"))
        setting_slugs = _objective_settings(category_tokens)

        return SourceRecord(
            source=self.source,
            source_record_id=str(source_id),
            name=str(name).strip(),
            address=address["freeform"],
            city=address["locality"],
            region=address.get("region"),
            postal_code=address.get("postcode"),
            country_code=address.get("country") or "US",
            latitude=latitude,
            longitude=longitude,
            phone=_first_string(phones),
            website_url=_first_string(websites),
            setting_slugs=setting_slugs,
            source_status=status,
            source_updated_at=_latest_update_time(sources),
            primary_type_slug=classification.primary_type_slug,
            classification_confidence=classification.confidence,
            source_family="consumer_poi",
            consumer_facing=classification.primary_type_slug in ACCESS_SPECIFIC_TYPES,
            public_access=(
                "walk_in"
                if classification.primary_type_slug in ACCESS_SPECIFIC_TYPES
                else "unknown"
            ),
            origin_keys=origin_keys,
            data_license="Overture-source-licenses",
            category_evidence={
                "reason": classification.reason,
                "basic_category": properties.get("basic_category"),
                "taxonomy": taxonomy,
                "legacy_categories": categories,
                "overture_confidence": existence_confidence,
            },
            permitted_metadata={
                "version": properties.get("version"),
                "overture_confidence": existence_confidence,
                "operating_status": properties.get("operating_status"),
                "source_datasets": source_datasets,
            },
            field_provenance=field_provenance,
        )


def _validate_bbox(value: str) -> str:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("PALOMA_OVERTURE_BBOX must be west,south,east,north")
    numbers = [float(part) for part in parts]
    west, south, east, north = numbers
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("Invalid PALOMA_OVERTURE_BBOX bounds")
    return ",".join(str(number) for number in numbers)


def _latest_release() -> str:
    with httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": "paloma-data/0.2"},
    ) as client:
        response = client.get(_RELEASE_INDEX_URL)
        response.raise_for_status()
    return _latest_release_from_index(response.text)


def _latest_release_from_index(document: str) -> str:
    root = ElementTree.fromstring(document)
    releases: list[tuple[date, int, str]] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "Prefix" or not element.text:
            continue
        match = _RELEASE_PREFIX.fullmatch(element.text)
        if match is None:
            continue
        release = f"{match.group(1)}.{match.group(2)}"
        releases.append((date.fromisoformat(match.group(1)), int(match.group(2)), release))
    if not releases:
        raise RuntimeError("Overture's public release bucket did not list an available release")
    return max(releases)[2]


def _download_release(output: Path, bbox: str, release: str) -> None:
    _download_feature(output, bbox, release, "place")


def _download_feature(output: Path, bbox: str, release: str, feature_type: str) -> None:
    reader = record_batch_reader(
        feature_type,
        bbox=[float(value) for value in bbox.split(",")],
        release=release,
        stac=False,
    )
    if reader is None:
        raise RuntimeError(f"Overture returned no reader for release {release}")
    with get_writer("geojsonseq", str(output), schema=reader.schema) as writer:
        copy(reader, writer)


def _category_tokens(basic: Any, taxonomy: Any, categories: Any) -> set[str]:
    tokens: set[str] = set()
    if basic:
        tokens.add(str(basic))
    if isinstance(taxonomy, dict):
        if taxonomy.get("primary"):
            tokens.add(str(taxonomy["primary"]))
        for key in ("hierarchy", "alternates", "alternate"):
            values = taxonomy.get(key) or []
            if isinstance(values, list):
                tokens.update(str(value) for value in values if value)
    if isinstance(categories, dict):
        if categories.get("primary"):
            tokens.add(str(categories["primary"]))
        for key in ("alternate", "alternates"):
            values = categories.get(key) or []
            if isinstance(values, list):
                tokens.update(str(value) for value in values if value)
    return tokens


def _best_address(value: Any) -> dict[str, str] | None:
    if not isinstance(value, list):
        return None
    candidates = [item for item in value if isinstance(item, dict)]
    candidates.sort(key=lambda item: item.get("country") != "US")
    for item in candidates:
        freeform = item.get("freeform")
        locality = item.get("locality")
        if freeform and locality:
            return {
                "freeform": str(freeform),
                "locality": str(locality),
                "postcode": str(item["postcode"]) if item.get("postcode") else None,
                "region": str(item["region"]) if item.get("region") else None,
                "country": str(item["country"]) if item.get("country") else "US",
            }
    return None


def _canonical_status(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).casefold()
    if "tempor" in text and "closed" in text:
        return "temporarily_closed"
    if "closed" in text:
        return "closed"
    if "open" in text:
        return "open"
    return None


def _latest_update_time(value: Any) -> datetime | None:
    if not isinstance(value, list):
        return None
    parsed: list[datetime] = []
    for item in value:
        if not isinstance(item, dict) or not item.get("update_time"):
            continue
        try:
            timestamp = datetime.fromisoformat(str(item["update_time"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        parsed.append(timestamp)
    return max(parsed) if parsed else None


def _first_string(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            if item:
                return str(item)
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _objective_settings(category_tokens: set[str]) -> tuple[str, ...]:
    settings: set[str] = set()
    if category_tokens & {"hotel_bar", "hotel_lounge"}:
        settings.add("hotel")
    if category_tokens & {"rooftop_bar", "rooftop_lounge"}:
        settings.add("rooftop")
    if "brewpub" in category_tokens:
        settings.update({"production_premises", "restaurant_attached"})
    return tuple(sorted(settings))


def _origin_keys(source_datasets: list[str]) -> tuple[str, ...]:
    """Normalize Overture lineage so copied providers are not independent evidence."""
    origins: set[str] = set()
    for dataset in source_datasets:
        token = dataset.casefold()
        if "foursquare" in token or token.startswith("fsq"):
            origins.add("foursquare")
        elif "facebook" in token or "meta" in token:
            origins.add("meta")
        elif "microsoft" in token or "bing" in token:
            origins.add("microsoft")
        elif "openstreetmap" in token or token == "osm":
            origins.add("openstreetmap")
        else:
            origins.add(f"overture:{token}")
    return tuple(sorted(origins or {"overture:unknown"}))


_OVERTURE_FIELD_PATHS = {
    "display_name": ("/names",),
    "address": ("/addresses",),
    "latitude": ("/geometry",),
    "longitude": ("/geometry",),
    "phone_e164": ("/phones",),
    "website_url": ("/websites",),
    "primary_type_slug": ("/basic_category", "/taxonomy", "/categories"),
    "operating_status": ("/operating_status",),
    "setting_slug": ("/basic_category", "/taxonomy", "/categories"),
}


def _field_provenance(value: Any) -> dict[str, Any]:
    """Preserve SourceItem lineage at the property granularity Overture publishes."""
    if not isinstance(value, list):
        return {}
    source_items = [_source_item(item) for item in value if isinstance(item, dict)]
    source_items = [item for item in source_items if item is not None]
    result: dict[str, Any] = {}
    for field_name, paths in _OVERTURE_FIELD_PATHS.items():
        relevant = [
            item
            for item in source_items
            if item["property"] is None
            or any(str(item["property"]).startswith(path) for path in paths)
        ]
        if not relevant:
            continue
        datasets = sorted({str(item["dataset"]) for item in relevant})
        result[field_name] = {
            "origin_keys": list(_origin_keys(datasets)),
            "license_ids": sorted({str(item["license"]) for item in relevant}),
            "source_items": relevant,
        }
    return result


def _source_item(value: dict[str, Any]) -> dict[str, Any] | None:
    dataset = value.get("dataset")
    if not dataset:
        return None
    dataset_text = str(dataset)
    return {
        "dataset": dataset_text,
        "record_id": str(value["record_id"]) if value.get("record_id") else None,
        "property": str(value["property"]) if value.get("property") else None,
        "license": str(value.get("license") or _overture_license(dataset_text)),
        "update_time": str(value["update_time"]) if value.get("update_time") else None,
    }


def _overture_license(dataset: str) -> str:
    token = dataset.casefold()
    if "foursquare" in token or token.startswith("fsq"):
        return "Apache-2.0"
    if "alltheplaces" in token or token in {"overture", "overturemaps"}:
        return "CC0-1.0"
    if any(
        provider in token
        for provider in (
            "brightquery",
            "dac",
            "krick",
            "meta",
            "microsoft",
            "pinmeto",
            "renderseo",
        )
    ):
        return "CDLA-Permissive-2.0"
    return "unknown"
