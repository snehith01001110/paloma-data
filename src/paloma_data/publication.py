from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from paloma_data.db import Database, execute_many
from paloma_data.normalizers import normalize_name
from paloma_data.taxonomy import BAR_TYPES, GENERIC_MANUFACTURER_TYPES


PUBLICATION_VERSION = "v1"
CONSUMER_FRESHNESS_DAYS = 550
PUBLIC_BAR_LICENSES = frozenset({"40", "42", "48", "61"})
MANUFACTURER_LICENSES = frozenset({"2", "23", "74"})
HARD_NEGATIVE_FLAGS = frozenset(
    {
        "delete",
        "doesnt_exist",
        "does_not_exist",
        "inappropriate",
        "privatevenue",
        "private_venue",
    }
)


@dataclass(frozen=True, slots=True)
class LinkedObservation:
    source: str
    source_family: str
    name: str
    source_status: str | None
    source_updated_at: datetime | None
    primary_type_slug: str | None
    consumer_facing: bool
    public_access: str
    quality_flags: tuple[str, ...]
    match_confidence: float
    permitted_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PublicationDecision:
    state: str
    reason: str
    access_mode: str = "unknown"
    primary_type_slug: str | None = None


class PublicationResolver:
    """Apply Paloma's product gate independently from entity matching and field scoring."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def resolve(self) -> dict[str, int]:
        counters = {"evaluated": 0, "published": 0, "candidate": 0, "review": 0, "suppressed": 0}
        with self.db.connection() as conn:
            establishments = conn.execute(
                """
                select e.id::text, e.name, e.normalized_name, e.status,
                       e.identity_confidence::float, e.display_name_confidence::float,
                       e.display_name_source, pt.slug as primary_type_slug
                from public.establishments e
                join public.primary_types pt on pt.id = e.primary_type_id
                where exists (
                    select 1 from ingest.establishment_sources es
                    where es.establishment_id = e.id
                )
                order by e.id
                """
            ).fetchall()

            observation_rows = conn.execute(
                """
                select es.establishment_id::text, sr.source, sr.source_family, sr.name,
                       sr.source_status, sr.source_updated_at, sr.primary_type_slug,
                       sr.consumer_facing, sr.public_access, sr.quality_flags,
                       es.match_confidence::float, sr.permitted_metadata
                from ingest.establishment_sources es
                join ingest.source_records sr
                  on sr.source = es.source and sr.source_record_id = es.source_record_id
                order by es.establishment_id, sr.source, sr.source_record_id
                """
            ).fetchall()
            grouped: dict[str, list[LinkedObservation]] = defaultdict(list)
            for row in observation_rows:
                grouped[str(row["establishment_id"])].append(_linked_observation(row))

            # Accuracy-first refresh: no stale published row remains visible while a new decision
            # is being computed. The following batched update then promotes only passing rows.
            conn.execute(
                """
                update public.establishments e
                set publication_state = 'candidate',
                    publication_reason = %s,
                    publication_evaluated_at = now(),
                    updated_at = now()
                where exists (
                  select 1 from ingest.establishment_sources es where es.establishment_id = e.id
                )
                """,
                (f"publication_resolution_in_progress:{PUBLICATION_VERSION}",),
            )
            conn.commit()

            updates: list[tuple[Any, ...]] = []
            for establishment in establishments:
                establishment_id = str(establishment["id"])
                decision = decide_publication(
                    dict(establishment), grouped.get(establishment_id, [])
                )
                updates.append(
                    (
                        decision.state,
                        decision.reason,
                        decision.access_mode,
                        decision.state,
                        decision.state,
                        decision.primary_type_slug,
                        decision.primary_type_slug,
                        establishment_id,
                    )
                )
                counters["evaluated"] += 1
                counters[decision.state] += 1
            for offset in range(0, len(updates), 500):
                execute_many(conn, _PUBLICATION_UPDATE_SQL, updates[offset : offset + 500])
                conn.commit()
        return counters

    def _observations(self, conn, establishment_id: str) -> list[LinkedObservation]:
        rows = conn.execute(
            """
            select sr.source, sr.source_family, sr.name, sr.source_status,
                   sr.source_updated_at, sr.primary_type_slug, sr.consumer_facing,
                   sr.public_access, sr.quality_flags, es.match_confidence::float,
                   sr.permitted_metadata
            from ingest.establishment_sources es
            join ingest.source_records sr
              on sr.source = es.source and sr.source_record_id = es.source_record_id
            where es.establishment_id = %s::uuid
            order by sr.source, sr.source_record_id
            """,
            (establishment_id,),
        ).fetchall()
        return [
            LinkedObservation(
                source=str(row["source"]),
                source_family=str(row["source_family"]),
                name=str(row["name"]),
                source_status=row["source_status"],
                source_updated_at=row["source_updated_at"],
                primary_type_slug=row["primary_type_slug"],
                consumer_facing=bool(row["consumer_facing"]),
                public_access=str(row["public_access"]),
                quality_flags=tuple(row["quality_flags"] or ()),
                match_confidence=float(row["match_confidence"] or 0.0),
                permitted_metadata=row["permitted_metadata"] or {},
            )
            for row in rows
        ]

    def _save(self, conn, establishment_id: str, decision: PublicationDecision) -> None:
        conn.execute(
            _PUBLICATION_UPDATE_SQL,
            (
                decision.state,
                decision.reason,
                decision.access_mode,
                decision.state,
                decision.state,
                decision.primary_type_slug,
                decision.primary_type_slug,
                establishment_id,
            ),
        )


_PUBLICATION_UPDATE_SQL = """
update public.establishments
set publication_state = %s,
    publication_reason = %s,
    access_mode = %s,
    public_access_verified_at = case
        when %s = 'published' then now()
        else public_access_verified_at
    end,
    published_at = case
        when %s = 'published' then coalesce(published_at, now())
        else published_at
    end,
    publication_evaluated_at = now(),
    primary_type_id = case
        when %s::text is null then primary_type_id
        else coalesce(
            (select id from public.primary_types where slug = %s::text),
            primary_type_id
        )
    end,
    updated_at = now()
