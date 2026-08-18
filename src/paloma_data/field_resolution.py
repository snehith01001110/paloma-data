from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from paloma_data.db import Database
from paloma_data.normalizers import normalize_address, normalize_name

RESOLUTION_VERSION = "v3"


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    name_kind: str | None
    name_authority: float
    address_authority: float
    type_authority: float
    status_authority: float
    phone_authority: float = 0.0
    website_authority: float = 0.0
    location_authority: float = 0.0


SOURCE_POLICIES: dict[str, SourcePolicy] = {
    # Government/registry names describe the licensee or registered business. They are deliberately
    # legal-name evidence and never compete directly with a current consumer-facing display name.
    "ca_abc": SourcePolicy("legal", 0.99, 0.99, 0.99, 0.98),
    "datasf": SourcePolicy("legal", 0.88, 0.95, 0.92, 0.84),
    # Overture is useful consumer-place evidence, but a high place confidence does not mean its
    # display name is necessarily current. Keep the name authority below official first-party web.
    "overture": SourcePolicy("display", 0.84, 0.91, 0.88, 0.80, 0.88, 0.84, 0.95),
    "fsq": SourcePolicy("display", 0.90, 0.91, 0.90, 0.84, 0.90, 0.88, 0.96),
    # Optional community POI evidence. It can corroborate a current name but cannot beat a verified
    # first-party website by itself.
    "osm": SourcePolicy("display", 0.82, 0.88, 0.84, 0.72, 0.84, 0.82, 0.92),
}


