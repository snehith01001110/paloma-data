from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from paloma_data.db import Database
from paloma_data.matching import decide_match
from paloma_data.models import SourceRecord
from paloma_data.taxonomy import ACCESS_SPECIFIC_TYPES, BAR_TYPES


class Pipeline:
    def __init__(
        self,
        db: Database,
        *,
        allowed_cities: frozenset[str],
        allowed_regions: frozenset[str],
        allowed_countries: frozenset[str],
    ) -> None:
        self.db = db
        self.allowed_cities = {city.casefold() for city in allowed_cities}
        self.allowed_regions = {region.casefold() for region in allowed_regions}
        self.allowed_countries = {country.casefold() for country in allowed_countries}

    def run(self, source: str, mode: str, records: Iterable[SourceRecord]) -> dict[str, int]:
        counters = {
            "fetched": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "review": 0,
            "closed": 0,
        }
        with self.db.connection() as conn:
            run_id = self.db.start_run(conn, source, mode)
            conn.commit()
            try:
                for record in records:
                    counters["fetched"] += 1
                    if self._in_scope(record):
                        self._process_record(conn, record, counters)
                    if counters["fetched"] % 250 == 0:
                        _checkpoint_run(conn, run_id, counters)
                        conn.commit()
                _checkpoint_run(conn, run_id, counters)
                conn.commit()
                self.db.finish_run(conn, run_id, status="succeeded", counters=counters)
                conn.commit()
                return counters
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

    def _in_scope(self, record: SourceRecord) -> bool:
        if self.allowed_countries and record.country_code.casefold() not in self.allowed_countries:
            return False
        if self.allowed_regions and (record.region or "").casefold() not in self.allowed_regions:
            return False
        if self.allowed_cities and record.city.casefold() not in self.allowed_cities:
            return False
        return True

    def reconcile_staged(self, source: str) -> dict[str, int]:
        """Re-decide staged records that never joined an establishment.

        Enrichment such as geocoding changes what can be decided about a record long after it was
        first read. Re-reading the upstream source to act on that would be wasteful and, for a
        source that publishes no coordinates, would discard the enrichment on the way back in.
        """
        counters = {"fetched": 0, "created": 0, "updated": 0, "unchanged": 0, "review": 0, "closed": 0}
        with self.db.connection() as conn:
            records = self.db.unlinked_staged_records(conn, source)
            run_id = self.db.start_run(conn, source, "reconciliation")
            conn.commit()
            try:
                for record in records:
                    counters["fetched"] += 1
                    if not self._in_scope(record):
                        continue
                    self._resolve_record(conn, record, counters)
                    if counters["fetched"] % 250 == 0:
                        _checkpoint_run(conn, run_id, counters)
                        conn.commit()
                _checkpoint_run(conn, run_id, counters)
                conn.commit()
                self.db.finish_run(conn, run_id, status="succeeded", counters=counters)
                conn.commit()
                return counters
            except Exception as exc:
                conn.rollback()
                self.db.finish_run(
                    conn, run_id, status="failed", counters=counters, error=str(exc)[:2000]
                )
                conn.commit()
                raise

    def _process_record(self, conn, record: SourceRecord, counters: dict[str, int]) -> None:
        changed = self.db.stage_source_record(conn, record)
        linked_id = self.db.linked_establishment_id(conn, record.source, record.source_record_id)

        if not changed and linked_id:
            self.db.upsert_source_link(conn, linked_id, record, 1.0, "exact_source_id")
            counters["unchanged"] += 1
            return
        if not changed:
            counters["unchanged"] += 1
            return

        if linked_id:
            self.db.upsert_source_link(conn, linked_id, record, 1.0, "exact_source_id")
            counters["updated"] += 1
            if record.source_status == "closed" and self.db.reconcile_closure(conn, linked_id):
                counters["closed"] += 1
            return

        self._resolve_record(conn, record, counters)

    def _resolve_record(self, conn, record: SourceRecord, counters: dict[str, int]) -> None:
        """Match, create, or queue an already staged record."""
        candidates = self.db.find_candidates(conn, record)
        decision = decide_match(record, candidates)
        if decision.action == "auto_match" and decision.candidate_id:
            self.db.upsert_source_link(
                conn,
                decision.candidate_id,
                record,
                decision.score,
                decision.reason or "auto_match",
            )
            counters["updated"] += 1
            if (
                record.source_status == "closed"
                and self.db.reconcile_closure(conn, decision.candidate_id)
            ):
                counters["closed"] += 1
            return

        if decision.action == "review":
            self.db.enqueue_review(
                conn,
                record,
                reason=decision.reason or "ambiguous_match",
                confidence=decision.score,
                candidate_id=decision.candidate_id,
                evidence={"match_score": decision.score, "category": record.category_evidence},
            )
            counters["review"] += 1
            return

        # Before creating anything new, look for independent evidence staged from another source.
        corroboration = self.db.find_source_corroboration(conn, record)
        if corroboration:
            other, identity_score = corroboration
            combined = _combine_for_creation(record, other)
            if combined is None:
                self.db.enqueue_review(
                    conn,
                    record,
                    reason="source_conflict",
                    confidence=identity_score,
                    candidate_id=None,
                    evidence={
                        "other_source": other.source,
                        "other_source_record_id": other.source_record_id,
                        "current_type": record.primary_type_slug,
                        "other_type": other.primary_type_slug,
                        "current_status": record.source_status,
                        "other_status": other.source_status,
                    },
                )
                counters["review"] += 1
                return

            quality = _combined_quality(record, other, identity_score)
            if _safe_to_create(combined, quality):
                other_establishment_id = self.db.linked_establishment_id(
                    conn, other.source, other.source_record_id
                )
                if other_establishment_id:
                    self.db.upsert_source_link(
                        conn,
                        other_establishment_id,
                        record,
                        identity_score,
                        "linked_source_corroboration",
                    )
                    counters["updated"] += 1
                    return
                establishment_id = self.db.create_establishment(conn, combined, quality)
                self.db.upsert_source_link(
                    conn,
                    establishment_id,
                    record,
                    identity_score,
                    "cross_source_corroboration",
                )
                self.db.upsert_source_link(
                    conn,
                    establishment_id,
                    other,
                    identity_score,
                    "cross_source_corroboration",
                )
                counters["created"] += 1
                return

        # A consumer POI may establish a hidden canonical candidate so later legal evidence can
        # link to a stable Paloma ID. Publication is a separate, stricter decision.
        quality = _confidence(record.classification_confidence)
        if _safe_to_create(record, quality):
            establishment_id = self.db.create_establishment(conn, record, quality)
            self.db.upsert_source_link(
                conn,
                establishment_id,
                record,
                quality,
                "high_confidence_new",
            )
            counters["created"] += 1
            return

        # Do not flood review with every weak discovery row. Meaningful Paloma evidence waits here
        # for a second source or operator; irrelevant rows remain staged only.
        if _confidence(record.classification_confidence) >= 0.78:
            self.db.enqueue_review(
                conn,
                record,
                reason="needs_type_or_location_corroboration",
                confidence=_confidence(record.classification_confidence),
                candidate_id=None,
                evidence={"category": record.category_evidence},
            )
            counters["review"] += 1


