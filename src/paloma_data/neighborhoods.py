from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from typing import Any

import httpx

from paloma_data.db import Database
from paloma_data.normalizers import normalize_name


SF_FIND_NEIGHBORHOODS_URL = (
    "https://data.sfgov.org/resource/gfpk-269f.geojson?$limit=5000"
)


@dataclass(frozen=True, slots=True)
class CivicNeighborhood:
    source_record_id: str
    jurisdiction: str
    name: str
    geometry: dict[str, Any]
    source_updated_at: datetime | None

    def payload_hash(self) -> str:
        value = {
            "jurisdiction": self.jurisdiction,
            "name": self.name,
            "geometry": self.geometry,
            "source_updated_at": (
                self.source_updated_at.isoformat() if self.source_updated_at else None
            ),
        }
        return sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class DataSFNeighborhoodAdapter:
    """Small, authoritative-enough display boundary feed; never an identity source."""

    source = "datasf_neighborhoods"
    jurisdiction = "San Francisco"
    data_license = "Public-Domain-US-Government"
    authority = 0.94

    def __init__(
        self,
        url: str = SF_FIND_NEIGHBORHOODS_URL,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"User-Agent": "paloma-data/0.4"},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "DataSFNeighborhoodAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def boundaries(self) -> Iterator[CivicNeighborhood]:
        response = self.client.get(self.url)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
            raise ValueError("DataSF neighborhood response is not a GeoJSON FeatureCollection")
        updated_at = _http_datetime(response.headers.get("Last-Modified"))
        for feature in payload["features"]:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties")
            geometry = feature.get("geometry")
            if not isinstance(properties, dict) or not isinstance(geometry, dict):
                continue
            if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
                continue
            name = str(properties.get("name") or "").strip()
            if not name:
                continue
            record_id = str(
                feature.get("id")
                or properties.get("link")
                or normalize_name(name)
            )
            yield CivicNeighborhood(
                source_record_id=record_id,
                jurisdiction=self.jurisdiction,
                name=name,
                geometry=geometry,
                source_updated_at=updated_at,
            )


class NeighborhoodStager:
    def __init__(self, db: Database, *, allow_snapshot_shrink: bool = False) -> None:
        self.db = db
        self.allow_snapshot_shrink = allow_snapshot_shrink

    def run(self, adapter: DataSFNeighborhoodAdapter) -> dict[str, int]:
        counters = {"fetched": 0, "created": 0, "updated": 0, "unchanged": 0, "closed": 0}
        with self.db.connection() as conn:
            conn.execute(
                "select pg_advisory_lock(hashtext('paloma_source_snapshot:' || %s))",
                (adapter.source,),
            )
            try:
                previous = conn.execute(
                    "select record_count from ingest.source_sync_state where source = %s",
                    (adapter.source,),
                ).fetchone()
                previous_count = int(previous["record_count"]) if previous else 0
                run_id = self.db.start_run(conn, adapter.source, "full")
                conn.commit()
                try:
                    for boundary in adapter.boundaries():
                        counters["fetched"] += 1
                        existing = conn.execute(
                            """
                            select payload_hash from ingest.neighborhood_boundaries
                            where source = %s and source_record_id = %s
                            """,
                            (adapter.source, boundary.source_record_id),
                        ).fetchone()
                        payload_hash = boundary.payload_hash()
                        status = (
                            "created"
                            if existing is None
                            else "unchanged"
                            if existing["payload_hash"] == payload_hash
                            else "updated"
                        )
                        counters[status] += 1
                        conn.execute(
                            """
                            insert into ingest.neighborhood_boundaries (
                              source, source_record_id, jurisdiction, name, normalized_name,
                              boundary, authority, data_license, source_updated_at,
                              payload_hash, last_seen_run_id, retired_at
                            ) values (
                              %s, %s, %s, %s, %s,
                              ST_Multi(ST_CollectionExtract(ST_MakeValid(
                                ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)
                              ), 3)),
                              %s, %s, %s, %s, %s::uuid, null
                            )
                            on conflict (source, source_record_id) do update set
                              jurisdiction = excluded.jurisdiction,
                              name = excluded.name,
                              normalized_name = excluded.normalized_name,
                              boundary = excluded.boundary,
                              authority = excluded.authority,
                              data_license = excluded.data_license,
                              source_updated_at = excluded.source_updated_at,
                              payload_hash = excluded.payload_hash,
                              last_seen_run_id = excluded.last_seen_run_id,
                              retired_at = null,
                              updated_at = now()
                            """,
                            (
                                adapter.source,
                                boundary.source_record_id,
                                boundary.jurisdiction,
                                boundary.name,
                                normalize_name(boundary.name),
                                json.dumps(boundary.geometry, separators=(",", ":")),
                                adapter.authority,
                                adapter.data_license,
                                boundary.source_updated_at,
                                payload_hash,
                                run_id,
                            ),
                        )

                    self._validate_count(
                        adapter.source,
                        current_count=counters["fetched"],
                        previous_count=previous_count,
                    )
                    counters["closed"] = len(
                        conn.execute(
                            """
                            update ingest.neighborhood_boundaries
                            set retired_at = now(), updated_at = now()
                            where source = %s and retired_at is null
                              and last_seen_run_id is distinct from %s::uuid
                            returning 1
                            """,
                            (adapter.source, run_id),
                        ).fetchall()
                    )
                    conn.execute(
                        """
                        insert into ingest.source_sync_state (
                          source, last_complete_run_id, completed_at, record_count, updated_at
                        ) values (%s, %s::uuid, now(), %s, now())
                        on conflict (source) do update set
                          last_complete_run_id = excluded.last_complete_run_id,
                          completed_at = excluded.completed_at,
                          record_count = excluded.record_count,
                          updated_at = now()
                        """,
                        (adapter.source, run_id, counters["fetched"]),
                    )
                    self.db.finish_run(
                        conn,
                        run_id,
                        status="succeeded",
                        counters=counters,
                    )
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    self.db.finish_run(
                        conn,
                        run_id,
                        status="failed",
                        counters=counters,
                        error=str(exc)[:2000],
                    )
                    conn.commit()
                    raise
            finally:
                conn.execute(
                    "select pg_advisory_unlock(hashtext('paloma_source_snapshot:' || %s))",
                    (adapter.source,),
                )
        return counters

    def _validate_count(
        self,
        source: str,
        *,
        current_count: int,
        previous_count: int,
    ) -> None:
        if current_count <= 0:
            raise RuntimeError(f"{source} returned no valid boundaries")
        if (
            not self.allow_snapshot_shrink
            and previous_count >= 10
            and current_count < previous_count * 0.5
        ):
            raise RuntimeError(
                f"{source} shrank from {previous_count} to {current_count}; "
                "refusing mass retirement"
            )


def _http_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