class FieldResolver:
    """Build field provenance and resolve canonical fields independently from entity matching."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def refresh_and_resolve(self) -> dict[str, int]:
        with self.db.connection() as conn:
            evidence_rows = self.refresh_source_evidence(conn)
            result = self.resolve(conn)
            conn.commit()
        result["evidence_refreshed"] = evidence_rows
        return result

    def refresh_source_evidence(self, conn) -> int:
        rows = conn.execute(
            """
            select es.establishment_id::text, es.source, es.source_record_id,
                   es.match_confidence, sr.name, sr.address, sr.phone_e164, sr.website_url,
                   sr.primary_type_slug, sr.source_status, sr.latitude, sr.longitude,
                   sr.source_updated_at, sr.classification_confidence
            from ingest.establishment_sources es
            join ingest.source_records sr
              on sr.source = es.source and sr.source_record_id = es.source_record_id
            order by es.establishment_id, es.source
            """
        ).fetchall()

        count = 0
        for row in rows:
            policy = SOURCE_POLICIES.get(str(row["source"]))
            if not policy:
                continue
            identity = _bounded(row["match_confidence"], default=0.75)
            updated = row["source_updated_at"]
            classification = _bounded(row["classification_confidence"], default=0.75)

            if row["name"] and policy.name_kind:
                field_name = "display_name" if policy.name_kind == "display" else "legal_name"
                self._upsert_evidence(
                    conn,
                    establishment_id=row["establishment_id"],
                    field_name=field_name,
                    value=str(row["name"]),
                    normalized_value=normalize_name(str(row["name"])),
                    source=str(row["source"]),
                    source_record_id=str(row["source_record_id"]),
                    claim_kind=policy.name_kind,
                    evidence_confidence=0.92 if policy.name_kind == "display" else 0.98,
                    identity_confidence=identity,
                    authority=policy.name_authority,
                    source_updated_at=updated,
                    metadata={"role": f"{policy.name_kind}_name"},
                )
                count += 1

            values = (
                ("address", row["address"], normalize_address(row["address"]), policy.address_authority, 0.97),
                ("phone_e164", row["phone_e164"], row["phone_e164"], policy.phone_authority, 0.96),
                ("website_url", row["website_url"], row["website_url"], policy.website_authority, 0.92),
                ("primary_type_slug", row["primary_type_slug"], row["primary_type_slug"], policy.type_authority, classification),
                ("status", row["source_status"], row["source_status"], policy.status_authority, 0.94),
                ("latitude", row["latitude"], row["latitude"], policy.location_authority, 0.98),
                ("longitude", row["longitude"], row["longitude"], policy.location_authority, 0.98),
            )
            for field_name, value, normalized, authority, evidence_confidence in values:
                if value is None or authority <= 0:
                    continue
                self._upsert_evidence(
                    conn,
                    establishment_id=row["establishment_id"],
                    field_name=field_name,
                    value=str(value),
                    normalized_value=str(normalized) if normalized is not None else None,
                    source=str(row["source"]),
                    source_record_id=str(row["source_record_id"]),
                    claim_kind="observed",
                    evidence_confidence=evidence_confidence,
                    identity_confidence=identity,
                    authority=authority,
                    source_updated_at=updated,
                    metadata={},
                )
                count += 1

        # Preserve the pre-resolver canonical name only as a low-authority fallback. ON CONFLICT DO
        # NOTHING is intentional: after a rename, the old canonical value remains available as an
        # alias/evidence instead of being rewritten to the new selection.
        conn.execute(
            """
            insert into ingest.establishment_field_evidence (
                establishment_id, field_name, value_text, normalized_value, source,
                source_record_id, claim_kind, evidence_confidence, identity_confidence, authority,
                metadata
            )
            select e.id, 'display_name', e.name, e.normalized_name, 'canonical_seed',
                   'seed:v1', 'display', 0.65, 0.80, 0.55,
                   jsonb_build_object('role', 'pre_resolver_canonical')
            from public.establishments e
            where exists (
                select 1 from ingest.establishment_sources es where es.establishment_id = e.id
            )
            on conflict (establishment_id, field_name, source, source_record_id) do nothing
            """
        )
        return count

    def resolve(self, conn) -> dict[str, int]:
        establishments = conn.execute(
            """
            select e.id::text, e.name, e.normalized_name, e.data_quality_score,
                   pt.slug as primary_type_slug
            from public.establishments e
            join public.primary_types pt on pt.id = e.primary_type_id
            where exists (
                select 1 from ingest.establishment_sources es where es.establishment_id = e.id
            )
            order by e.id
            """
        ).fetchall()

        metrics = {"resolved": 0, "renamed": 0, "low_name_confidence": 0, "name_conflicts": 0}
        for establishment in establishments:
            establishment_id = establishment["id"]
            identity = self._identity_confidence(conn, establishment_id)
            display = self._resolve_display_name(conn, establishment_id)
            type_confidence = self._type_confidence(
                conn, establishment_id, str(establishment["primary_type_slug"]), identity
            )

            current_name = str(establishment["name"])
            current_normalized = establishment["normalized_name"] or normalize_name(current_name)
            chosen_name = current_name
            chosen_source = "unresolved"
            name_confidence = 0.45

            if display:
                best = display[0]
                runner_up = display[1] if len(display) > 1 else None
                best_score = float(best["score"])
                best_sources = int(best["source_count"])
                best_is_official = bool(best["has_official_web"])
                best_is_consumer = bool(best["has_consumer_poi"])
                best_identity = float(best["best_identity"])
                margin = best_score - float(runner_up["score"]) if runner_up else best_score
                best_matches_current = best["normalized_value"] == current_normalized

                # A changed display name is allowed only with first-party verification or strong
                # independent agreement. A lone aggregator can lower confidence but cannot rename.
                can_replace = (
                    best_matches_current
                    or (best_is_official and best_score >= 0.88)
                    or (best_is_consumer and best_identity >= 0.90 and best_score >= 0.72)
                    or (best_sources >= 2 and best_score >= 0.86 and margin >= 0.05)
                )
                if can_replace:
                    chosen_name = str(best["value_text"])
                    chosen_source = str(best["best_source"])
                    name_confidence = min(0.995, best_score)
                else:
                    name_confidence = min(0.79, best_score)
                    metrics["name_conflicts"] += 1

                conn.execute(
                    "update ingest.establishment_field_evidence set selected = false where establishment_id = %s::uuid and field_name = 'display_name'",
                    (establishment_id,),
                )
                if can_replace:
                    conn.execute(
                        """
                        update ingest.establishment_field_evidence
                        set selected = true, resolution_score = %s, updated_at = now()
                        where id = %s::uuid
                        """,
                        (best_score, best["evidence_id"]),
                    )

            if chosen_name != current_name:
                metrics["renamed"] += 1
            if name_confidence < 0.85:
                metrics["low_name_confidence"] += 1

            # data_quality_score is now a weakest-critical-field score. It can no longer be 0.99
            # merely because two source rows are the same physical place while the display name is
            # stale or uncertain.
            overall_quality = min(identity, name_confidence, type_confidence)
            conn.execute(
                """
                update public.establishments
                set name = %s,
                    normalized_name = %s,
                    identity_confidence = %s,
                    display_name_confidence = %s,
                    display_name_source = %s,
                    type_confidence = %s,
                    data_quality_score = %s,
                    field_resolution_version = %s,
                    updated_at = now()
                where id = %s::uuid
                """,
                (
                    chosen_name,
                    normalize_name(chosen_name),
                    round(identity, 3),
                    round(name_confidence, 3),
                    chosen_source,
                    round(type_confidence, 3),
                    round(overall_quality, 3),
                    RESOLUTION_VERSION,
                    establishment_id,
                ),
            )
            metrics["resolved"] += 1
        return metrics

    def _identity_confidence(self, conn, establishment_id: str) -> float:
        rows = conn.execute(
            """
            select coalesce(sr.source_family, es.source) as source_family,
                   max(es.match_confidence)::float as confidence
            from ingest.establishment_sources es
            left join ingest.source_records sr
              on sr.source = es.source and sr.source_record_id = es.source_record_id
            where establishment_id = %s::uuid
            group by coalesce(sr.source_family, es.source)
            order by confidence desc
            """,
            (establishment_id,),
        ).fetchall()
        confidences = [_bounded(row["confidence"], 0.0) for row in rows]
        if not confidences:
            return 0.0
        if len(confidences) == 1:
            return min(0.94, 0.90 * confidences[0])
        # Two independent linked sources are substantially stronger than one. Additional sources
        # provide diminishing returns; cap below certainty.
        miss_probability = 1.0
        for value in confidences[:4]:
            miss_probability *= 1.0 - (0.90 * value)
        return min(0.995, max(0.0, 1.0 - miss_probability))

    def _resolve_display_name(self, conn, establishment_id: str) -> list[dict[str, Any]]:
        evidence = conn.execute(
            """
            select id::text as evidence_id, value_text, normalized_value, source,
                   evidence_confidence::float, identity_confidence::float, authority::float,
                   source_updated_at
            from ingest.establishment_field_evidence
            where establishment_id = %s::uuid and field_name = 'display_name'
              and normalized_value is not null and normalized_value <> ''
            """,
            (establishment_id,),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in evidence:
            grouped[str(row["normalized_value"])].append(dict(row))

        candidates: list[dict[str, Any]] = []
        for normalized_value, rows in grouped.items():
            scored = sorted(
                ((self._evidence_score(row), row) for row in rows), key=lambda item: item[0], reverse=True
            )
            base_score, best = scored[0]
            independent_families = {
                _source_family(str(row["source"]))
                for row in rows
                if row["source"] != "canonical_seed"
            }
            agreement_bonus = min(0.08, 0.04 * max(0, len(independent_families) - 1))
            score = min(0.995, base_score + agreement_bonus)
            candidates.append(
                {
                    "normalized_value": normalized_value,
                    "value_text": best["value_text"],
                    "evidence_id": best["evidence_id"],
                    "best_source": best["source"],
                    "source_count": len(independent_families),
                    "has_official_web": any(row["source"] == "official_web" for row in rows),
                    "has_consumer_poi": any(
                        _source_family(str(row["source"])) == "consumer_poi" for row in rows
                    ),
                    "best_identity": _bounded(best["identity_confidence"], 0.0),
                    "score": score,
                }
            )
        candidates.sort(key=lambda row: float(row["score"]), reverse=True)
        return candidates

    def _type_confidence(self, conn, establishment_id: str, current_type: str, identity: float) -> float:
        rows = conn.execute(
            """
            select normalized_value, source, evidence_confidence::float, identity_confidence::float,
                   authority::float, source_updated_at
            from ingest.establishment_field_evidence
            where establishment_id = %s::uuid and field_name = 'primary_type_slug'
            """,
            (establishment_id,),
        ).fetchall()
        matching = [dict(row) for row in rows if row["normalized_value"] == current_type]
        if not matching:
            return min(0.70, identity)
        best = max(self._evidence_score(row) for row in matching)
        source_families = {_source_family(str(row["source"])) for row in matching}
        return min(0.995, best + min(0.06, 0.03 * max(0, len(source_families) - 1)))

    def _evidence_score(self, row: dict[str, Any]) -> float:
        identity_factor = 0.70 + (0.30 * _bounded(row.get("identity_confidence"), 0.0))
        return (
            _bounded(row.get("authority"), 0.0)
            * _bounded(row.get("evidence_confidence"), 0.0)
            * identity_factor
            * _freshness_factor(row.get("source_updated_at"))
        )

    def _upsert_evidence(
        self,
        conn,
        *,
        establishment_id: str,
        field_name: str,
        value: str,
        normalized_value: str | None,
        source: str,
        source_record_id: str,
        claim_kind: str,
        evidence_confidence: float,
        identity_confidence: float,
        authority: float,
        source_updated_at,
        metadata: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            insert into ingest.establishment_field_evidence (
                establishment_id, field_name, value_text, normalized_value, source,
                source_record_id, claim_kind, evidence_confidence, identity_confidence, authority,
                source_updated_at, metadata
            ) values (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            on conflict (establishment_id, field_name, source, source_record_id) do update set
                value_text = excluded.value_text,
                normalized_value = excluded.normalized_value,
                claim_kind = excluded.claim_kind,
                evidence_confidence = excluded.evidence_confidence,
                identity_confidence = excluded.identity_confidence,
                authority = excluded.authority,
                source_updated_at = excluded.source_updated_at,
                metadata = excluded.metadata,
                updated_at = now()
            """,
            (
                establishment_id,
                field_name,
                value,
                normalized_value,
                source,
                source_record_id,
                claim_kind,
                round(_bounded(evidence_confidence, 0.0), 3),
                round(_bounded(identity_confidence, 0.0), 3),
                round(_bounded(authority, 0.0), 3),
                source_updated_at,
                __import__("json").dumps(metadata, sort_keys=True),
            ),
        )


def _bounded(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _freshness_factor(value: Any) -> float:
    if value is None:
        return 0.92
    if not isinstance(value, datetime):
        return 0.92
    timestamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    age_days = max(0, (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).days)
    if age_days <= 365:
        return 1.0
    if age_days <= 1095:
        return 0.96
    return 0.88


def _source_family(source: str) -> str:
    return {
        "ca_abc": "government_regulator",
        "datasf": "government_registry",
        "overture": "consumer_poi",
        "fsq": "consumer_poi",
        "osm": "consumer_poi",
        "official_web": "first_party",
        "canonical_seed": "canonical_seed",
    }.get(source, source)
