from __future__ import annotations

from collections.abc import Iterable

from paloma_data.db import Database
from paloma_data.models import SourceRecord


class SourceStager:
    """Persist one complete source snapshot without creating product entities."""

    def __init__(
        self,
        db: Database,
        *,
        allowed_cities: frozenset[str],
        allowed_regions: frozenset[str],
        allowed_countries: frozenset[str],
        allow_snapshot_shrink: bool = False,
    ) -> None:
        self.db = db
        self.allowed_cities = {value.casefold() for value in allowed_cities}
        self.allowed_regions = {value.casefold() for value in allowed_regions}
        self.allowed_countries = {value.casefold() for value in allowed_countries}
        self.allow_snapshot_shrink = allow_snapshot_shrink

    def run_snapshot(
        self,
        source: str,
        mode: str,
        records: Iterable[SourceRecord],
        *,
        release_id: str | None = None,
        cursor_after: str | None = None,
    ) -> dict[str, int]:
        if mode not in {"full", "incremental"}:
            raise ValueError("snapshot mode must be full or incremental")
        counters = {
            "fetched": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "review": 0,
            "closed": 0,
        }
        with self.db.connection() as conn:
            # Session-scoped: checkpoints commit during a long snapshot, so a transaction lock
            # would not prevent a second operator from racing the same source.
            conn.execute(
                "select pg_advisory_lock(hashtext('paloma_source_snapshot:' || %s))",
                (source,),
            )
            try:
                previous = conn.execute(
                    "select record_count from ingest.source_sync_state where source = %s",
                    (source,),
                ).fetchone()
                previous_count = int(previous["record_count"]) if previous else 0
                run_id = self.db.start_run(conn, source, mode, cursor_after)
                conn.commit()
                for record in records:
                    counters["fetched"] += 1
                    if self._in_scope(record):
                        outcome = self.db.stage_source_record(conn, record, run_id)
                        counters[outcome] += 1
                    if counters["fetched"] % 500 == 0:
                        _checkpoint(conn, run_id, counters)
                        conn.commit()

                current_count = (
                    counters["created"] + counters["updated"] + counters["unchanged"]
                )
                self._validate_snapshot_size(
                    source,
                    current_count=current_count,
                    previous_count=previous_count,
                )
                counters["closed"] = self.db.complete_source_snapshot(
                    conn,
                    source=source,
                    run_id=run_id,
                    record_count=current_count,
                    release_id=release_id,
                    cursor_after=cursor_after,
                )
                _checkpoint(conn, run_id, counters)
                self.db.finish_run(conn, run_id, status="succeeded", counters=counters)
                conn.commit()
                return counters
            except Exception as exc:
                conn.rollback()
                if "run_id" in locals():
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
                    (source,),
                )

    def _in_scope(self, record: SourceRecord) -> bool:
        if self.allowed_countries and record.country_code.casefold() not in self.allowed_countries:
            return False
        if self.allowed_regions and (record.region or "").casefold() not in self.allowed_regions:
            return False
        if self.allowed_cities and record.city.casefold() not in self.allowed_cities:
            return False
        return True

    def _validate_snapshot_size(
        self,
        source: str,
        *,
        current_count: int,
        previous_count: int,
    ) -> None:
        if current_count <= 0:
            raise RuntimeError(
                f"{source} snapshot produced zero in-scope rows; refusing absence processing"
            )
        if (
            not self.allow_snapshot_shrink
            and previous_count >= 20
            and current_count < previous_count * 0.5
        ):
            raise RuntimeError(
                f"{source} snapshot shrank from {previous_count} to {current_count}; "
                "refusing mass retirement (set PALOMA_ALLOW_SNAPSHOT_SHRINK=true only "
                "after confirming an intentional scope change)"
            )


def _checkpoint(conn, run_id: str, counters: dict[str, int]) -> None:
    conn.execute(
        """
        update ingest.ingestion_runs
        set fetched_count = %s,
            created_count = %s,
            updated_count = %s,
            unchanged_count = %s,
            review_count = %s,
            closed_count = %s
        where id = %s::uuid and status = 'running'
        """,
        (
            counters["fetched"],
            counters["created"],
            counters["updated"],
            counters["unchanged"],
            counters["review"],
            counters["closed"],
            run_id,
        ),
    )
