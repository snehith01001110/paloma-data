from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from paloma_data.db import Database
from paloma_data.matching import decide_match
from paloma_data.models import SourceRecord


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
                    if not self._in_scope(record):
                        continue
                    self._process_record(conn, record, counters)
                    if counters["fetched"] % 500 == 0:
                        conn.commit()
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

        # A single exceptionally strong source may create only a tightly classified open record.
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


def _confidence(value: object | None) -> float:
    """Normalize DB numeric/Decimal values and adapter floats to one arithmetic type."""
    if value is None:
        return 0.0
    return float(value)


def _safe_to_create(record: SourceRecord, quality: float) -> bool:
    return bool(
        record.source_status == "open"
        and record.latitude is not None
        and record.longitude is not None
        and record.primary_type_slug
        and quality >= 0.95
    )


def _combine_for_creation(a: SourceRecord, b: SourceRecord) -> SourceRecord | None:
    if "closed" in {a.source_status, b.source_status} and "open" in {
        a.source_status,
        b.source_status,
    }:
        return None
    if a.primary_type_slug and b.primary_type_slug and a.primary_type_slug != b.primary_type_slug:
        return None

    typed = a if a.primary_type_slug else b
    located = a if a.latitude is not None and a.longitude is not None else b
    if not typed.primary_type_slug or located.latitude is None or located.longitude is None:
        return None

    return replace(
        located,
        primary_type_slug=typed.primary_type_slug,
        classification_confidence=max(
            _confidence(a.classification_confidence),
            _confidence(b.classification_confidence),
        ),
        phone=located.phone or typed.phone,
        website_url=located.website_url or typed.website_url,
        source_status="open" if "open" in {a.source_status, b.source_status} else located.source_status,
        category_evidence={
            "corroborated_sources": [a.source, b.source],
            "typed_by": typed.source,
            "typed_evidence": typed.category_evidence,
        },
    )


def _combined_quality(a: SourceRecord, b: SourceRecord, identity_score: float) -> float:
    typed_confidence = max(
        _confidence(a.classification_confidence),
        _confidence(b.classification_confidence),
    )
    combined = 0.65 * typed_confidence + 0.35 * float(identity_score)
    if a.primary_type_slug and a.primary_type_slug == b.primary_type_slug:
        combined += 0.03
    return min(0.99, combined)
