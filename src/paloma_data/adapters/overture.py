from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import Any

from paloma_data.models import SourceRecord
from paloma_data.taxonomy import classify_overture


class OvertureAdapter:
    source = "overture"

    def __init__(self, bbox: str) -> None:
        self.bbox = _validate_bbox(bbox)

    def backfill(self) -> Iterator[SourceRecord]:
        # The official Overture CLI discovers the latest release through STAC and transfers only
        # the requested bbox. GeoJSON Sequence lets us stream the result without loading it all.
        with TemporaryDirectory(prefix="paloma-overture-") as directory:
            output = Path(directory) / "places.geojsonseq"
            subprocess.run(
                [
                    "overturemaps",
                    "download",
                    f"--bbox={self.bbox}",
                    "-f",
                    "geojsonseq",
                    "--type=place",
                    "-o",
                    str(output),
                ],
                check=True,
            )
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
        websites = properties.get("websites") or []
        phones = properties.get("phones") or []
        status = _canonical_status(properties.get("operating_status"))

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
            source_status=status,
            source_updated_at=_latest_update_time(sources),
            primary_type_slug=classification.primary_type_slug,
            classification_confidence=classification.confidence,
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
            parsed.append(datetime.fromisoformat(str(item["update_time"]).replace("Z", "+00:00")))
        except ValueError:
            continue
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
