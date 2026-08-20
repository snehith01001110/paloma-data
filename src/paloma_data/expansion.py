from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib import resources
import json
from typing import Any

from paloma_data.catalog import CATALOG_DECISION_VERSION
from paloma_data.db import Database


_MANIFEST_RESOURCE = "data/bay_area_expansion_v1.json"


class ExpansionBlocked(RuntimeError):
    """Raised when a new-publication release is absent, stale, or unhealthy."""


@dataclass(frozen=True, slots=True)
class ExpansionRelease:
    release_id: str
    label: str
    cities: tuple[str, ...]
    maximum_new_publications: int


@dataclass(frozen=True, slots=True)
class ExpansionManifest:
    manifest_id: str
    sha256: str
    deployment_phase: str
    county_fips: dict[str, str]
    jurisdictions: dict[str, tuple[str, ...]]
    required_source_freshness_days: dict[str, int]
    minimum_healthy_refresh_weeks: int
    refresh_history_days: int
    maximum_latest_refresh_age_hours: int
    failed_run_lookback_days: int
    releases: tuple[ExpansionRelease, ...]

    def release(self, release_id: str) -> ExpansionRelease:
        for release in self.releases:
            if release.release_id == release_id:
                return release
        valid = ", ".join(item.release_id for item in self.releases)
        raise ValueError(f"Unknown expansion release {release_id!r}; choose one of: {valid}")


def load_expansion_manifest() -> ExpansionManifest:
    raw = resources.files("paloma_data").joinpath(_MANIFEST_RESOURCE).read_bytes()
    payload = json.loads(raw)
    if payload.get("schema_version") != 1:
        raise RuntimeError("Unsupported expansion manifest schema")

    region = payload["region"]
    health = payload["health_policy"]
    deployment_phase = str(payload.get("deployment_phase", "production"))
    jurisdictions = {
        str(county): tuple(str(city) for city in cities)
        for county, cities in region["jurisdictions"].items()
    }
    known_cities = {city.casefold() for cities in jurisdictions.values() for city in cities}
    releases = tuple(
        ExpansionRelease(
            release_id=str(item["release_id"]),
            label=str(item["label"]),
            cities=tuple(sorted({str(city) for city in item["cities"]}, key=str.casefold)),
            maximum_new_publications=int(item["maximum_new_publications"]),
        )
        for item in payload["releases"]
    )
    _validate_manifest(
        jurisdictions,
        known_cities,
        releases,
        deployment_phase=deployment_phase,
        minimum_healthy_refresh_weeks=int(health["minimum_healthy_refresh_weeks"]),
    )
    return ExpansionManifest(
        manifest_id=str(payload["manifest_id"]),
        sha256=sha256(raw).hexdigest(),
        deployment_phase=deployment_phase,
        county_fips={str(name): str(fips) for name, fips in region["county_fips"].items()},
        jurisdictions=jurisdictions,
        required_source_freshness_days={
            str(source): int(days)
            for source, days in health["required_source_freshness_days"].items()
        },
        minimum_healthy_refresh_weeks=int(health["minimum_healthy_refresh_weeks"]),
        refresh_history_days=int(health["refresh_history_days"]),
        maximum_latest_refresh_age_hours=int(health["maximum_latest_refresh_age_hours"]),
        failed_run_lookback_days=int(health["failed_run_lookback_days"]),
        releases=releases,
    )


def _validate_manifest(
    jurisdictions: dict[str, tuple[str, ...]],
    known_cities: set[str],
    releases: tuple[ExpansionRelease, ...],
    *,
    deployment_phase: str,
    minimum_healthy_refresh_weeks: int,
) -> None:
    if deployment_phase not in {"development", "production"}:
        raise RuntimeError("Expansion deployment phase must be development or production")
    if minimum_healthy_refresh_weeks < 1:
        raise RuntimeError("At least one healthy refresh week is required")
    if deployment_phase == "production" and minimum_healthy_refresh_weeks < 2:
        raise RuntimeError("Production expansion requires at least two healthy refresh weeks")
    if len(jurisdictions) != 9:
        raise RuntimeError("The Bay Area manifest must contain all nine counties")
    if sum(len(cities) for cities in jurisdictions.values()) != 101:
        raise RuntimeError("The Bay Area manifest must contain all 101 cities and towns")
    release_ids = [release.release_id for release in releases]
    if len(set(release_ids)) != len(release_ids):
        raise RuntimeError("Expansion release IDs must be unique")
    for release in releases:
        if not release.cities or release.maximum_new_publications <= 0:
            raise RuntimeError(f"Expansion release {release.release_id} has an empty scope")
        unknown = sorted(city for city in release.cities if city.casefold() not in known_cities)
        if unknown:
            raise RuntimeError(
                f"Expansion release {release.release_id} has unknown cities: {unknown}"
            )


class ExpansionGate:
    """Read and arm the database-enforced gate for one bounded publication release."""

    def __init__(self, db: Database, manifest: ExpansionManifest | None = None) -> None:
        self.db = db
        self.manifest = manifest or load_expansion_manifest()

    def status(self, conn, release_id: str) -> dict[str, Any]:
        release = self.manifest.release(release_id)
        row = conn.execute(
            """
            select governance.catalog_expansion_status(
              %s, %s, %s::text[], %s, %s::jsonb, %s, %s, %s, %s, %s
            ) as status
            """,
            self._status_parameters(release),
        ).fetchone()
        if not row or not isinstance(row["status"], dict):
            raise RuntimeError("Expansion status function returned no structured result")
        status = dict(row["status"])
        # Capacity is intentionally unavailable before approval, but reporting it as exhausted
        # obscures the actual next action. The database still returns zero available slots.
        if status.get("authorization_event_id") is None:
            status["blockers"] = [
                blocker
                for blocker in status.get("blockers") or ()
                if blocker != "release_capacity_exhausted"
            ]
        return status

    def all_statuses(self) -> dict[str, Any]:
        with self.db.connection() as conn:
            releases = {
                release.release_id: self.status(conn, release.release_id)
                for release in self.manifest.releases
            }
        return {
            "manifest_id": self.manifest.manifest_id,
            "manifest_sha256": self.manifest.sha256,
            "region": {
                "counties": len(self.manifest.county_fips),
                "cities_and_towns": sum(
                    len(cities) for cities in self.manifest.jurisdictions.values()
                ),
            },
            "releases": releases,
        }

    def arm(self, conn, release_id: str) -> tuple[ExpansionRelease, dict[str, Any]]:
        release = self.manifest.release(release_id)
        status = self.status(conn, release_id)
        if not status.get("ready"):
            blockers = "; ".join(str(item) for item in status.get("blockers") or ())
            raise ExpansionBlocked(
                f"Expansion release {release_id} is blocked" + (f": {blockers}" if blockers else "")
            )
        conn.execute(
            "select set_config('paloma.expansion_release_id', %s, true)",
            (release.release_id,),
        )
        conn.execute(
            "select set_config('paloma.expansion_manifest_sha256', %s, true)",
            (self.manifest.sha256,),
        )
        return release, status

    def _status_parameters(self, release: ExpansionRelease) -> tuple[Any, ...]:
        return (
            release.release_id,
            self.manifest.sha256,
            list(release.cities),
            release.maximum_new_publications,
            json.dumps(self.manifest.required_source_freshness_days, sort_keys=True),
            CATALOG_DECISION_VERSION,
            self.manifest.minimum_healthy_refresh_weeks,
            self.manifest.refresh_history_days,
            self.manifest.maximum_latest_refresh_age_hours,
            self.manifest.failed_run_lookback_days,
        )
