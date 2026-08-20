from __future__ import annotations

import json
from typing import Any

import psycopg

from paloma_data.catalog import (
    CATALOG_DECISION_VERSION,
    HARD_NEGATIVE_FLAGS,
    CatalogDecision,
    LinkedSource,
    VerificationEvidence,
)
from paloma_data.db import Database
from paloma_data.models import SourceRecord
from paloma_data.normalizers import normalize_address, normalize_name


NEIGHBORHOOD_BOUNDARY_GUARD_METERS = 10
POTENTIAL_SOURCE_EXCLUDED_FLAGS = HARD_NEGATIVE_FLAGS | frozenset(
    {"consumer_identity_conflict", "stale"}
)


class CatalogRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def discoverable_anchors(
        self,
        conn: psycopg.Connection,
        *,
        city: str | None,
        limit: int,
        anchor_sources: tuple[str, ...] = ("fsq",),
    ) -> list[SourceRecord]:
        rows = conn.execute(
            """
            select sr.*
            from ingest.source_records sr
            where sr.source = any(%s)
              and sr.retired_at is null
              and sr.source_status = 'open'
              and not (sr.quality_flags && %s::text[])
              and sr.consumer_facing
              and sr.public_access = 'walk_in'
              and sr.primary_type_slug is not null
              and sr.latitude is not null
              and sr.longitude is not null
              and (%s::text is null or lower(sr.city) = lower(%s::text))
              and not exists (
                select 1 from ingest.candidate_source_links csl
                where csl.source = sr.source
                  and csl.source_record_id = sr.source_record_id
              )
            order by
              case sr.source when 'fsq' then 0 else 1 end,
              sr.classification_confidence desc nulls last,
              sr.source_updated_at desc nulls last,
              sr.source_record_id
            limit %s
            """,
            (
                list(anchor_sources),
                sorted(POTENTIAL_SOURCE_EXCLUDED_FLAGS),
                city,
                city,
                limit,
            ),
        ).fetchall()
        return [_source_record(row) for row in rows]

    def candidate_id_for_source(
        self,
        conn: psycopg.Connection,
        record: SourceRecord,
    ) -> str | None:
        """Return the candidate that already owns an exact source identity, if any.

        ``discoverable_anchors`` materializes a batch before discovery starts. An anchor later
        in that batch can be linked while an earlier anchor is correlated. Rechecking here keeps
        the materialized batch idempotent and prevents an empty orphan candidate from being
        created for a source identity that is no longer unclaimed.
        """
        row = conn.execute(
            """
            select candidate_id::text
            from ingest.candidate_source_links
            where source = %s and source_record_id = %s
            limit 1
            """,
            (record.source, record.source_record_id),
        ).fetchone()
        return str(row["candidate_id"]) if row else None

    def candidate_matches(
        self,
        conn: psycopg.Connection,
        anchor: SourceRecord,
        *,
        limit: int = 10,
    ) -> list[tuple[str, SourceRecord]]:
        rows = conn.execute(
            """
            select c.id::text as candidate_id, sr.*
            from ingest.catalog_candidates c
            join ingest.source_records sr
              on sr.source = c.anchor_source
             and sr.source_record_id = c.anchor_source_record_id
            where lower(c.city) = lower(%s)
              and trim(c.country_code) = %s
              and (
                c.normalized_name OPERATOR(extensions.%%) %s
                or c.normalized_address OPERATOR(extensions.%%) %s
                or ST_DWithin(
                  c.location,
                  ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                  100
                )
              )
            order by greatest(
              extensions.similarity(c.normalized_name, %s),
              extensions.similarity(c.normalized_address, %s)
            ) desc
            limit %s
            """,
            (
                anchor.city,
                anchor.country_code,
                normalize_name(anchor.name),
                normalize_address(anchor.address),
                anchor.longitude,
                anchor.latitude,
                normalize_name(anchor.name),
                normalize_address(anchor.address),
                limit,
            ),
        ).fetchall()
        result: list[tuple[str, SourceRecord]] = []
        for row in rows:
            values = dict(row)
            candidate_id = str(values.pop("candidate_id"))
            result.append((candidate_id, _source_record(values)))
        return result

    def create_candidate(
        self, conn: psycopg.Connection, anchor: SourceRecord
    ) -> str:
        row = conn.execute(
            """
            insert into ingest.catalog_candidates (
              anchor_source, anchor_source_record_id,
              name, normalized_name, primary_type_slug,
              address, normalized_address, city, region, postal_code, country_code,
              location, candidate_state, decision_reason, decision_version
            ) values (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
              'discovered', %s, %s
            )
            on conflict (anchor_source, anchor_source_record_id) do update set
              name = excluded.name,
              normalized_name = excluded.normalized_name,
              primary_type_slug = excluded.primary_type_slug,
              address = excluded.address,
              normalized_address = excluded.normalized_address,
              city = excluded.city,
              region = excluded.region,
              postal_code = excluded.postal_code,
              country_code = excluded.country_code,
              location = excluded.location,
              updated_at = now()
            returning id::text
            """,
            (
                anchor.source,
                anchor.source_record_id,
                anchor.name,
                normalize_name(anchor.name),
                anchor.primary_type_slug,
                anchor.address,
                normalize_address(anchor.address),
                anchor.city,
                anchor.region,
                anchor.postal_code,
                anchor.country_code,
                anchor.longitude,
                anchor.latitude,
                f"not_evaluated:{CATALOG_DECISION_VERSION}",
                CATALOG_DECISION_VERSION,
            ),
        ).fetchone()
        return str(row["id"])

    def promote_anchor_if_better(
        self, conn: psycopg.Connection, candidate_id: str, anchor: SourceRecord
    ) -> None:
        if anchor.source != "fsq":
            return
        conn.execute(
            """
            update ingest.catalog_candidates
            set anchor_source = %s,
                anchor_source_record_id = %s,
                name = %s,
                normalized_name = %s,
                primary_type_slug = %s,
                address = %s,
                normalized_address = %s,
                city = %s,
                region = %s,
                postal_code = %s,
                country_code = %s,
                location = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                updated_at = now()
            where id = %s::uuid and anchor_source <> 'fsq'
            """,
            (
                anchor.source,
                anchor.source_record_id,
                anchor.name,
                normalize_name(anchor.name),
                anchor.primary_type_slug,
                anchor.address,
                normalize_address(anchor.address),
                anchor.city,
                anchor.region,
                anchor.postal_code,
                anchor.country_code,
                anchor.longitude,
                anchor.latitude,
                candidate_id,
            ),
        )

    def refresh_candidate_anchor(
        self, conn: psycopg.Connection, candidate_id: str, anchor: SourceRecord
    ) -> None:
        """Keep the candidate's denormalized identity aligned with its current anchor row."""
        conn.execute(
            """
            update ingest.catalog_candidates
            set name = %s,
                normalized_name = %s,
                primary_type_slug = %s,
                address = %s,
                normalized_address = %s,
                city = %s,
                region = %s,
                postal_code = %s,
                country_code = %s,
                location = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                updated_at = now()
            where id = %s::uuid
              and anchor_source = %s
              and anchor_source_record_id = %s
            """,
            (
                anchor.name,
                normalize_name(anchor.name),
                anchor.primary_type_slug,
                anchor.address,
                normalize_address(anchor.address),
                anchor.city,
                anchor.region,
                anchor.postal_code,
                anchor.country_code,
                anchor.longitude,
                anchor.latitude,
                candidate_id,
                anchor.source,
                anchor.source_record_id,
            ),
        )

    def link_source(
        self,
        conn: psycopg.Connection,
        candidate_id: str,
        record: SourceRecord,
        *,
        confidence: float,
        method: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        row = conn.execute(
            """
            insert into ingest.candidate_source_links (
              candidate_id, source, source_record_id, identity_confidence,
              match_method, origin_keys, last_checked_at, metadata
            ) values (%s::uuid, %s, %s, %s, %s, %s, now(), %s::jsonb)
            on conflict do nothing
            returning 1
            """,
            (
                candidate_id,
                record.source,
                record.source_record_id,
                confidence,
                method,
                list(record.origin_keys or (record.source,)),
                json.dumps(metadata or {}, sort_keys=True),
            ),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                update ingest.candidate_source_links
                set identity_confidence = %s,
                    match_method = %s,
                    origin_keys = %s,
                    last_checked_at = now(),
                    metadata = %s::jsonb
                where candidate_id = %s::uuid
                  and source = %s
                  and source_record_id = %s
                returning 1
                """,
                (
                    confidence,
                    method,
                    list(record.origin_keys or (record.source,)),
                    json.dumps(metadata or {}, sort_keys=True),
                    candidate_id,
                    record.source,
                    record.source_record_id,
                ),
            ).fetchone()
        linked = row is not None
        if linked:
            # A now-unambiguous link settles stale review prompts for this exact pairing.
            conn.execute(
                """
                update ingest.candidate_match_reviews
                set state = 'superseded', resolved_at = now()
                where candidate_id = %s::uuid
                  and source = %s and source_record_id = %s
                  and state = 'pending'
                """,
                (candidate_id, record.source, record.source_record_id),
            )
        return linked

    def potential_sources(
        self,
        conn: psycopg.Connection,
        anchor: SourceRecord,
        *,
        limit: int = 50,
    ) -> list[SourceRecord]:
        rows = conn.execute(
            """
            select sr.*
            from ingest.source_records sr
            where sr.retired_at is null
              and sr.source_status = 'open'
              and not (sr.quality_flags && %s::text[])
              and not (sr.source = %s and sr.source_record_id = %s)
              and lower(sr.city) = lower(%s)
              and trim(sr.country_code) = %s
              and (
                sr.normalized_name OPERATOR(extensions.%%) %s
                or sr.normalized_address OPERATOR(extensions.%%) %s
                or (
                  sr.latitude is not null and sr.longitude is not null
                  and ST_DWithin(
                    ST_SetSRID(ST_MakePoint(sr.longitude, sr.latitude), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                    100
                  )
                )
              )
            order by greatest(
              extensions.similarity(sr.normalized_name, %s),
              extensions.similarity(sr.normalized_address, %s)
            ) desc
            limit %s
            """,
            (
                sorted(POTENTIAL_SOURCE_EXCLUDED_FLAGS),
                anchor.source,
                anchor.source_record_id,
                anchor.city,
                anchor.country_code,
                normalize_name(anchor.name),
                normalize_address(anchor.address),
                anchor.longitude,
                anchor.latitude,
                normalize_name(anchor.name),
                normalize_address(anchor.address),
                limit,
            ),
        ).fetchall()
        return [_source_record(row) for row in rows]

    def enqueue_match_review(
        self,
        conn: psycopg.Connection,
        candidate_id: str,
        record: SourceRecord,
        *,
        reason: str,
        score: float,
        evidence: dict[str, Any],
    ) -> bool:
        encoded_evidence = json.dumps(evidence, sort_keys=True)
        resolved = conn.execute(
            """
            select 1
            from ingest.candidate_match_reviews
            where candidate_id = %s::uuid
              and source = %s
              and source_record_id = %s
              and reason = %s
              and state in ('accepted', 'rejected')
              and evidence = %s::jsonb
            limit 1
            """,
            (
                candidate_id,
                record.source,
                record.source_record_id,
                reason,
                encoded_evidence,
            ),
        ).fetchone()
        if resolved is not None:
            return False
        row = conn.execute(
            """
            insert into ingest.candidate_match_reviews (
              candidate_id, source, source_record_id, reason, score, evidence
            ) values (%s::uuid, %s, %s, %s, %s, %s::jsonb)
            on conflict (candidate_id, source, source_record_id, reason)
              where state = 'pending'
            do update set score = excluded.score, evidence = excluded.evidence
            returning id
            """,
            (
                candidate_id,
                record.source,
                record.source_record_id,
                reason,
                score,
                encoded_evidence,
            ),
        ).fetchone()
        return row is not None

    def resolve_match_review(
        self,
        conn: psycopg.Connection,
        review_id: int,
        *,
        resolution: str,
        resolved_by: str,
        note: str | None = None,
    ) -> str:
        states = {
            "same_place": "accepted",
            "not_same_or_stale": "rejected",
        }
        state = states.get(resolution)
        if state is None:
            raise ValueError("resolution must be same_place or not_same_or_stale")
        reviewer = resolved_by.strip()
        if not 3 <= len(reviewer) <= 200:
            raise ValueError("resolved_by must contain 3-200 non-whitespace characters")
        normalized_note = note.strip() if note is not None else None
        if normalized_note == "":
            normalized_note = None
        if normalized_note is not None and len(normalized_note) > 2000:
            raise ValueError("note must contain at most 2000 characters")
        recorded = conn.execute(
            """
            insert into ingest.candidate_match_review_resolutions (
              review_id, resolution, resolved_by, note, evidence
            )
            select id, %s, %s, %s,
                   jsonb_build_object(
                     'candidate_id', candidate_id,
                     'source', source,
                     'source_record_id', source_record_id,
                     'reason', reason,
                     'score', score,
                     'review_evidence', evidence
                   )
            from ingest.candidate_match_reviews
            where id = %s and state = 'pending'
            returning review_id
            """,
            (resolution, reviewer, normalized_note, review_id),
        ).fetchone()
        if recorded is None:
            raise ValueError("review does not exist or is no longer pending")
        row = conn.execute(
            """
            update ingest.candidate_match_reviews
            set state = %s, resolved_at = now()
            where id = %s and state = 'pending'
            returning candidate_id::text
            """,
            (state, review_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("review changed while its resolution was being recorded")
        return str(row["candidate_id"])

    def pending_match_review_candidate_id(
        self,
        conn: psycopg.Connection,
        review_id: int,
    ) -> str:
        row = conn.execute(
            """
            select candidate_id::text
            from ingest.candidate_match_reviews
            where id = %s and state = 'pending'
            for update
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            raise ValueError("review does not exist or is no longer pending")
        return str(row["candidate_id"])

    def pending_match_review(
        self,
        conn: psycopg.Connection,
        review_id: int,
    ) -> dict[str, Any]:
        """Lock and return the exact evidence a reviewer is about to decide."""
        row = conn.execute(
            """
            select r.candidate_id::text as candidate_id,
                   r.reason as review_reason, r.score as review_score,
                   r.evidence as review_evidence,
                   sr.*
            from ingest.candidate_match_reviews r
            join ingest.source_records sr
              on sr.source = r.source and sr.source_record_id = r.source_record_id
            where r.id = %s and r.state = 'pending'
            for update of r
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            raise ValueError("review does not exist or is no longer pending")
        values = dict(row)
        return {
            "candidate_id": str(values.pop("candidate_id")),
            "reason": str(values.pop("review_reason")),
            "score": float(values.pop("review_score")),
            "evidence": values.pop("review_evidence") or {},
            "record": _source_record(values),
        }

    def accepted_match_review(
        self,
        conn: psycopg.Connection,
        candidate_id: str,
        record: SourceRecord,
        *,
        reason: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return a same-place decision only while its full evidence fingerprint is current."""
        row = conn.execute(
            """
            select id, score::float as score
            from ingest.candidate_match_reviews
            where candidate_id = %s::uuid
              and source = %s and source_record_id = %s
              and reason = %s and state = 'accepted'
              and evidence = %s::jsonb
            order by resolved_at desc
            limit 1
            """,
            (
                candidate_id,
                record.source,
                record.source_record_id,
                reason,
                json.dumps(evidence, sort_keys=True),
            ),
        ).fetchone()
        return dict(row) if row else None

    def has_blocking_match_review(
        self,
        conn: psycopg.Connection,
        candidate_id: str,
    ) -> bool:
        """Return true for an unresolved current-name conflict at the exact premise.

        Nearby venues are common in dense nightlife districts and remain review hints. A second
        current record at the candidate's normalized street address is different: it can be a
        stale brand, a rebrand, or a nested venue, so publication must wait for resolution.
        """
        row = conn.execute(
            """
            select 1
            from ingest.catalog_candidates c
            join ingest.candidate_match_reviews r on r.candidate_id = c.id
            join ingest.source_records sr
              on sr.source = r.source and sr.source_record_id = r.source_record_id
            where c.id = %s::uuid
              and r.state = 'pending'
              and sr.retired_at is null
              and sr.source_status = 'open'
              and not (sr.quality_flags && %s::text[])
              and sr.normalized_address = c.normalized_address
              and (
                r.reason like '%%same_location_name_conflict'
                or r.reason like '%%probable_identity_needs_review'
              )
            limit 1
            """,
            (candidate_id, sorted(POTENTIAL_SOURCE_EXCLUDED_FLAGS)),
        ).fetchone()
        return row is not None

    def linked_sources(
        self, conn: psycopg.Connection, candidate_id: str
    ) -> list[LinkedSource]:
        rows = conn.execute(
            """
            select sr.*, csl.identity_confidence::float, csl.match_method
            from ingest.candidate_source_links csl
            join ingest.source_records sr
              on sr.source = csl.source and sr.source_record_id = csl.source_record_id
            where csl.candidate_id = %s::uuid
            order by sr.source, sr.source_record_id
            """,
            (candidate_id,),
        ).fetchall()
        return [
            LinkedSource(
                _source_record(row),
                float(row["identity_confidence"]),
                str(row["match_method"]),
            )
            for row in rows
        ]

    def unlink_source(
        self,
        conn: psycopg.Connection,
        candidate_id: str,
        record: SourceRecord,
    ) -> None:
        conn.execute(
            """
            delete from ingest.candidate_source_links
            where candidate_id = %s::uuid and source = %s and source_record_id = %s
            """,
            (candidate_id, record.source, record.source_record_id),
        )

    def verifications(
        self, conn: psycopg.Connection, candidate_id: str
    ) -> list[VerificationEvidence]:
        rows = conn.execute(
            """
            select verifier, verifier_record_id, outcome, verification_tier,
                   checks, permitted_snapshot, storage_policy, verified_at, expires_at
            from ingest.candidate_verifications
            where candidate_id = %s::uuid
            order by verified_at desc
            """,
            (candidate_id,),
        ).fetchall()
        return [
            VerificationEvidence(
                verifier=str(row["verifier"]),
                verifier_record_id=str(row["verifier_record_id"]),
                outcome=str(row["outcome"]),
                verification_tier=str(row["verification_tier"]),
                checks=row["checks"] or {},
                permitted_snapshot=row["permitted_snapshot"] or {},
                storage_policy=str(row["storage_policy"]),
                verified_at=row["verified_at"],
                expires_at=row["expires_at"],
            )
            for row in rows
        ]

    def save_verification(
        self,
        conn: psycopg.Connection,
        candidate_id: str,
        verification: VerificationEvidence,
    ) -> None:
        if verification.storage_policy not in {"contract", "manual"}:
            raise ValueError("Ephemeral provider content must never be persisted")
        conn.execute(
            """
            insert into ingest.candidate_verifications (
              candidate_id, verifier, verifier_record_id, outcome, verification_tier,
              checks, permitted_snapshot, storage_policy, verified_at, expires_at,
              decision_version
            ) values (
              %s::uuid, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s
            )
            """,
            (
                candidate_id,
                verification.verifier,
                verification.verifier_record_id,
                verification.outcome,
                verification.verification_tier,
                json.dumps(verification.checks, sort_keys=True),
                json.dumps(verification.permitted_snapshot, sort_keys=True),
                verification.storage_policy,
                verification.verified_at,
                verification.expires_at,
                CATALOG_DECISION_VERSION,
            ),
        )

    def save_evaluation(
        self,
        conn: psycopg.Connection,
        candidate_id: str,
        decision: CatalogDecision,
        *,
        mode: str,
        mutate_candidate: bool = True,
        persist_snapshot: bool = True,
    ) -> None:
        # API trial callers set persist_snapshot=False. Open-source discovery trials may safely
        # persist their Apache/public-record snapshot even though they never touch the app table.
        if mutate_candidate and decision.state == "verified" and not persist_snapshot:
            raise ValueError("A verified candidate cannot be mutated without its snapshot")
        snapshot = (
            dict(decision.resolved)
            if decision.state == "verified" and persist_snapshot
            else {}
        )
        if snapshot:
            self._attach_civic_neighborhood(conn, candidate_id, snapshot)
        conn.execute(
            """
            insert into ingest.catalog_evaluations (
              candidate_id, evaluation_mode, decision, reasons, decision_version, snapshot
            ) values (%s::uuid, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                candidate_id,
                mode,
                decision.state,
                list(decision.reasons),
                CATALOG_DECISION_VERSION,
                json.dumps(snapshot, sort_keys=True),
            ),
        )
        if not mutate_candidate:
            return
        conn.execute(
            """
            update ingest.catalog_candidates
            set candidate_state = %s,
                decision_reason = %s,
                decision_reasons = %s,
                decision_version = %s,
                identity_confidence = %s,
                verification_tier = %s,
                verified_at = %s,
                verification_expires_at = %s,
                resolved_snapshot = %s::jsonb,
                last_evaluated_at = now(),
                updated_at = now()
            where id = %s::uuid
            """,
            (
                decision.state,
                decision.reason,
                list(decision.reasons),
                CATALOG_DECISION_VERSION,
                decision.identity_confidence,
                decision.verification_tier,
                decision.verified_at,
                decision.expires_at,
                json.dumps(snapshot, sort_keys=True),
                candidate_id,
            ),
        )
        # Consumer reads are fail-closed. A lapsed provider lease, missing current anchor, or
        # newly ambiguous identity is just as disqualifying as an explicit closure.
        if decision.state != "verified":
            conn.execute(
                """
                update public.establishments
                set publication_state = 'suppressed',
                    publication_reason = %s,
                    publication_evaluated_at = now(),
                    updated_at = now()
                where catalog_candidate_id = %s::uuid
                  and publication_state = 'published'
                """,
                (decision.reason, candidate_id),
            )

    def candidate_ids(
        self,
        conn: psycopg.Connection,
        *,
        city: str | None = None,
        cities: tuple[str, ...] | None = None,
        limit: int = 100,
        states: tuple[str, ...] | None = None,
        decision_version: str | None = None,
    ) -> list[str]:
        rows = conn.execute(
            """
            select id::text
            from ingest.catalog_candidates
            where (%s::text is null or lower(city) = lower(%s::text))
              and (%s::text[] is null or lower(city) = any(%s::text[]))
              and (%s::text[] is null or candidate_state = any(%s::text[]))
              and (%s::text is null or decision_version = %s::text)
            order by updated_at, id
            limit %s
            """,
            (
                city,
                city,
                [value.casefold() for value in cities] if cities else None,
                [value.casefold() for value in cities] if cities else None,
                list(states) if states else None,
                list(states) if states else None,
                decision_version,
                decision_version,
                limit,
            ),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def materialized_candidate_ids(
        self,
        conn: psycopg.Connection,
        *,
        city: str | None = None,
        limit: int = 100,
        publication_states: tuple[str, ...] = ("published",),
    ) -> list[str]:
        """Select product identities already materialized from verified candidates."""
        rows = conn.execute(
            """
            select c.id::text
            from ingest.catalog_candidates c
            join public.establishments e on e.catalog_candidate_id = c.id
            where (%s::text is null or lower(e.city) = lower(%s::text))
              and e.publication_state = any(%s::text[])
            order by e.updated_at, e.id
            limit %s
            """,
            (city, city, list(publication_states), limit),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def materialized_publication(
        self,
        conn: psycopg.Connection,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            select e.id::text as establishment_id, e.publication_state
            from public.establishments e
            where e.catalog_candidate_id = %s::uuid
            for update
            """,
            (candidate_id,),
        ).fetchone()
        return dict(row) if row else None

    def verification_candidate_ids(
        self,
        conn: psycopg.Connection,
        *,
        city: str | None,
        limit: int,
        refresh_window_days: int = 14,
    ) -> list[str]:
        """Return only new/ambiguous candidates and leases that are actually due."""
        rows = conn.execute(
            """
            select id::text
            from ingest.catalog_candidates
            where (%s::text is null or lower(city) = lower(%s::text))
              and (
                candidate_state in ('needs_verification', 'needs_review')
                or (
                  candidate_state in ('verified', 'published')
                  and (
                    verification_expires_at is null
                    or verification_expires_at <= now() + make_interval(days => %s)
                  )
                )
              )
            order by
              case
                when candidate_state in ('verified', 'published') then 0
                when candidate_state = 'needs_verification' then 1
                else 2
              end,
              verification_expires_at asc nulls first,
              updated_at,
              id
            limit %s
            """,
            (city, city, refresh_window_days, limit),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def fsq_anchor(
        self, conn: psycopg.Connection, candidate_id: str
    ) -> SourceRecord | None:
        row = conn.execute(
            """
            select sr.*
            from ingest.candidate_source_links csl
            join ingest.source_records sr
              on sr.source = csl.source and sr.source_record_id = csl.source_record_id
            where csl.candidate_id = %s::uuid and sr.source = 'fsq'
            order by sr.source_updated_at desc nulls last
            limit 1
            """,
            (candidate_id,),
        ).fetchone()
        return _source_record(row) if row else None

    def neighborhood_at(
        self,
        conn: psycopg.Connection,
        *,
        city: str,
        latitude: float,
        longitude: float,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            select name, source, authority::float,
                   ST_Distance(
                     ST_Boundary(boundary)::geography,
                     ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                   )::float as boundary_distance_m
            from ingest.neighborhood_boundaries
            where retired_at is null
              and lower(jurisdiction) = lower(%s)
              and ST_Covers(
                boundary,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)
              )
              and ST_Distance(
                ST_Boundary(boundary)::geography,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
              ) >= %s
            order by authority desc, ST_Area(boundary::geography), source, source_record_id
            limit 1
            """,
            (
                longitude,
                latitude,
                city,
                longitude,
                latitude,
                longitude,
                latitude,
                NEIGHBORHOOD_BOUNDARY_GUARD_METERS,
            ),
        ).fetchone()
        return dict(row) if row else None

    def linked_source_neighborhood_consensus(
        self,
        conn: psycopg.Connection,
        *,
        candidate_id: str,
        city: str,
    ) -> dict[str, Any] | None:
        """Resolve boundary-adjacent points from independent linked coordinates.

        The canonical point is deliberately left unlabeled when it falls within the boundary
        guard. In that case, two independent upstream coordinate lineages may resolve the
        ambiguity. An origin that lands in multiple neighborhoods is discarded, and a tied
        result is never selected.
        """
        row = conn.execute(
            """
            with source_votes as (
              select csl.source, csl.source_record_id,
                     coalesce(nullif(csl.origin_keys, '{}'), array[csl.source]) as origin_keys,
                     neighborhood.name, neighborhood.source as boundary_source,
                     neighborhood.authority::float
              from ingest.candidate_source_links csl
              join ingest.source_records sr
                on sr.source = csl.source
               and sr.source_record_id = csl.source_record_id
              join lateral (
                select nb.name, nb.source, nb.authority
                from ingest.neighborhood_boundaries nb
                where nb.retired_at is null
                  and lower(nb.jurisdiction) = lower(%s)
                  and ST_Covers(
                    nb.boundary,
                    ST_SetSRID(ST_MakePoint(sr.longitude, sr.latitude), 4326)
                  )
                order by nb.authority desc, ST_Area(nb.boundary::geography),
                         nb.source, nb.source_record_id
                limit 1
              ) neighborhood on true
              where csl.candidate_id = %s::uuid
                and sr.retired_at is null
                and sr.latitude is not null
                and sr.longitude is not null
                and not (sr.quality_flags && %s::text[])
            ), origin_votes as (
              select distinct unnest(origin_keys) as origin_key,
                     name, boundary_source, authority
              from source_votes
            ), consistent_origins as (
              select origin_key, min(name) as name,
                     min(boundary_source) as boundary_source,
                     min(authority)::float as authority
              from origin_votes
              group by origin_key
              having count(distinct name) = 1
            ), tallies as (
              select name, min(boundary_source) as boundary_source,
                     min(authority)::float as authority,
                     count(*)::int as independent_votes,
                     array_agg(origin_key order by origin_key) as origin_keys
              from consistent_origins
              group by name
            ), winner as (
              select *
              from tallies
              order by independent_votes desc, name
              limit 1
            )
            select name,
                   boundary_source || ':linked_coordinate_consensus' as source,
                   least(authority, 0.96)::float as authority,
                   independent_votes,
                   origin_keys
            from winner
            where independent_votes >= 2
              and independent_votes > coalesce(
                (select max(t.independent_votes) from tallies t where t.name <> winner.name),
                0
              )
            """,
            (city, candidate_id, sorted(POTENTIAL_SOURCE_EXCLUDED_FLAGS)),
        ).fetchone()
        return dict(row) if row else None

    def _attach_civic_neighborhood(
        self,
        conn: psycopg.Connection,
        candidate_id: str,
        resolved: dict[str, Any],
    ) -> bool:
        if resolved.get("neighborhood"):
            return False
        neighborhood = self.neighborhood_at(
            conn,
            city=str(resolved["city"]),
            latitude=float(resolved["latitude"]),
            longitude=float(resolved["longitude"]),
        )
        if not neighborhood:
            neighborhood = self.linked_source_neighborhood_consensus(
                conn,
                candidate_id=candidate_id,
                city=str(resolved["city"]),
            )
        if not neighborhood:
            return False
        field_sources = dict(resolved.get("field_sources") or {})
        field_confidences = dict(resolved.get("field_confidences") or {})
        resolved["neighborhood"] = neighborhood["name"]
        field_sources["neighborhood"] = neighborhood["source"]
        field_confidences["neighborhood"] = neighborhood["authority"]
        resolved["field_sources"] = field_sources
        resolved["field_confidences"] = field_confidences
        return True

    def materialize(self, conn: psycopg.Connection, candidate_id: str) -> bool:
        candidate = conn.execute(
            """
            select *
            from ingest.catalog_candidates
            where id = %s::uuid
              and candidate_state = 'verified'
              and decision_version = %s
              and verification_expires_at > now()
            for update
            """,
            (candidate_id, CATALOG_DECISION_VERSION),
        ).fetchone()
        if not candidate:
            return False
        resolved = dict(candidate["resolved_snapshot"] or {})
        field_sources = dict(resolved.get("field_sources") or {})
        field_confidences = dict(resolved.get("field_confidences") or {})
        if self._attach_civic_neighborhood(conn, candidate_id, resolved):
            field_sources = dict(resolved.get("field_sources") or {})
            field_confidences = dict(resolved.get("field_confidences") or {})
            conn.execute(
                """
                update ingest.catalog_candidates
                set resolved_snapshot = %s::jsonb, updated_at = now()
                where id = %s::uuid
                """,
                (json.dumps(resolved, sort_keys=True), candidate_id),
            )
        current_projection = conn.execute(
            """
            select name, normalized_name, data_quality_score,
                   display_name_confidence, display_name_source,
                   field_resolution_version,
                   phone_e164, phone_source, phone_confidence,
                   website_url, website_source, website_confidence,
                   neighborhood, neighborhood_source, neighborhood_confidence,
                   hours, hours_source, hours_confidence,
                   price_level, price_source, price_confidence
            from public.establishments
            where id = %s::uuid
            """,
            (candidate_id,),
        ).fetchone()
        projection_owned = _overlay_public_field_projection(
            resolved,
            dict(current_projection) if current_projection else None,
        )
        field_sources = dict(resolved.get("field_sources") or {})
        field_confidences = dict(resolved.get("field_confidences") or {})
        type_row = conn.execute(
            "select id from public.primary_types where slug = %s",
            (resolved.get("primary_type_slug"),),
        ).fetchone()
        if not type_row:
            raise ValueError(f"Unknown primary type: {resolved.get('primary_type_slug')}")
        settings = list(resolved.get("setting_slugs") or ())
        identity = float(candidate["identity_confidence"] or 0.0)
        data_quality = identity
        display_name_confidence = 0.98
        field_resolution_version = CATALOG_DECISION_VERSION
        if projection_owned and current_projection:
            data_quality = float(current_projection["data_quality_score"] or identity)
            display_name_confidence = float(
                current_projection["display_name_confidence"] or 0.98
            )
            field_resolution_version = str(
                current_projection["field_resolution_version"]
            )

        conn.execute("select pg_advisory_xact_lock(hashtext('paloma_catalog_materialize'))")
        duplicate = conn.execute(
            """
            select id::text
            from public.establishments
            where id <> %s::uuid
              and publication_state = 'published'
              and normalized_address = %s
              and extensions.similarity(normalized_name, %s) >= 0.95
              and ST_DWithin(
                location,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                25
              )
            limit 1
            """,
            (
                candidate_id,
                resolved["normalized_address"],
                resolved["normalized_name"],
                resolved["longitude"],
                resolved["latitude"],
            ),
        ).fetchone()
        if duplicate:
            conn.execute(
                """
                update ingest.catalog_candidates
                set candidate_state = 'needs_review',
                    decision_reason = %s,
                    decision_reasons = array['possible_public_duplicate'],
                    updated_at = now()
                where id = %s::uuid
                """,
                (f"possible_public_duplicate:{CATALOG_DECISION_VERSION}", candidate_id),
            )
            return False
        conn.execute(
            """
            insert into public.establishments (
              id, catalog_candidate_id, name, normalized_name, primary_type_id,
              address, normalized_address, city, region, postal_code, country_code,
              location, phone_e164, website_url, neighborhood, hours, price_level,
              cover_image_url, status, last_verified_at, data_quality_score,
              identity_confidence, display_name_confidence, display_name_source,
              type_confidence, field_resolution_version,
              publication_state, publication_reason, access_mode,
              public_access_verified_at, publication_evaluated_at, published_at,
              phone_source, phone_confidence, website_source,
              website_confidence, neighborhood_source, neighborhood_confidence,
              hours_source, hours_confidence,
              price_source, price_confidence, verification_tier,
              verification_expires_at, verification_version, updated_at
            ) values (
              %s::uuid, %s::uuid, %s, %s, %s,
              %s, %s, %s, %s, %s, %s,
              ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
              %s, %s, %s, %s::jsonb, %s,
              null, 'open', %s, %s,
              %s, %s, %s, 0.98, %s,
              'published', %s, 'walk_in',
              %s, now(), coalesce(
                (select published_at from public.establishments where id = %s::uuid), now()
              ),
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
            )
            on conflict (id) do update set
              catalog_candidate_id = excluded.catalog_candidate_id,
              name = excluded.name,
              normalized_name = excluded.normalized_name,
              primary_type_id = excluded.primary_type_id,
              address = excluded.address,
              normalized_address = excluded.normalized_address,
              city = excluded.city,
              region = excluded.region,
              postal_code = excluded.postal_code,
              country_code = excluded.country_code,
              location = excluded.location,
              phone_e164 = excluded.phone_e164,
              website_url = excluded.website_url,
              neighborhood = excluded.neighborhood,
              hours = excluded.hours,
              price_level = excluded.price_level,
              status = 'open',
              last_verified_at = excluded.last_verified_at,
              data_quality_score = excluded.data_quality_score,
              identity_confidence = excluded.identity_confidence,
              display_name_confidence = excluded.display_name_confidence,
              display_name_source = excluded.display_name_source,
              type_confidence = excluded.type_confidence,
              field_resolution_version = excluded.field_resolution_version,
              publication_state = 'published',
              publication_reason = excluded.publication_reason,
              access_mode = 'walk_in',
              public_access_verified_at = excluded.public_access_verified_at,
              publication_evaluated_at = now(),
              phone_source = excluded.phone_source,
              phone_confidence = excluded.phone_confidence,
              website_source = excluded.website_source,
              website_confidence = excluded.website_confidence,
              neighborhood_source = excluded.neighborhood_source,
              neighborhood_confidence = excluded.neighborhood_confidence,
              hours_source = excluded.hours_source,
              hours_confidence = excluded.hours_confidence,
              price_source = excluded.price_source,
              price_confidence = excluded.price_confidence,
              verification_tier = excluded.verification_tier,
              verification_expires_at = excluded.verification_expires_at,
              verification_version = excluded.verification_version,
              closed_at = null,
              updated_at = now()
            """,
            (
                candidate_id,
                candidate_id,
                resolved["name"],
                resolved["normalized_name"],
                type_row["id"],
                resolved["address"],
                resolved["normalized_address"],
                resolved["city"],
                resolved.get("region"),
                resolved.get("postal_code"),
                resolved["country_code"],
                resolved["longitude"],
                resolved["latitude"],
                resolved.get("phone_e164"),
                resolved.get("website_url"),
                resolved.get("neighborhood"),
                json.dumps(resolved.get("hours"), sort_keys=True)
                if resolved.get("hours") is not None
                else None,
                resolved.get("price_level"),
                candidate["verified_at"],
                data_quality,
                identity,
                display_name_confidence,
                field_sources.get("name"),
                field_resolution_version,
                f"all_hard_gates_passed:{CATALOG_DECISION_VERSION}",
                candidate["verified_at"],
                candidate_id,
                field_sources.get("phone"),
                _projection_confidence(
                    field_confidences, "phone", resolved.get("phone_e164")
                ),
                field_sources.get("website"),
                _projection_confidence(
                    field_confidences, "website", resolved.get("website_url")
                ),
                field_sources.get("neighborhood"),
                field_confidences.get("neighborhood")
                if resolved.get("neighborhood")
                else None,
                field_sources.get("hours"),
                _projection_confidence(
                    field_confidences, "hours", resolved.get("hours")
                ),
                field_sources.get("price"),
                _projection_confidence(
                    field_confidences, "price", resolved.get("price_level")
                ),
                candidate["verification_tier"],
                candidate["verification_expires_at"],
                CATALOG_DECISION_VERSION,
            ),
        )
        if not projection_owned:
            conn.execute(
                "delete from public.establishment_settings where establishment_id = %s::uuid and source <> 'manual'",
                (candidate_id,),
            )
        if settings and not projection_owned:
            conn.execute(
                """
                insert into public.establishment_settings (
                  establishment_id, setting_id, source, confidence, last_verified_at
                )
                select %s::uuid, s.id, %s, 0.95, %s
                from public.settings s
                where s.slug = any(%s)
                on conflict (establishment_id, setting_id) do update set
                  source = excluded.source,
                  confidence = excluded.confidence,
                  last_verified_at = excluded.last_verified_at
                """,
                (
                    candidate_id,
                    field_sources.get("settings") or candidate["verification_tier"],
                    candidate["verified_at"],
                    settings,
                ),
            )
        conn.execute(
            """
            insert into ingest.establishment_sources (
              establishment_id, source, source_record_id, source_status, source_updated_at,
              last_seen_at, last_verified_at, match_confidence, match_method,
              matching_version, payload_hash, permitted_metadata
            )
            select %s::uuid, csl.source, csl.source_record_id,
                   sr.source_status, sr.source_updated_at, now(), now(),
                   csl.identity_confidence, csl.match_method, %s,
                   sr.payload_hash, sr.permitted_metadata
            from ingest.candidate_source_links csl
            join ingest.source_records sr
              on sr.source = csl.source and sr.source_record_id = csl.source_record_id
            where csl.candidate_id = %s::uuid
            on conflict (source, source_record_id) do update set
              establishment_id = excluded.establishment_id,
              source_status = excluded.source_status,
              source_updated_at = excluded.source_updated_at,
              last_seen_at = now(),
              last_verified_at = now(),
              match_confidence = excluded.match_confidence,
              match_method = excluded.match_method,
              matching_version = excluded.matching_version,
              payload_hash = excluded.payload_hash,
              permitted_metadata = excluded.permitted_metadata,
              updated_at = now()
            """,
            (candidate_id, CATALOG_DECISION_VERSION, candidate_id),
        )
        self.upsert_runtime_provider_links(conn, candidate_id)
        conn.execute(
            """
            update ingest.catalog_candidates
            set candidate_state = 'published', updated_at = now()
            where id = %s::uuid
            """,
            (candidate_id,),
        )
        return True

    def upsert_runtime_provider_links(
        self, conn: psycopg.Connection, candidate_id: str
    ) -> None:
        """Persist only provider IDs that their terms allow Paloma to retain."""
        conn.execute(
            """
            with eligible as materialized (
              select
                csl.source_record_id, csl.match_method, csl.identity_confidence,
                csl.linked_at, csl.last_checked_at
              from ingest.candidate_source_links csl
              join ingest.source_records source_record
                on source_record.source = csl.source
               and source_record.source_record_id = csl.source_record_id
              where csl.candidate_id = %s::uuid
                and csl.source = 'fsq'
                and csl.identity_confidence >= 0.96
                and source_record.retired_at is null
                and source_record.source_status = 'open'
                and source_record.consumer_facing
                and source_record.public_access = 'walk_in'
                and not (source_record.quality_flags && %s::text[])
              order by csl.identity_confidence desc, csl.last_checked_at desc
              limit 1
            ), upserted as (
              insert into runtime.runtime_provider_links as runtime_link (
                establishment_id, provider, provider_place_id, match_method,
                match_confidence, matched_at, last_validated_at, retired_at, updated_at
              )
              select
                %s::uuid, 'foursquare', eligible.source_record_id,
                eligible.match_method, eligible.identity_confidence,
                eligible.linked_at, eligible.last_checked_at, null, now()
              from eligible
              on conflict (establishment_id, provider) do update set
                provider_place_id = excluded.provider_place_id,
                match_method = excluded.match_method,
                match_confidence = excluded.match_confidence,
                matched_at = least(runtime_link.matched_at, excluded.matched_at),
                last_validated_at = greatest(
                  runtime_link.last_validated_at, excluded.last_validated_at
                ),
                retired_at = null,
                updated_at = now()
              returning establishment_id
            ), retired as (
              update runtime.runtime_provider_links runtime_link
              set retired_at = now(), updated_at = now()
              where runtime_link.establishment_id = %s::uuid
                and runtime_link.provider = 'foursquare'
                and runtime_link.retired_at is null
                and not exists (select 1 from eligible)
              returning establishment_id
            )
            select
              (select count(*) from upserted) as upserted_count,
              (select count(*) from retired) as retired_count
            """,
            (
                candidate_id,
                sorted(POTENTIAL_SOURCE_EXCLUDED_FLAGS),
                candidate_id,
                candidate_id,
            ),
        )

    def withdraw_expired(self, conn: psycopg.Connection) -> int:
        rows = conn.execute(
            """
            update public.establishments
            set publication_state = 'suppressed',
                publication_reason = %s,
                publication_evaluated_at = now(),
                updated_at = now()
            where publication_state = 'published'
              and catalog_candidate_id is not null
              and (verification_expires_at is null or verification_expires_at <= now())
            returning catalog_candidate_id
            """,
            (f"verification_expired:{CATALOG_DECISION_VERSION}",),
        ).fetchall()
        ids = [row["catalog_candidate_id"] for row in rows]
        if ids:
            conn.execute(
                """
                update ingest.catalog_candidates
                set candidate_state = 'needs_verification',
                    decision_reason = %s,
                    decision_reasons = array['verification_expired'],
                    updated_at = now()
                where id = any(%s)
                """,
                (f"verification_expired:{CATALOG_DECISION_VERSION}", ids),
            )
        return len(rows)

def _overlay_public_field_projection(
    resolved: dict[str, Any],
    current: dict[str, Any] | None,
) -> bool:
    """Keep the field resolver's public projection authoritative during identity refreshes.

    Candidate materialization owns identity and publication state. Once the rights-aware field
    resolver has projected canonical attributes, a later candidate refresh must preserve that
    projection—including deliberate NULLs—until the resolver writes a newer projection.
    """
    if not current:
        return False
    resolver_version = str(current.get("field_resolution_version") or "")
    if not resolver_version or resolver_version == CATALOG_DECISION_VERSION:
        return False

    resolved["name"] = current["name"]
    resolved["normalized_name"] = current["normalized_name"]
    field_sources = dict(resolved.get("field_sources") or {})
    field_confidences = dict(resolved.get("field_confidences") or {})
    field_sources["name"] = current.get("display_name_source")
    field_confidences["name"] = current.get("display_name_confidence")

    for resolved_key, source_key, confidence_key, provenance_key in (
        ("phone_e164", "phone_source", "phone_confidence", "phone"),
        ("website_url", "website_source", "website_confidence", "website"),
        (
            "neighborhood",
            "neighborhood_source",
            "neighborhood_confidence",
            "neighborhood",
        ),
        ("hours", "hours_source", "hours_confidence", "hours"),
        ("price_level", "price_source", "price_confidence", "price"),
    ):
        resolved[resolved_key] = current.get(resolved_key)
        field_sources[provenance_key] = current.get(source_key)
        field_confidences[provenance_key] = current.get(confidence_key)

    resolved["field_sources"] = field_sources
    resolved["field_confidences"] = field_confidences
    return True


def _projection_confidence(
    field_confidences: dict[str, Any], field_name: str, value: Any
) -> float | None:
    if value is None:
        return None
    confidence = field_confidences.get(field_name)
    return float(confidence) if confidence is not None else 0.95


def _source_record(row: dict[str, Any]) -> SourceRecord:
    return SourceRecord(
        source=str(row["source"]),
        source_record_id=str(row["source_record_id"]),
        name=str(row["name"]),
        address=str(row["address"]),
        city=str(row["city"]),
        region=row.get("region"),
        postal_code=row.get("postal_code"),
        country_code=str(row.get("country_code") or "US").strip(),
        latitude=float(row["latitude"]) if row.get("latitude") is not None else None,
        longitude=float(row["longitude"]) if row.get("longitude") is not None else None,
        phone=row.get("phone_e164"),
        website_url=row.get("website_url"),
        neighborhood=row.get("neighborhood"),
        hours=row.get("hours"),
        price_level=row.get("price_level"),
        setting_slugs=tuple(row.get("setting_slugs") or ()),
        source_status=row.get("source_status"),
        source_updated_at=row.get("source_updated_at"),
        primary_type_slug=row.get("primary_type_slug"),
        classification_confidence=(
            float(row["classification_confidence"])
            if row.get("classification_confidence") is not None
            else None
        ),
        source_family=str(row.get("source_family") or "unknown"),
        consumer_facing=bool(row.get("consumer_facing")),
        public_access=str(row.get("public_access") or "unknown"),
        quality_flags=tuple(row.get("quality_flags") or ()),
        origin_keys=tuple(row.get("origin_keys") or (row["source"],)),
        data_license=str(row.get("data_license") or "unknown"),
        storage_scope=str(row.get("storage_scope") or "durable"),
        provider_veracity=(
            int(row["provider_veracity"])
            if row.get("provider_veracity") is not None
            else None
        ),
        category_evidence=row.get("category_evidence") or {},
        permitted_metadata=row.get("permitted_metadata") or {},
        field_provenance=row.get("field_provenance") or {},
    )
