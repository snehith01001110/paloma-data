from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Mapping
from urllib.parse import urlsplit


HOURS_SOURCE_KINDS = frozenset(
    {
        "first_party",
        "merchant",
        "firsthand",
        "open_data",
        "manual_review",
        "other",
    }
)
MANUAL_EVIDENCE_KINDS = frozenset({"factual_reference", "first_party"})
OPEN_DATA_SOURCES = frozenset(
    {
        "ca_abc",
        "datasf",
        "datasf_neighborhoods",
        "fsq",
        "osm",
        "overture",
        "wikidata",
    }
)


@dataclass(frozen=True, slots=True)
class HoursProvenance:
    verified_at: datetime
    expires_at: datetime
    source_url: str | None
    source_kind: str


def hours_observation_provenance(
    observation: Mapping[str, Any] | None,
) -> HoursProvenance | None:
    """Return the bounded public metadata for one selected hours observation."""
    if not observation:
        return None
    verified_at = observation.get("observed_at")
    expires_at = observation.get("expires_at")
    if not isinstance(verified_at, datetime) or not isinstance(expires_at, datetime):
        return None
    if expires_at <= verified_at:
        return None

    metadata = _object(observation.get("metadata"))
    evidence_kind = str(metadata.get("evidence_kind") or "").strip().casefold()
    source = str(observation.get("best_source") or observation.get("source") or "")
    items = _items(observation.get("source_items"))
    has_first_party_item = any(item.get("kind") == "first_party" for item in items)

    if evidence_kind == "first_party" or has_first_party_item:
        source_kind = "first_party"
    elif source == "merchant":
        source_kind = "merchant"
    elif source == "firsthand":
        source_kind = "firsthand"
    elif source == "manual":
        source_kind = "manual_review"
    elif source in OPEN_DATA_SOURCES:
        source_kind = "open_data"
    else:
        source_kind = "other"

    ordered_items = sorted(
        items,
        key=lambda item: (item.get("kind") != "first_party", item.get("url") or ""),
    )
    source_url = next(
        (
            str(item["url"])
            for item in ordered_items
            if _is_https_url(item.get("url"))
        ),
        None,
    )
    return HoursProvenance(
        verified_at=verified_at,
        expires_at=expires_at,
        source_url=source_url,
        source_kind=source_kind,
    )


def _items(value: Any) -> list[dict[str, Any]]:
    parsed = _json(value)
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, Mapping)]


def _object(value: Any) -> dict[str, Any]:
    parsed = _json(value)
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _is_https_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.netloc)
