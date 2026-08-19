from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
import re
from typing import Any

import httpx

from paloma_data.models import SourceRecord
from paloma_data.taxonomy import classify_name


_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
_POINT = re.compile(r"^Point\(([-0-9.]+) ([-0-9.]+)\)$")
_CLASSES = (
    "wd:Q187456",  # bar
    "wd:Q212198",  # pub
    "wd:Q622425",  # nightclub
    "wd:Q131734",  # brewery
    "wd:Q156362",  # winery
    "wd:Q271081",  # brewpub
)


class WikidataAdapter:
    """Small CC0 corroboration feed for established Bay Area drinking venues."""

    source = "wikidata"

    def __init__(self, bbox: str, endpoint: str = _SPARQL_ENDPOINT) -> None:
        self.west, self.south, self.east, self.north = _bbox(bbox)
        self.endpoint = endpoint

    def backfill(self) -> Iterator[SourceRecord]:
        query = _query(self.west, self.south, self.east, self.north)
        with httpx.Client(
            timeout=httpx.Timeout(90.0, connect=15.0),
            headers={
                "Accept": "application/sparql-results+json",
                "User-Agent": "PalomaData/0.4 (https://github.com/snehith01001110/paloma-data)",
            },
        ) as client:
            response = client.get(self.endpoint, params={"query": query, "format": "json"})
            response.raise_for_status()
            payload = response.json()
        rows = payload.get("results", {}).get("bindings", [])
        seen: set[str] = set()
        for row in rows:
            record = self._to_record(row)
            if record is None or record.source_record_id in seen:
                continue
            seen.add(record.source_record_id)
            yield record

    def incremental(self, cursor: str | None = None) -> Iterator[SourceRecord]:
        yield from self.backfill()

    def _to_record(self, row: dict[str, Any]) -> SourceRecord | None:
        item_url = _binding(row, "item")
        name = _binding(row, "itemLabel")
        address = _binding(row, "streetAddress")
        city = _binding(row, "adminLabel")
        point = _point(_binding(row, "coord"))
        if not item_url or not name or not address or not city or point is None:
            return None
        classification = classify_name(name)
        if not classification.eligible:
            return None
        source_id = item_url.rsplit("/", 1)[-1]
        modified = _timestamp(_binding(row, "modified"))
        latitude, longitude = point
        source_item = {
            "dataset": "wikidata",
            "record_id": source_id,
            "license": "CC0-1.0",
            "update_time": modified.isoformat() if modified else None,
        }
        fields = {
            key: {
                "origin_keys": ["wikidata"],
                "license_ids": ["CC0-1.0"],
                "source_items": [{**source_item, "property": property_id}],
            }
            for key, property_id in {
                "display_name": "P1448/P1476/label",
                "address": "P6375",
                "latitude": "P625",
                "longitude": "P625",
                "phone_e164": "P1329",
                "website_url": "P856",
                "primary_type_slug": "P31",
            }.items()
        }
        return SourceRecord(
            source=self.source,
            source_record_id=source_id,
            name=name,
            address=address,
            city=city,
            region="CA",
            country_code="US",
            latitude=latitude,
            longitude=longitude,
            phone=_binding(row, "phone"),
            website_url=_binding(row, "website"),
            source_status="open",
            source_updated_at=modified,
            primary_type_slug=classification.primary_type_slug,
            classification_confidence=min(0.88, classification.confidence),
            source_family="knowledge_graph",
            consumer_facing=True,
            public_access="unknown",
            origin_keys=("wikidata",),
            data_license="CC0-1.0",
            category_evidence={"reason": classification.reason},
            permitted_metadata={"entity_url": item_url},
            field_provenance=fields,
        )


def _query(west: float, south: float, east: float, north: float) -> str:
    classes = " ".join(_CLASSES)
    return f"""
PREFIX bd: <http://www.bigdata.com/rdf#>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX schema: <http://schema.org/>
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
SELECT DISTINCT ?item ?itemLabel ?coord ?streetAddress ?phone ?website ?modified ?adminLabel
WHERE {{
  VALUES ?class {{ {classes} }}
  ?item wdt:P31/wdt:P279* ?class;
        wdt:P6375 ?streetAddress;
        wdt:P131 ?admin.
  SERVICE wikibase:box {{
    ?item wdt:P625 ?coord.
    bd:serviceParam wikibase:cornerSouthWest "Point({west} {south})"^^geo:wktLiteral;
                    wikibase:cornerNorthEast "Point({east} {north})"^^geo:wktLiteral.
  }}
  OPTIONAL {{ ?item wdt:P1329 ?phone. }}
  OPTIONAL {{ ?item wdt:P856 ?website. }}
  OPTIONAL {{ ?item schema:dateModified ?modified. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY ?item
"""


def _binding(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if not isinstance(value, dict) or not value.get("value"):
        return None
    return str(value["value"]).strip() or None


def _point(value: str | None) -> tuple[float, float] | None:
    match = _POINT.fullmatch(value or "")
    if match is None:
        return None
    return float(match.group(2)), float(match.group(1))


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bbox(value: str) -> tuple[float, float, float, float]:
    try:
        west, south, east, north = (float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise ValueError("bbox must be west,south,east,north") from exc
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("invalid bbox")
    return west, south, east, north