def _checkpoint_run(conn, run_id: str, counters: dict[str, int]) -> None:
    """Persist live counters without marking the run finished."""
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
            counters.get("fetched", 0),
            counters.get("created", 0),
            counters.get("updated", 0),
            counters.get("unchanged", 0),
            counters.get("review", 0),
            counters.get("closed", 0),
            run_id,
        ),
    )


def _confidence(value: object | None) -> float:
    """Normalize DB numeric/Decimal values and adapter floats to one arithmetic type."""
    if value is None:
        return 0.0
    return float(value)


def _safe_to_create(record: SourceRecord, quality: float) -> bool:
    hard_negative = {
        "closed",
        "delete",
        "doesnt_exist",
        "does_not_exist",
        "duplicate",
        "privatevenue",
        "private_venue",
    }
    return bool(
        record.source_family == "consumer_poi"
        and record.source_status == "open"
        and record.latitude is not None
        and record.longitude is not None
        and record.primary_type_slug in ACCESS_SPECIFIC_TYPES
        and record.consumer_facing
        and record.public_access == "walk_in"
        and not hard_negative.intersection(record.quality_flags)
        and quality >= 0.85
    )


def _combine_for_creation(a: SourceRecord, b: SourceRecord) -> SourceRecord | None:
    if "closed" in {a.source_status, b.source_status} and "open" in {
        a.source_status,
        b.source_status,
    }:
        return None
    chosen_type = _compatible_public_type(a, b)
    if chosen_type is None:
        return None

    consumer = a if a.consumer_facing else b if b.consumer_facing else None
    located = (
        consumer
        if consumer and consumer.latitude is not None and consumer.longitude is not None
        else a
        if a.latitude is not None and a.longitude is not None
        else b
    )
    if consumer is None or located.latitude is None or located.longitude is None:
        return None

    return replace(
        consumer,
        latitude=located.latitude,
        longitude=located.longitude,
        primary_type_slug=chosen_type,
        classification_confidence=max(
            _confidence(a.classification_confidence),
            _confidence(b.classification_confidence),
        ),
        phone=consumer.phone or located.phone,
        website_url=consumer.website_url or located.website_url,
        source_status="open" if "open" in {a.source_status, b.source_status} else consumer.source_status,
        category_evidence={
            "corroborated_sources": [a.source, b.source],
            "typed_by": consumer.source,
            "typed_evidence": consumer.category_evidence,
        },
    )


def _compatible_public_type(a: SourceRecord, b: SourceRecord) -> str | None:
    left = a.primary_type_slug
    right = b.primary_type_slug
    if left == right:
        return left
    if not left:
        return right
    if not right:
        return left

    if left in BAR_TYPES and right in BAR_TYPES:
        if a.consumer_facing and left != "bar":
            return left
        if b.consumer_facing and right != "bar":
            return right
        return "bar"

    compatible = {
        frozenset({"winery", "tasting_room"}): "tasting_room",
        frozenset({"distillery", "tasting_room"}): "tasting_room",
        frozenset({"brewery", "taproom"}): "taproom",
        frozenset({"brewery", "brewpub"}): "brewpub",
    }
    return compatible.get(frozenset({left, right}))


def _combined_quality(a: SourceRecord, b: SourceRecord, identity_score: float) -> float:
    typed_confidence = max(
        _confidence(a.classification_confidence),
        _confidence(b.classification_confidence),
    )
    combined = 0.65 * typed_confidence + 0.35 * float(identity_score)
    if a.primary_type_slug and a.primary_type_slug == b.primary_type_slug:
        combined += 0.03
    return min(0.99, combined)
