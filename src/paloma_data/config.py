from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    supabase_url: str | None
    supabase_secret_key: str | None
    datasf_dataset_id: str
    abc_reports_url: str
    overture_bbox: str
    osm_overpass_url: str
    fsq_catalog_uri: str | None
    fsq_catalog_token: str | None
    fsq_places_table: str | None
    fsq_catalog_warehouse: str | None
    fsq_places_api_key: str | None
    yelp_api_key: str | None
    mapillary_access_token: str | None
    fsq_server_storage_licensed: bool
    catalog_provider_lease_days: int
    allow_snapshot_shrink: bool
    sf_neighborhoods_url: str
    allowed_countries: frozenset[str]
    allowed_regions: frozenset[str]
    allowed_cities: frozenset[str]

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("SUPABASE_DB_URL (or DATABASE_URL) is required")
        return cls(
            database_url=database_url,
            supabase_url=os.getenv("SUPABASE_URL"),
            supabase_secret_key=(
                os.getenv("SUPABASE_SECRET_KEY")
                or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            ),
            datasf_dataset_id=os.getenv("DATASF_DATASET_ID", "g8m3-pdis"),
            abc_reports_url=os.getenv(
                "ABC_REPORTS_URL", "https://www.abc.ca.gov/licensing/licensing-reports/"
            ),
            overture_bbox=os.getenv(
                "PALOMA_OVERTURE_BBOX", "-123.2,36.8,-121.1,38.9"
            ),
            osm_overpass_url=os.getenv(
                "OSM_OVERPASS_URL", "https://overpass-api.de/api/interpreter"
            ),
            fsq_catalog_uri=os.getenv("FSQ_CATALOG_URI"),
            fsq_catalog_token=os.getenv("FSQ_CATALOG_TOKEN"),
            fsq_places_table=os.getenv("FSQ_PLACES_TABLE"),
            fsq_catalog_warehouse=os.getenv("FSQ_CATALOG_WAREHOUSE"),
            fsq_places_api_key=os.getenv("FSQ_PLACES_API_KEY"),
            yelp_api_key=os.getenv("YELP_API_KEY"),
            mapillary_access_token=os.getenv("MAPILLARY_ACCESS_TOKEN"),
            fsq_server_storage_licensed=_boolean(
                os.getenv("FSQ_SERVER_STORAGE_LICENSED", "false")
            ),
            catalog_provider_lease_days=_positive_int(
                os.getenv("CATALOG_PROVIDER_LEASE_DAYS", "45"),
                "CATALOG_PROVIDER_LEASE_DAYS",
            ),
            allow_snapshot_shrink=_boolean(
                os.getenv("PALOMA_ALLOW_SNAPSHOT_SHRINK", "false")
            ),
            sf_neighborhoods_url=os.getenv(
                "SF_NEIGHBORHOODS_URL",
                "https://data.sfgov.org/resource/gfpk-269f.geojson?$limit=5000",
            ),
            allowed_countries=_csv_set(os.getenv("PALOMA_COUNTRIES", "US")),
            allowed_regions=_csv_set(os.getenv("PALOMA_REGIONS", "CA")),
            allowed_cities=_csv_set(os.getenv("PALOMA_CITIES", "San Francisco")),
        )


def _csv_set(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _boolean(value: str) -> bool:
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be positive")
    return parsed
