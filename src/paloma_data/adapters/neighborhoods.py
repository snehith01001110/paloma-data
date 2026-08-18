from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from paloma_data.adapters.overture import (
    _download_feature,
    _latest_release,
    _latest_update_time,
    _validate_bbox,
)


@dataclass(frozen=True, slots=True)
class NeighborhoodBoundary:
    source_record_id: str
    name: str
    subtype: str
    geometry: dict[str, Any]
    source_updated_at: datetime | None


class OvertureNeighborhoodAdapter:
    source = "overture_divisions"
    _SUBTYPES = frozenset({"macrohood", "neighborhood", "microhood"})

    def __init__(self, bbox: str) -> None:
        self.bbox = _validate_bbox(bbox)

    def boundaries(self) -> Iterator[NeighborhoodBoundary]:
        with TemporaryDirectory(prefix="paloma-neighborhoods-") as directory:
            output = Path(directory) / "division_areas.geojsonseq"
            release = _latest_release()
            _download_feature(output, self.bbox, release, "division_area")
            with output.open("r", encoding="utf-8") as handle:
                for line in handle:
                    payload = line.lstrip("\x1e").strip()
                    if not payload:
                        continue
                    boundary = self._to_boundary(json.loads(payload))
                    if boundary is not None:
                        yield boundary

    def _to_boundary(self, feature: dict[str, Any]) -> NeighborhoodBoundary | None:
        properties = feature.get("properties") or {}
        subtype = str(properties.get("subtype") or "")
        if subtype not in self._SUBTYPES:
            return None
        names = properties.get("names") or {}
        name = names.get("primary") if isinstance(names, dict) else None
        source_id = properties.get("id") or feature.get("id")
        geometry = feature.get("geometry")
        if not source_id or not name or not isinstance(geometry, dict):
            return None
        if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            return None
        return NeighborhoodBoundary(
            source_record_id=str(source_id),
            name=str(name).strip(),
            subtype=subtype,
            geometry=geometry,
            source_updated_at=_latest_update_time(properties.get("sources") or []),
        )
