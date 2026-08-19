from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from math import floor
from typing import Any, Callable, Iterable

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from paloma_data.adapters.neighborhoods import (
    NeighborhoodBoundary,
    OvertureNeighborhoodAdapter,
)
from paloma_data.adapters.osm import OSMAttributeAdapter, OSMAttributeObservation
from paloma_data.db import Database, execute_many
from paloma_data.normalizers import (
    haversine_meters,
    normalize_name,
    normalize_phone,
    similarity,
    website_host,
)


@dataclass(frozen=True, slots=True)
class CatalogPlace:
    id: str
    name: str
    normalized_name: str
    latitude: float
    longitude: float
    phone_e164: str | None
    website_url: str | None


@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    establishment_id: str
    field_name: str
    value_text: str
    normalized_value: str | None
    value_json: Any
    source: str
    source_record_id: str
    evidence_confidence: float
    identity_confidence: float
    authority: float
    source_updated_at: datetime | None
    metadata: dict[str, Any]


class OpenAttributeEnricher:
    """Attach open attribute observations to existing identities; never create a venue."""

    def __init__(self, db: Database, *, bbox: str, overpass_url: str) -> None:
        self.db = db
        self.bbox = bbox
        self.overpass_url = overpass_url

    def run(self) -> dict[str, dict[str, Any]]:
        return {
            "osm": {
                "status": "excluded",
                "reason": "ODbL strategy is unresolved; OSM cannot enter the canonical ledger",
            },
            "overture_divisions": self._run_source(
                "overture_divisions", self._neighborhood_claims
            ),
        }

    def _run_source(
        self,
        source: str,
        loader: Callable[[], tuple[int, list[EvidenceClaim], int]],
    ) -> dict[str, Any]:
        with self.db.connection() as conn:
            run_id = self.db.start_run(conn, source, "full")
            conn.commit()
        counters = {"fetched": 0, "created": 0, "updated": 0, "unchanged": 0, "review": 0, "closed": 0}
        try:
            fetched, claims, ambiguous = loader()
            counters.update(fetched=fetched, updated=len(claims), review=ambiguous)
            with self.db.connection() as conn:
                self._append_evidence(conn, source, claims)
                self.db.finish_run(conn, run_id, status="succeeded", counters=counters)
                conn.commit()
            return {"status": "succeeded", **counters}
        except Exception as exc:
            with self.db.connection() as conn:
                self.db.finish_run(
                    conn,
                    run_id,
                    status="failed",
                    counters=counters,
                    error=str(exc)[:2000],
                )
                conn.commit()
            return {"status": "failed", "error": str(exc), **counters}

    def _osm_claims(self) -> tuple[int, list[EvidenceClaim], int]:
        observations = list(OSMAttributeAdapter(self.bbox, self.overpass_url).observations())
        places = self._catalog_places()
        index = _place_grid(places)
        claims: list[EvidenceClaim] = []
        ambiguous = 0
        for observation in observations:
            match = _match_osm(observation, index)
            if match is None:
                ambiguous += 1
                continue
            place, identity, method = match
            base_metadata = {"match_method": method, "observed_name": observation.name}
            phone = normalize_phone(observation.phone, "US")
            if phone:
                claims.append(
                    _claim(
                        place.id,
                        "phone_e164",
                        phone,
                        phone,
                        None,
                        "osm",
                        observation.source_record_id,
                        0.90,
                        identity,
                        0.84,
                        observation.source_updated_at,
                        base_metadata,
                    )
                )
            if observation.website_url:
                claims.append(
                    _claim(
                        place.id,
                        "website_url",
                        observation.website_url,
                        observation.website_url,
                        None,
                        "osm",
                        observation.source_record_id,
                        0.86,
                        identity,
                        0.82,
                        observation.source_updated_at,
                        base_metadata,
                    )
                )
            if observation.hours:
                claims.append(
                    _claim(
                        place.id,
                        "hours",
                        observation.hours,
                        observation.hours,
                        observation.hours,
                        "osm",
                        observation.source_record_id,
                        0.84,
                        identity,
                        0.76,
                        observation.source_updated_at,
                        base_metadata,
                    )
                )
            for setting in observation.setting_slugs:
                claims.append(
                    _claim(
                        place.id,
                        "setting_slug",
                        setting,
                        setting,
                        None,
                        "osm",
                        f"{observation.source_record_id}#setting:{setting}",
                        0.82,
                        identity,
                        0.78,
                        observation.source_updated_at,
                        base_metadata,
                    )
                )
        return len(observations), claims, ambiguous

    def _neighborhood_claims(self) -> tuple[int, list[EvidenceClaim], int]:
        boundaries = list(OvertureNeighborhoodAdapter(self.bbox).boundaries())
        geometries = []
        valid_boundaries: list[NeighborhoodBoundary] = []
        for boundary in boundaries:
            geometry = shape(boundary.geometry)
            if geometry.is_empty or not geometry.is_valid:
                continue
            geometries.append(geometry)
            valid_boundaries.append(boundary)
        if not geometries:
            return len(boundaries), [], 0

        tree = STRtree(geometries)
        priorities = {"macrohood": 1, "microhood": 2, "neighborhood": 3}
        claims: list[EvidenceClaim] = []
        unmatched = 0
        for place in self._catalog_places():
            point = Point(place.longitude, place.latitude)
            matches: list[tuple[int, float, NeighborhoodBoundary]] = []
            for raw_index in tree.query(point):
                index = int(raw_index)
                geometry = geometries[index]
                if geometry.covers(point):
                    boundary = valid_boundaries[index]
                    matches.append((priorities[boundary.subtype], -geometry.area, boundary))
            if not matches:
                unmatched += 1
                continue
            boundary = max(matches, key=lambda item: (item[0], item[1]))[2]
            claims.append(
                _claim(
                    place.id,
                    "neighborhood",
                    boundary.name,
                    normalize_name(boundary.name),
                    None,
                    "overture_divisions",
                    boundary.source_record_id,
                    0.94,
                    1.0,
                    0.84,
                    boundary.source_updated_at,
                    {"match_method": "point_in_polygon", "subtype": boundary.subtype},
                )
            )
        return len(boundaries), claims, unmatched

    def _catalog_places(self) -> list[CatalogPlace]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                select e.id::text, e.name, coalesce(e.normalized_name, lower(e.name)) as normalized_name,
                       ST_Y(e.location::geometry) as latitude,
                       ST_X(e.location::geometry) as longitude,
                       e.phone_e164, e.website_url
                from public.establishments e
                where e.status <> 'closed'
                  and exists (
                    select 1 from ingest.establishment_sources es where es.establishment_id = e.id
                  )
                """
            ).fetchall()
        return [CatalogPlace(**row) for row in rows]

    def _append_evidence(
        self, conn, source: str, claims: Iterable[EvidenceClaim]
    ) -> None:
        rows = list(claims)
        if not rows:
            return
        execute_many(
            conn,
            """
            insert into catalog.field_observations (
                establishment_id, field_name, value_text, normalized_value, value_json,
                value_hash, source, source_record_id, source_property, claim_kind,
                evidence_confidence, identity_confidence, authority,
                upstream_origin_keys, license_ids, source_items, source_policy_id,
                source_updated_at, expires_at, observation_fingerprint, metadata
            ) values (
                %s::uuid, %s, %s, %s, %s::jsonb,
                %s, %s, %s, %s, 'derived', %s, %s, %s,
                %s, %s, '[]'::jsonb,
                (select source_policy_id from governance.current_source_field_policies
                 where source = %s and field_name = %s),
                %s, now() + interval '365 days', %s, %s::jsonb
            )
            on conflict (observation_fingerprint) do nothing
            """,
            [
                (
                    row.establishment_id,
                    row.field_name,
                    row.value_text,
                    row.normalized_value,
                    json.dumps(row.value_json, sort_keys=True)
                    if row.value_json is not None
                    else None,
                    _hash({"text": row.value_text, "json": row.value_json}),
                    row.source,
                    row.source_record_id,
                    row.field_name,
                    row.evidence_confidence,
                    row.identity_confidence,
                    row.authority,
                    ["overture:divisions"],
                    ["Overture-source-licenses"],
                    row.source,
                    row.field_name,
                    row.source_updated_at,
                    _hash(
                        {
                            "establishment_id": row.establishment_id,
                            "field": row.field_name,
                            "source": row.source,
                            "source_record_id": row.source_record_id,
                            "value": row.value_text,
                            "source_updated_at": row.source_updated_at,
                        }
                    ),
                    json.dumps(row.metadata, sort_keys=True),
                )
                for row in rows
            ],
        )


def _hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode()).hexdigest()


def _place_grid(places: Iterable[CatalogPlace]) -> dict[tuple[int, int], list[CatalogPlace]]:
    grid: dict[tuple[int, int], list[CatalogPlace]] = defaultdict(list)
    for place in places:
        grid[_grid_key(place.latitude, place.longitude)].append(place)
    return grid


def _grid_key(latitude: float, longitude: float) -> tuple[int, int]:
    return floor(latitude / 0.002), floor(longitude / 0.002)


def _match_osm(
    observation: OSMAttributeObservation,
    grid: dict[tuple[int, int], list[CatalogPlace]],
) -> tuple[CatalogPlace, float, str] | None:
    lat_key, lon_key = _grid_key(observation.latitude, observation.longitude)
    candidates = [
        place
        for lat_delta in (-1, 0, 1)
        for lon_delta in (-1, 0, 1)
        for place in grid.get((lat_key + lat_delta, lon_key + lon_delta), ())
    ]
    ranked: list[tuple[float, CatalogPlace, str]] = []
    observed_phone = normalize_phone(observation.phone, "US")
    observed_host = website_host(observation.website_url)
    observed_name = normalize_name(observation.name)
    for place in candidates:
        distance = haversine_meters(
            observation.latitude,
            observation.longitude,
            place.latitude,
            place.longitude,
        )
        if distance > 100:
            continue
        if observed_phone and place.phone_e164 and observed_phone == place.phone_e164:
            ranked.append((0.98, place, "exact_phone_nearby"))
            continue
        if observed_host and observed_host == website_host(place.website_url):
            ranked.append((0.96, place, "exact_website_nearby"))
            continue
        name_score = similarity(observed_name, place.normalized_name)
        if name_score >= 0.92 and distance <= 50:
            ranked.append((0.94, place, "strong_name_nearby"))
        elif name_score >= 0.86 and distance <= 20:
            ranked.append((0.90, place, "name_same_door"))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.05:
        return None
    return ranked[0][1], ranked[0][0], ranked[0][2]


def _claim(
    establishment_id: str,
    field_name: str,
    value_text: str,
    normalized_value: str | None,
    value_json: Any,
    source: str,
    source_record_id: str,
    evidence_confidence: float,
    identity_confidence: float,
    authority: float,
    source_updated_at: datetime | None,
    metadata: dict[str, Any],
) -> EvidenceClaim:
    return EvidenceClaim(
        establishment_id=establishment_id,
        field_name=field_name,
        value_text=value_text,
        normalized_value=normalized_value,
        value_json=value_json,
        source=source,
        source_record_id=source_record_id,
        evidence_confidence=evidence_confidence,
        identity_confidence=identity_confidence,
        authority=authority,
        source_updated_at=source_updated_at,
        metadata=metadata,
    )
