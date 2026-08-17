from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    datasf_dataset_id: str
    abc_reports_url: str
    overture_bbox: str
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
            datasf_dataset_id=os.getenv("DATASF_DATASET_ID", "g8m3-pdis"),
            abc_reports_url=os.getenv(
                "ABC_REPORTS_URL", "https://www.abc.ca.gov/licensing/licensing-reports/"
            ),
            overture_bbox=os.getenv(
                "PALOMA_OVERTURE_BBOX", "-123.2,36.8,-121.1,38.9"
            ),
            allowed_countries=_csv_set(os.getenv("PALOMA_COUNTRIES", "US")),
            allowed_regions=_csv_set(os.getenv("PALOMA_REGIONS", "CA")),
            allowed_cities=_csv_set(os.getenv("PALOMA_CITIES", "San Francisco")),
        )


def _csv_set(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())
