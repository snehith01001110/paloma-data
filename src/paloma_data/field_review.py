from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from paloma_data.db import Database


REVIEW_VERSION = "manual-review-v1"


@dataclass(frozen=True, slots=True)
class FieldReviewResult:
    conflict_id: int
    decision_id: int
    establishment_id: str
    field_name: str
    decision_status: str


class FieldConflictReviewer:
    """Close one field conflict while preserving an immutable decision trail."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def review(
        self,
        conflict_id: int,
        *,
        reviewer: str,
        notes: str,
        selected_evidence_id: str | None = None,
    ) -> FieldReviewResult:
        if not reviewer.strip() or not notes.strip():
            raise ValueError("reviewer and notes are required")
        with self.db.connection() as conn:
            conflict = conn.execute(
                """
                select id, establishment_id::text, field_name, state, evidence_ids
                from review.field_conflicts
                where id = %s
                for update
                """,
                (conflict_id,),
            ).fetchone()
            if conflict is None:
                raise ValueError(f"field conflict {conflict_id} does not exist")
            if conflict["state"] != "pending":
                raise ValueError(f"field conflict {conflict_id} is not pending")

            establishment_id = str(conflict["establishment_id"])
            field_name = str(conflict["field_name"])
            evidence = None
            if selected_evidence_id:
                evidence = conn.execute(
                    """
                    select id::text, value_text, normalized_value, value_json,
                           evidence_confidence::float, identity_confidence::float,
                           authority::float, upstream_origin_keys
                    from catalog.field_observations
                    where id = %s::uuid
                      and establishment_id = %s::uuid
                      and field_name = %s
                      and observation_status = 'asserted'
                      and (expires_at is null or expires_at > now())
                    """,
                    (selected_evidence_id, establishment_id, field_name),
                ).fetchone()
                if evidence is None:
                    raise ValueError(
                        "selected evidence is not current evidence for this conflict"
                    )

            current = conn.execute(
                """
                select id
                from catalog.current_field_decisions
                where establishment_id = %s::uuid and field_name = %s
                """,
                (establishment_id, field_name),
            ).fetchone()
            decision_status = "selected" if evidence else "unknown"
            confidence = _review_confidence(evidence) if evidence else None
            evidence_ids = [str(value) for value in (conflict["evidence_ids"] or [])]
            if selected_evidence_id and selected_evidence_id not in evidence_ids:
                evidence_ids.append(selected_evidence_id)
            fingerprint = _review_fingerprint(
                conflict_id,
                decision_status,
                selected_evidence_id,
                reviewer,
                notes,
            )
            decision = conn.execute(
                """
                insert into catalog.field_decisions (
                  establishment_id, field_name, decision_status, value_text,
                  normalized_value, value_json, confidence, resolver_version,
                  evidence_ids, independent_origin_keys, reason_codes,
                  supersedes_decision_id, decision_fingerprint, metadata
                ) values (
                  %s::uuid, %s, %s, %s, %s, %s::jsonb, %s, %s,
                  %s::uuid[], %s, %s, %s, %s,
                  jsonb_build_object(
                    'reviewer', %s::text, 'notes', %s::text,
                    'conflict_id', %s::bigint
                  )
                )
                returning id
                """,
                (
                    establishment_id,
                    field_name,
                    decision_status,
                    evidence["value_text"] if evidence else None,
                    evidence["normalized_value"] if evidence else None,
                    json.dumps(evidence["value_json"]) if evidence and evidence["value_json"] is not None else None,
                    confidence,
                    REVIEW_VERSION,
                    evidence_ids,
                    list(evidence["upstream_origin_keys"] or []) if evidence else [],
                    ["human_evidence_review"] if evidence else ["human_review_unverified"],
                    int(current["id"]) if current else None,
                    fingerprint,
                    reviewer,
                    notes,
                    conflict_id,
                ),
            ).fetchone()
            decision_id = int(decision["id"])
            _project_reviewed_value(
                conn,
                establishment_id,
                field_name,
                evidence,
                confidence,
            )
            conn.execute(
                """
                update review.field_conflicts
                set state = 'resolved', resolved_at = now(), resolved_by = %s,
                    resolution_notes = %s, decision_id = %s
                where id = %s
                """,
                (reviewer, notes, decision_id, conflict_id),
            )
            conn.commit()
        return FieldReviewResult(
            conflict_id=conflict_id,
            decision_id=decision_id,
            establishment_id=establishment_id,
            field_name=field_name,
            decision_status=decision_status,
        )


def _review_confidence(evidence: Any) -> float:
    return round(
        min(
            0.99,
            max(
                0.0,
                float(evidence["evidence_confidence"])
                * float(evidence["identity_confidence"])
                * (0.7 + 0.3 * float(evidence["authority"])),
            ),
        ),
        3,
    )


def _review_fingerprint(
    conflict_id: int,
    status: str,
    evidence_id: str | None,
    reviewer: str,
    notes: str,
) -> str:
    payload = json.dumps(
        {
            "conflict_id": conflict_id,
            "status": status,
            "evidence_id": evidence_id,
            "reviewer": reviewer,
            "notes": notes,
            "resolver_version": REVIEW_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode()).hexdigest()


def _project_reviewed_value(
    conn: Any,
    establishment_id: str,
    field_name: str,
    evidence: Any | None,
    confidence: float | None,
) -> None:
    scalar_columns = {
        "phone_e164": ("phone_e164", "phone_source", "phone_confidence"),
        "website_url": ("website_url", "website_source", "website_confidence"),
        "neighborhood": (
            "neighborhood",
            "neighborhood_source",
            "neighborhood_confidence",
        ),
    }
    if field_name in scalar_columns:
        value_column, source_column, confidence_column = scalar_columns[field_name]
        value = evidence["value_text"] if evidence else None
        source = "manual" if evidence else None
        conn.execute(
            f"""
            update public.establishments
            set {value_column} = %s, {source_column} = %s,
                {confidence_column} = %s, updated_at = now()
            where id = %s::uuid
            """,
            (value, source, confidence, establishment_id),
        )
    elif field_name == "address" and evidence:
        conn.execute(
            """
            update public.establishments
            set address = %s, updated_at = now()
            where id = %s::uuid
            """,
            (evidence["value_text"], establishment_id),
        )
    elif field_name == "hours":
        conn.execute(
            """
            update public.establishments
            set hours = %s::jsonb, hours_source = %s,
                hours_confidence = %s, updated_at = now()
            where id = %s::uuid
            """,
            (
                json.dumps(evidence["value_json"]) if evidence else None,
                "manual" if evidence else None,
                confidence,
                establishment_id,
            ),
        )
    elif field_name == "price_level":
        value = int(evidence["value_text"]) if evidence else None
        conn.execute(
            """
            update public.establishments
            set price_level = %s, price_source = %s,
                price_confidence = %s, updated_at = now()
            where id = %s::uuid
            """,
            (value, "manual" if evidence else None, confidence, establishment_id),
        )