where id = %s::uuid
"""


def _linked_observation(row: dict[str, Any]) -> LinkedObservation:
    return LinkedObservation(
        source=str(row["source"]),
        source_family=str(row["source_family"]),
        name=str(row["name"]),
        source_status=row["source_status"],
        source_updated_at=row["source_updated_at"],
        primary_type_slug=row["primary_type_slug"],
        consumer_facing=bool(row["consumer_facing"]),
        public_access=str(row["public_access"]),
        quality_flags=tuple(row["quality_flags"] or ()),
        match_confidence=float(row["match_confidence"] or 0.0),
        permitted_metadata=row["permitted_metadata"] or {},
    )


def decide_publication(
    establishment: dict[str, Any],
    observations: list[LinkedObservation],
    *,
    now: datetime | None = None,
) -> PublicationDecision:
    """Return a reasoned hard-gate decision; no composite score can bypass a missing fact."""
    current_time = now or datetime.now(timezone.utc)
    if establishment.get("status") == "closed":
        return _decision("suppressed", "canonical_closed")

    consumer = [row for row in observations if row.source_family == "consumer_poi"]
    flags = {flag for row in consumer for flag in row.quality_flags}
    if flags & HARD_NEGATIVE_FLAGS:
        return _decision("suppressed", "consumer_hard_negative")
    if "duplicate" in flags:
        return _decision("review", "consumer_duplicate_requires_merge")

    open_consumer = [
        row
        for row in consumer
        if row.consumer_facing
        and row.public_access == "walk_in"
        and row.source_status == "open"
        and not (set(row.quality_flags) & HARD_NEGATIVE_FLAGS)
    ]
    closed_consumer = [row for row in consumer if row.source_status == "closed"]
    if open_consumer and closed_consumer:
        return _decision("review", "consumer_status_conflict")
    if not open_consumer:
        if closed_consumer:
            return _decision("suppressed", "consumer_reported_closed")
        if any(row.primary_type_slug in GENERIC_MANUFACTURER_TYPES for row in consumer):
            return _decision("candidate", "manufacturer_without_access_evidence")
        return _decision("candidate", "missing_consumer_access_evidence")

    fresh_consumer = [
        row
        for row in open_consumer
        if _fresh(row.source_updated_at, current_time)
    ]
    if not fresh_consumer:
        return _decision("candidate", "stale_consumer_evidence")

    identity_confidence = _float(establishment.get("identity_confidence"))
    if identity_confidence < 0.90 or max(row.match_confidence for row in fresh_consumer) < 0.90:
        return _decision("candidate", "identity_not_resolved")

    chosen = max(fresh_consumer, key=_consumer_preference)
    if not _consumer_name_reliable(establishment, chosen):
        return _decision("candidate", "consumer_name_not_resolved")

    abc_codes = {
        _license_code(row.permitted_metadata.get("license_type"))
        for row in observations
        if row.source == "ca_abc" and row.source_status == "open"
    }
    abc_codes.discard("")
    if not abc_codes:
        return _decision("candidate", "missing_active_abc_license")

    primary_type = chosen.primary_type_slug
    if primary_type in BAR_TYPES and abc_codes & PUBLIC_BAR_LICENSES:
        return _decision(
            "published",
            "public_bar_license_and_current_consumer_poi",
            access_mode="walk_in",
            primary_type_slug=primary_type,
        )
    if primary_type == "taproom" and "23" in abc_codes:
        return _decision(
            "published",
            "brewery_license_and_explicit_taproom",
            access_mode="walk_in",
            primary_type_slug=primary_type,
        )
    if primary_type == "tasting_room" and abc_codes & MANUFACTURER_LICENSES:
        return _decision(
            "published",
            "manufacturer_license_and_explicit_tasting_room",
            access_mode="walk_in",
            primary_type_slug=primary_type,
        )
    if primary_type == "brewpub" and abc_codes & {"23", "75"}:
        return _decision(
            "published",
            "brewery_license_and_explicit_brewpub",
            access_mode="walk_in",
            primary_type_slug=primary_type,
        )
    return _decision("candidate", "license_does_not_support_consumer_venue_type")


def _consumer_name_reliable(
    establishment: dict[str, Any], observation: LinkedObservation
) -> bool:
    display_source = str(establishment.get("display_name_source") or "")
    display_confidence = _float(establishment.get("display_name_confidence"))
    canonical_name = str(establishment.get("normalized_name") or "")
    observed_name = normalize_name(observation.name)
    return bool(
        observed_name
        and canonical_name == observed_name
        and observation.match_confidence >= 0.90
        and display_source in {"fsq", "overture", "osm"}
        and display_confidence >= 0.72
    )


def _consumer_preference(row: LinkedObservation) -> tuple[int, int, float, float]:
    specificity = 0 if row.primary_type_slug == "bar" else 1
    source_preference = 1 if row.source == "fsq" else 0
    updated = row.source_updated_at
    timestamp = updated.timestamp() if isinstance(updated, datetime) else 0.0
    return specificity, source_preference, row.match_confidence, timestamp


def _fresh(value: datetime | None, now: datetime) -> bool:
    if value is None:
        return False
    timestamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc) >= now - timedelta(days=CONSUMER_FRESHNESS_DAYS)


def _license_code(value: Any) -> str:
    text = str(value or "").strip().lstrip("0")
    return text or "0" if value not in (None, "") else ""


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _decision(
    state: str,
    reason: str,
    *,
    access_mode: str = "unknown",
    primary_type_slug: str | None = None,
) -> PublicationDecision:
    return PublicationDecision(
        state=state,
        reason=f"{reason}:{PUBLICATION_VERSION}",
        access_mode=access_mode,
        primary_type_slug=primary_type_slug,
    )
