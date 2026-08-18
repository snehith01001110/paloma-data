"""Address geocoding for sources that publish a street address but no coordinates.

Paloma refuses to create an establishment it cannot place on a map, so a source without
coordinates can never introduce a venue on its own no matter how authoritative it is. The
California ABC export is exactly that case: it is the strongest licence signal available and
carries a full street address, but no latitude or longitude.

The US Census Bureau batch geocoder is used because it is public, keyed to US street addresses,
free of charge, and requires no account or API key, so it adds no credential to manage and no
per-call cost to the ingestion budget.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import io
from typing import Iterable, Iterator, Sequence

import httpx

CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
# Current nationwide address ranges. Census also publishes dated benchmarks; pin the rolling
# current one so re-runs pick up newly built addresses.
CENSUS_BENCHMARK = "Public_AR_Current"
# The published cap is 10,000 rows per request. Stay below it so one oversized city batch
# cannot fail the whole pass.
BATCH_SIZE = 5000


@dataclass(frozen=True, slots=True)
class GeocodeRequest:
    key: str
    street: str
    city: str
    region: str | None
    postal_code: str | None


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    key: str
    latitude: float
    longitude: float
    matched_address: str


def chunked(items: Sequence[GeocodeRequest], size: int = BATCH_SIZE) -> Iterator[list[GeocodeRequest]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def build_csv(requests: Iterable[GeocodeRequest]) -> str:
    """Census expects a headerless CSV of id, street, city, state, zip."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for request in requests:
        writer.writerow(
            [
                request.key,
                request.street,
                request.city,
                request.region or "",
                request.postal_code or "",
            ]
        )
    return buffer.getvalue()


def parse_response(text: str) -> dict[str, GeocodeResult]:
    """Read the batch response, keeping only unambiguous matches.

    Census returns id, input, match indicator, match type, matched address, "lon,lat", and two
    TIGER columns. A tie means several addresses fit, which is not good enough to place a venue,
    so only an exact ``Match`` is accepted.
    """
    results: dict[str, GeocodeResult] = {}
    for row in csv.reader(io.StringIO(text)):
        if (
            len(row) < 6
            or row[2].strip().casefold() != "match"
            or row[3].strip().casefold() != "exact"
        ):
            continue
        longitude, _, latitude = row[5].partition(",")
        try:
            results[row[0].strip()] = GeocodeResult(
                key=row[0].strip(),
                latitude=float(latitude),
                longitude=float(longitude),
                matched_address=row[4].strip(),
            )
        except ValueError:
            # A malformed coordinate is a non-match, not a reason to abandon the batch.
            continue
    return results


def geocode(
    client: httpx.Client,
    requests: Sequence[GeocodeRequest],
    *,
    benchmark: str = CENSUS_BENCHMARK,
) -> dict[str, GeocodeResult]:
    """Geocode a batch of addresses, returning only the ones that matched exactly."""
    if not requests:
        return {}
    payload = build_csv(requests)
    response = client.post(
        CENSUS_BATCH_URL,
        data={"benchmark": benchmark},
        files={"addressFile": ("addresses.csv", payload, "text/csv")},
    )
    response.raise_for_status()
    return parse_response(response.text)


class AddressGeocoder:
    """Fill in coordinates for staged records whose source publishes none."""

    GEOCODER = "census"

    def __init__(self, db) -> None:
        self.db = db

    def run(self, source: str) -> dict[str, int]:
        metrics = {"considered": 0, "matched": 0, "unmatched": 0, "failed_batches": 0}
        with self.db.connection() as conn:
            rows = self.db.records_needing_geocode(conn, source)
            metrics["considered"] = len(rows)
            if not rows:
                return metrics
            requests = [
                GeocodeRequest(
                    key=row["source_record_id"],
                    street=row["address"],
                    city=row["city"],
                    region=row["region"],
                    postal_code=row["postal_code"],
                )
                for row in rows
            ]
            # A large batch is answered in one slow response, so allow a generous read timeout.
            with httpx.Client(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
                for batch in chunked(requests):
                    try:
                        results = geocode(client, batch)
                    except httpx.HTTPError:
                        # Treat a transport or server failure as unattempted so the next run
                        # retries it, rather than recording a permanent non-match.
                        metrics["failed_batches"] += 1
                        continue
                    for request in batch:
                        hit = results.get(request.key)
                        if hit is None:
                            metrics["unmatched"] += 1
                            continue
                        self.db.save_geocode(
                            conn,
                            source,
                            request.key,
                            hit.latitude,
                            hit.longitude,
                            self.GEOCODER,
                            hit.matched_address,
                        )
                        metrics["matched"] += 1
                    self.db.mark_geocode_attempted(conn, source, [item.key for item in batch])
                    # Each batch costs a slow round trip; never discard one that already landed.
                    conn.commit()
        return metrics
