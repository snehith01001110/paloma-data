from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from paloma_data.db import Database, execute_many
from paloma_data.evidence_ledger import append_linked_source_observations
from paloma_data.hours_provenance import hours_observation_provenance
from paloma_data.normalizers import consumer_display_name, normalize_name

RESOLUTION_VERSION = "v6-hours-freshness"


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
    neighborhood_authority: float = 0.0
    hours_authority: float = 0.0
    price_authority: float = 0.0
    setting_authority: float = 0.0


SOURCE_POLICIES: dict[str, SourcePolicy] = {
    "ca_abc": SourcePolicy("legal", 0.99, 0.99, 0.99, 0.98),
    "datasf": SourcePolicy(
        "legal",
        0.88,
        0.95,
        0.92,
        0.84,
        neighborhood_authority=0.98,
    ),
    "overture": SourcePolicy(
        "display",
        0.84,
        0.91,
        0.88,
        0.80,
        phone_authority=0.88,
        website_authority=0.84,
        location_authority=0.95,
        setting_authority=0.78,
    ),
    "fsq": SourcePolicy(
        "display",
        0.90,
        0.91,
        0.90,
        0.84,
        phone_authority=0.90,
        website_authority=0.88,
        location_authority=0.96,
        hours_authority=0.92,
        price_authority=0.90,
        setting_authority=0.88,
    ),
    "osm": SourcePolicy(
        "display",
        0.82,
        0.88,
        0.84,
        0.72,
        phone_authority=0.84,
        website_authority=0.82,
        location_authority=0.92,
        hours_authority=0.76,
        setting_authority=0.78,
    ),
}


class FieldResolver:
    """Resolve every canonical field from provenance in bounded database operations."""

    def __init__(self, db: Database | None) -> None:
        self.db = db

    def refresh_and_resolve(self) -> dict[str, int]:
        if self.db is None:
            raise RuntimeError("A database is required to resolve fields")
        with self.db.connection() as conn:
            evidence_rows = self.refresh_source_evidence(conn)
            conn.commit()
            result = self.resolve(conn)
            conn.commit()
        result["evidence_refreshed"] = evidence_rows
        return result

    def refresh_source_evidence(self, conn) -> int:
        return append_linked_source_observations(conn)

    def resolve(self, conn) -> dict[str, int]:
        conn.execute("select pg_advisory_xact_lock(hashtext('paloma_field_resolver'))")
        establishments = conn.execute(
            """
            select e.id::text, e.name, e.normalized_name, e.phone_e164,
                   e.website_url, e.address,
                   ST_Y(e.location::geometry) as latitude,
                   ST_X(e.location::geometry) as longitude,
                   e.neighborhood, e.hours, e.price_level,
                   e.phone_source, e.website_source, e.neighborhood_source,
                   e.hours_source, e.hours_verified_at, e.hours_expires_at,
                   e.hours_source_url, e.hours_source_kind, e.price_source,
                   pt.slug as primary_type_slug
            from public.establishments e
            join public.primary_types pt on pt.id = e.primary_type_id
            where exists (
                select 1 from ingest.establishment_sources es where es.establishment_id = e.id
            )
            order by e.id
            """
        ).fetchall()
        identity_rows = conn.execute(
            """
            select es.establishment_id::text,
                   coalesce(sr.source_family, es.source) as source_family,
                   max(es.match_confidence)::float as confidence
            from ingest.establishment_sources es
            left join ingest.source_records sr
              on sr.source = es.source and sr.source_record_id = es.source_record_id
            group by es.establishment_id, coalesce(sr.source_family, es.source)
            """
        ).fetchall()
        evidence_rows = conn.execute(
            """
            select distinct on (
                     coalesce(establishment_id, candidate_id),
                     field_name, source, source_record_id
                   )
                   id::text as evidence_id,
                   coalesce(establishment_id, candidate_id)::text as establishment_id,
                   field_name,
                   value_text, normalized_value, value_json, source,
                   evidence_confidence::float, identity_confidence::float,
                   authority::float, source_updated_at, observed_at, expires_at,
                   upstream_origin_keys, source_items, metadata
            from catalog.field_observations
            where observation_status = 'asserted'
              and (expires_at is null or expires_at > now())
              and (field_name <> 'hours' or expires_at is not null)
              and field_name in (
                'display_name', 'primary_type_slug', 'phone_e164', 'website_url',
                'address', 'latitude', 'longitude', 'operating_status',
                'neighborhood', 'hours', 'price_level', 'setting_slug'
              )
            order by coalesce(establishment_id, candidate_id),
                     field_name, source, source_record_id,
                     observed_at desc, id desc
            """
        ).fetchall()
        setting_rows = conn.execute("select id, slug from public.settings").fetchall()
        current_decision_rows = conn.execute(
            """
            select establishment_id::text, field_name, evidence_ids,
                   resolver_version, decision_fingerprint
            from catalog.current_field_decisions
            """
        ).fetchall()

        identities: dict[str, list[float]] = defaultdict(list)
        for row in identity_rows:
            identities[str(row["establishment_id"])].append(
                _bounded(row["confidence"], 0.0)
            )
        evidence: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for row in evidence_rows:
            evidence[str(row["establishment_id"])][str(row["field_name"])].append(dict(row))
        setting_ids = {str(row["slug"]): int(row["id"]) for row in setting_rows}
        manual_reviews = {
            (str(row["establishment_id"]), str(row["field_name"])): {
                str(value) for value in (row["evidence_ids"] or [])
            }
            for row in current_decision_rows
            if str(row["resolver_version"]).startswith("manual-review-")
        }
        current_fingerprints = {
            (str(row["establishment_id"]), str(row["field_name"])): str(
                row["decision_fingerprint"]
            )
            for row in current_decision_rows
        }

        conn.execute(
            "delete from public.establishment_settings where source <> 'manual'"
        )

        metrics = {
            "resolved": 0,
            "renamed": 0,
            "low_name_confidence": 0,
            "name_conflicts": 0,
            "phones": 0,
            "websites": 0,
            "operating_statuses": 0,
            "neighborhoods": 0,
            "hours": 0,
            "prices": 0,
            "settings": 0,
        }
        updates: list[tuple[Any, ...]] = []
        decisions: list[tuple[Any, ...]] = []
        conflicts: list[tuple[Any, ...]] = []
        resolved_settings: list[tuple[str, int, str, float]] = []

        for establishment in establishments:
            establishment_id = str(establishment["id"])
            by_field = evidence[establishment_id]
            protected_fields = {
                field_name
                for field_name, rows in by_field.items()
                if _manual_review_covers_current_evidence(
                    manual_reviews.get((establishment_id, field_name)), rows
                )
            }
            identity = _identity_confidence(identities[establishment_id])
            display = self._rank_evidence(by_field["display_name"])
            type_confidence = self._type_confidence(
                by_field["primary_type_slug"],
                str(establishment["primary_type_slug"]),
                identity,
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
                margin = best_score - float(runner_up["score"]) if runner_up else best_score
                best_matches_current = best["normalized_value"] == current_normalized
                can_replace = (
                    best_matches_current
                    or (best["has_official_web"] and best_score >= 0.88)
                    or (
                        best["has_consumer_poi"]
                        and float(best["best_identity"]) >= 0.90
                        and best_score >= 0.72
                    )
                    or (
                        int(best["source_count"]) >= 2
                        and best_score >= 0.86
                        and margin >= 0.05
                    )
                )
                if can_replace:
                    chosen_name = consumer_display_name(str(best["value_text"]))
                    chosen_source = str(best["best_source"])
                    name_confidence = min(0.995, best_score)
                else:
                    name_confidence = min(0.79, best_score)
                    metrics["name_conflicts"] += 1

            if chosen_name != current_name:
                metrics["renamed"] += 1
            if name_confidence < 0.85:
                metrics["low_name_confidence"] += 1

            phone = self._select_attribute(by_field["phone_e164"], 0.68)
            website = self._select_attribute(by_field["website_url"], 0.68)
            address = self._select_attribute(by_field["address"], 0.68)
            latitude = self._select_attribute(by_field["latitude"], 0.68)
            longitude = self._select_attribute(by_field["longitude"], 0.68)
            operating_status = self._select_attribute(
                by_field["operating_status"], 0.68
            )
            neighborhood = self._select_neighborhood(by_field["neighborhood"])
            hours = self._select_attribute(by_field["hours"], 0.58)
            hours_provenance = hours_observation_provenance(hours)
            price = self._select_attribute(by_field["price_level"], 0.75)
            for selected, metric in (
                (phone, "phones"),
                (website, "websites"),
                (operating_status, "operating_statuses"),
                (neighborhood, "neighborhoods"),
                (hours, "hours"),
                (price, "prices"),
            ):
                if selected:
                    metrics[metric] += 1

            for candidate in self._rank_evidence(by_field["setting_slug"]):
                slug = str(candidate["normalized_value"] or candidate["value_text"])
                if float(candidate["score"]) < 0.58 or slug not in setting_ids:
                    continue
                resolved_settings.append(
                    (
                        establishment_id,
                        setting_ids[slug],
                        str(candidate["best_source"]),
                        float(candidate["score"]),
                    )
                )
                decisions.append(
                    _decision_row(establishment_id, "setting_slug", candidate)
                )
                metrics["settings"] += 1

            overall_quality = min(identity, name_confidence, type_confidence)
            updates.append(
                (
                    chosen_name,
                    normalize_name(chosen_name),
                    round(identity, 3),
                    round(name_confidence, 3),
                    chosen_source,
                    round(type_confidence, 3),
                    round(overall_quality, 3),
                    RESOLUTION_VERSION,
                    _selected_text(phone, establishment, "phone_e164", "phone_source"),
                    _selected_source(phone, establishment, "phone_source"),
                    _selected_score(phone),
                    _selected_text(
                        website, establishment, "website_url", "website_source"
                    ),
                    _selected_source(website, establishment, "website_source"),
                    _selected_score(website),
                    _selected_text(
                        neighborhood, establishment, "neighborhood", "neighborhood_source"
                    ),
                    _selected_source(neighborhood, establishment, "neighborhood_source"),
                    _selected_score(neighborhood),
                    _selected_json(hours),
                    _selected_source(
                        hours, establishment, "hours_source", retain_manual=False
                    ),
                    _selected_score(hours),
                    hours_provenance.verified_at if hours_provenance else None,
                    hours_provenance.expires_at if hours_provenance else None,
                    hours_provenance.source_url if hours_provenance else None,
                    hours_provenance.source_kind if hours_provenance else None,
                    _selected_price(price, establishment),
                    _selected_source(price, establishment, "price_source"),
                    _selected_score(price),
                    establishment_id,
                )
            )
            automatic_decisions = (
                    _decision_row(
                        establishment_id,
                        "display_name",
                        display[0] if chosen_source != "unresolved" and display else None,
                        fallback_value=chosen_name,
                        fallback_confidence=name_confidence,
                    ),
                    _decision_row(
                        establishment_id,
                        "primary_type_slug",
                        self._select_attribute(by_field["primary_type_slug"], 0.58),
                        fallback_value=str(establishment["primary_type_slug"]),
                        fallback_confidence=type_confidence,
                    ),
                    _decision_row(establishment_id, "phone_e164", phone),
                    _decision_row(establishment_id, "website_url", website),
                    _decision_row(establishment_id, "address", address),
                    _decision_row(establishment_id, "latitude", latitude),
                    _decision_row(establishment_id, "longitude", longitude),
                    _decision_row(
                        establishment_id, "operating_status", operating_status
                    ),
                    _decision_row(establishment_id, "neighborhood", neighborhood),
                    _decision_row(establishment_id, "hours", hours),
                    _decision_row(establishment_id, "price_level", price),
            )
            decisions.extend(
                row for row in automatic_decisions if row[1] not in protected_fields
            )
            for field_name, selected, high_risk in (
                ("phone_e164", phone, False),
                ("website_url", website, False),
                ("address", address, False),
                ("latitude", latitude, False),
                ("longitude", longitude, False),
                ("neighborhood", neighborhood, False),
                ("operating_status", operating_status, True),
                ("hours", hours, True),
                ("price_level", price, False),
            ):
                if field_name in protected_fields:
                    continue
                reason = _review_reason(
                    field_name, by_field[field_name], selected, high_risk
                )
                if reason:
                    evidence_ids = _conflict_evidence_ids(by_field[field_name])
                    conflicts.append(
                        (
                            establishment_id,
                            field_name,
                            reason,
                            evidence_ids,
                            90 if high_risk else 70,
                        )
                    )
            metrics["resolved"] += 1

        execute_many(
            conn,
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
                phone_e164 = %s,
                phone_source = %s,
                phone_confidence = %s,
                website_url = %s,
                website_source = %s,
                website_confidence = %s,
                neighborhood = %s,
                neighborhood_source = %s,
                neighborhood_confidence = %s,
                hours = %s::jsonb,
                hours_source = %s,
                hours_confidence = %s,
                hours_verified_at = %s,
                hours_expires_at = %s,
                hours_source_url = %s,
                hours_source_kind = %s,
                price_level = %s::smallint,
                price_source = %s,
                price_confidence = %s,
                updated_at = now()
            where id = %s::uuid
            """,
            updates,
        )
        _reapply_manual_projections(conn)
        decisions = _filter_changed_decisions(decisions, current_fingerprints)
        if decisions:
            execute_many(
                conn,
                """
                insert into catalog.field_decisions (
                  establishment_id, field_name, decision_status, value_text,
                  normalized_value, value_json, confidence, resolver_version,
                  evidence_ids, independent_origin_keys, reason_codes,
                  decision_fingerprint
                ) values (
                  %s::uuid, %s, %s, %s, %s, %s::jsonb, %s, %s,
                  %s::uuid[], %s, %s, %s
                )
                """,
                decisions,
            )
        if resolved_settings:
            execute_many(
                conn,
                """
                insert into public.establishment_settings (
                  establishment_id, setting_id, source, confidence, last_verified_at
                ) values (%s::uuid, %s, %s, %s, now())
                on conflict (establishment_id, setting_id) do nothing
                """,
                resolved_settings,
            )
        if conflicts:
            execute_many(
                conn,
                """
                insert into review.field_conflicts (
                  establishment_id, field_name, reason, evidence_ids, priority
                ) values (%s::uuid, %s, %s, %s::uuid[], %s)
                on conflict (establishment_id, field_name, reason)
                  where state = 'pending'
                do update set evidence_ids = excluded.evidence_ids,
                              priority = excluded.priority
                """,
                conflicts,
            )
        conn.execute(
            """
            update review.field_conflicts conflict
            set state = 'resolved', resolved_at = now(),
                resolved_by = %s,
                resolution_notes = 'Resolver normalization produced an unambiguous selected decision.'
            where conflict.state = 'pending'
              and conflict.reason = 'conflicting_admissible_evidence'
              and exists (
                select 1
                from catalog.current_field_decisions decision
                where decision.establishment_id = conflict.establishment_id
                  and decision.field_name = conflict.field_name
                  and decision.decision_status = 'selected'
                  and decision.decided_at >= conflict.created_at
              )
            """,
            (RESOLUTION_VERSION,),
        )
        return metrics

    def _rank_evidence(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            key = str(row.get("normalized_value") or row.get("value_text") or "")
            if key:
                grouped[key].append(row)

        candidates: list[dict[str, Any]] = []
        for normalized_value, matching in grouped.items():
            scored = sorted(
                ((self._evidence_score(row), row) for row in matching),
                key=lambda item: item[0],
                reverse=True,
            )
            base_score, best = scored[0]
            value_row = best
            if str(best.get("field_name")) == "website_url":
                value_row = max(
                    matching,
                    key=lambda row: (
                        str(row.get("value_text") or "").startswith("https://"),
                        self._evidence_score(row),
                    ),
                )
            independent_origins = {
                str(origin)
                for row in matching
                for origin in (
                    row.get("upstream_origin_keys")
                    or (_source_family(str(row["source"])),)
                )
            }
            agreement_bonus = min(0.08, 0.04 * max(0, len(independent_origins) - 1))
            candidates.append(
                {
                    "normalized_value": normalized_value,
                    "value_text": value_row["value_text"],
                    "value_json": value_row.get("value_json"),
                    "evidence_id": value_row["evidence_id"],
                    "evidence_ids": sorted(
                        {str(row["evidence_id"]) for row in matching}
                    ),
                    "independent_origin_keys": sorted(independent_origins),
                    "best_source": value_row["source"],
                    "observed_at": value_row.get("observed_at"),
                    "expires_at": value_row.get("expires_at"),
                    "source_items": value_row.get("source_items") or [],
                    "metadata": value_row.get("metadata") or {},
                    "source_count": len(independent_origins),
                    "has_official_web": any(
                        row["source"] == "official_web" for row in matching
                    ),
                    "has_consumer_poi": any(
                        _source_family(str(row["source"])) == "consumer_poi"
                        for row in matching
                    ),
                    "best_identity": _bounded(best["identity_confidence"], 0.0),
                    "score": min(0.995, base_score + agreement_bonus),
                }
            )
        candidates.sort(key=lambda row: float(row["score"]), reverse=True)
        return candidates

    def _select_neighborhood(
        self, rows: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Prefer the reviewed civic polygon vocabulary over broader registration labels."""
        for source in ("datasf_neighborhoods", "overture_divisions"):
            preferred = [row for row in rows if str(row.get("source")) == source]
            if preferred:
                return self._select_attribute(preferred, 0.68)
        return self._select_attribute(rows, 0.68)

    def _select_attribute(
        self, rows: list[dict[str, Any]], minimum_score: float
    ) -> dict[str, Any] | None:
        ranked = self._rank_evidence(rows)
        if not ranked or float(ranked[0]["score"]) < minimum_score:
            return None
        if len(ranked) > 1 and float(ranked[0]["score"]) - float(ranked[1]["score"]) < 0.06:
            first_origins = set(ranked[0].get("independent_origin_keys") or ())
            second_origins = set(ranked[1].get("independent_origin_keys") or ())
            if (
                first_origins & second_origins
                and _direct_origin_for_source(str(ranked[0]["best_source"]))
                in first_origins
            ):
                return ranked[0]
            return None
        return ranked[0]

    def _type_confidence(
        self,
        rows: list[dict[str, Any]],
        current_type: str,
        identity: float,
    ) -> float:
        matching = [row for row in rows if row["normalized_value"] == current_type]
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
            * _freshness_factor(
                row.get("source_updated_at"), str(row.get("field_name") or "")
            )
        )


def resolve_candidate_observations(
    conn: Any,
    candidate_id: str,
    resolved_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Overlay admissible private-candidate observations onto a verified snapshot."""
    rows = conn.execute(
        """
        select distinct on (field_name, source, source_record_id)
               id::text as evidence_id, field_name, value_text, normalized_value,
               value_json, source, evidence_confidence::float,
               identity_confidence::float, authority::float, source_updated_at,
               observed_at, expires_at, upstream_origin_keys, source_items, metadata
        from catalog.field_observations
        where candidate_id = %s::uuid
          and observation_status = 'asserted'
          and (expires_at is null or expires_at > now())
          and (field_name <> 'hours' or expires_at is not null)
          and field_name in (
            'phone_e164', 'website_url', 'neighborhood', 'hours',
            'price_level', 'setting_slug'
          )
        order by field_name, source, source_record_id, observed_at desc, id desc
        """,
        (candidate_id,),
    ).fetchall()
    by_field: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_field[str(row["field_name"])].append(dict(row))

    result = dict(resolved_snapshot)
    field_sources = dict(result.get("field_sources") or {})
    field_confidences = dict(result.get("field_confidences") or {})
    field_evidence_ids = dict(result.get("field_evidence_ids") or {})
    resolution_status = dict(result.get("field_resolution_status") or {})
    resolver = FieldResolver(None)
    specs = (
        ("phone_e164", "phone_e164", "phone", 0.68, False),
        ("website_url", "website_url", "website", 0.68, False),
        ("neighborhood", "neighborhood", "neighborhood", 0.68, False),
        ("hours", "hours", "hours", 0.58, True),
        ("price_level", "price_level", "price", 0.75, True),
    )
    for field_name, output_key, provenance_key, minimum, use_json in specs:
        evidence = by_field[field_name]
        if not evidence:
            continue
        selected = (
            resolver._select_neighborhood(evidence)
            if field_name == "neighborhood"
            else resolver._select_attribute(evidence, minimum)
        )
        selected = _require_candidate_contact_corroboration(field_name, selected)
        if selected is None:
            result[output_key] = None
            if field_name == "hours":
                result.pop("hours_provenance", None)
            field_sources[provenance_key] = None
            field_confidences[provenance_key] = None
            field_evidence_ids[provenance_key] = []
            distinct_values = {
                str(row.get("normalized_value") or row.get("value_text"))
                for row in evidence
            }
            resolution_status[provenance_key] = (
                "conflicted" if len(distinct_values) > 1 else "insufficient"
            )
            continue
        value: Any = selected.get("value_json") if use_json else selected["value_text"]
        if field_name == "price_level":
            try:
                value = int(selected["value_text"])
            except (TypeError, ValueError):
                value = None
            if value not in range(1, 5):
                value = None
        result[output_key] = value
        if field_name == "hours":
            provenance = hours_observation_provenance(selected)
            if provenance:
                result["hours_provenance"] = {
                    "verified_at": provenance.verified_at.isoformat(),
                    "expires_at": provenance.expires_at.isoformat(),
                    "source_url": provenance.source_url,
                    "source_kind": provenance.source_kind,
                }
            else:
                result.pop("hours_provenance", None)
        field_sources[provenance_key] = str(selected["best_source"])
        field_confidences[provenance_key] = round(float(selected["score"]), 3)
        field_evidence_ids[provenance_key] = list(
            selected.get("evidence_ids") or [selected["evidence_id"]]
        )
        resolution_status[provenance_key] = "selected"

    setting_evidence = by_field["setting_slug"]
    if setting_evidence:
        known_settings = {
            str(row["slug"])
            for row in conn.execute("select slug from public.settings").fetchall()
        }
        settings: list[str] = []
        evidence_ids: set[str] = set()
        sources: set[str] = set()
        scores: list[float] = []
        for candidate in resolver._rank_evidence(setting_evidence):
            slug = str(candidate["normalized_value"] or candidate["value_text"])
            if float(candidate["score"]) < 0.58 or slug not in known_settings:
                continue
            settings.append(slug)
            evidence_ids.update(
                str(value)
                for value in (
                    candidate.get("evidence_ids") or [candidate["evidence_id"]]
                )
            )
            sources.add(str(candidate["best_source"]))
            scores.append(float(candidate["score"]))
        result["setting_slugs"] = sorted(set(settings))
        field_sources["settings"] = "+".join(sorted(sources)) if settings else None
        field_confidences["settings"] = round(min(scores), 3) if scores else None
        field_evidence_ids["settings"] = sorted(evidence_ids)
        resolution_status["settings"] = "selected" if settings else "insufficient"

    result["field_sources"] = field_sources
    result["field_confidences"] = field_confidences
    result["field_evidence_ids"] = field_evidence_ids
    result["field_resolution_status"] = resolution_status
    return result


def _require_candidate_contact_corroboration(
    field_name: str,
    selected: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Fail closed on unreviewed candidate contact facts from a single origin."""
    if selected is None or field_name not in {"phone_e164", "website_url"}:
        return selected
    if str(selected.get("best_source")) == "manual":
        return selected
    if int(selected.get("source_count") or 0) < 2:
        return None
    return selected


def _identity_confidence(confidences: list[float]) -> float:
    if not confidences:
        return 0.0
    ordered = sorted(confidences, reverse=True)
    if len(ordered) == 1:
        return min(0.94, 0.90 * ordered[0])
    miss_probability = 1.0
    for value in ordered[:4]:
        miss_probability *= 1.0 - (0.90 * value)
    return min(0.995, max(0.0, 1.0 - miss_probability))


def _selected_text(
    selected: dict[str, Any] | None,
    current: dict[str, Any],
    value_key: str,
    source_key: str,
) -> str | None:
    if selected:
        return str(selected["value_text"])
    return current.get(value_key) if current.get(source_key) == "manual" else None


def _selected_json(selected: dict[str, Any] | None) -> str | None:
    if selected:
        value = selected.get("value_json")
        if value is None:
            value = selected.get("value_text")
        return json.dumps(value, sort_keys=True)
    return None


def _selected_price(
    selected: dict[str, Any] | None, current: dict[str, Any]
) -> int | None:
    if selected:
        try:
            value = int(selected["value_text"])
        except (TypeError, ValueError):
            return None
        return value if value in range(1, 5) else None
    if current.get("price_source") == "manual":
        return current.get("price_level")
    return None


def _selected_source(
    selected: dict[str, Any] | None,
    current: dict[str, Any],
    source_key: str,
    *,
    retain_manual: bool = True,
) -> str | None:
    if selected:
        return str(selected["best_source"])
    return "manual" if retain_manual and current.get(source_key) == "manual" else None


def _selected_score(selected: dict[str, Any] | None) -> float | None:
    return round(float(selected["score"]), 3) if selected else None


def _decision_row(
    establishment_id: str,
    field_name: str,
    selected: dict[str, Any] | None,
    *,
    fallback_value: str | None = None,
    fallback_confidence: float | None = None,
) -> tuple[Any, ...]:
    if selected:
        value_text = str(selected["value_text"])
        normalized_value = str(selected["normalized_value"])
        value_json = selected.get("value_json")
        confidence = round(float(selected["score"]), 3)
        evidence_ids = list(selected.get("evidence_ids") or [selected["evidence_id"]])
        origin_keys = list(selected.get("independent_origin_keys") or ())
        status = "selected"
        reasons = ["highest_scoring_admissible_evidence"]
    elif fallback_value is not None:
        value_text = fallback_value
        normalized_value = normalize_name(fallback_value) if field_name == "display_name" else fallback_value
        value_json = None
        confidence = round(float(fallback_confidence or 0.0), 3)
        evidence_ids = []
        origin_keys = []
        status = "selected"
        reasons = ["retained_existing_canonical_value"]
    else:
        value_text = normalized_value = value_json = confidence = None
        evidence_ids = []
        origin_keys = []
        status = "unknown"
        reasons = ["no_admissible_unconflicted_evidence"]
    serialized_json = (
        json.dumps(value_json, sort_keys=True, separators=(",", ":"))
        if value_json is not None
        else None
    )
    fingerprint_payload = json.dumps(
        {
            "establishment_id": establishment_id,
            "field_name": field_name,
            "status": status,
            "value_text": value_text,
            "value_json": value_json,
            "confidence": confidence,
            "evidence_ids": evidence_ids,
            "resolver_version": RESOLUTION_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        establishment_id,
        field_name,
        status,
        value_text,
        normalized_value,
        serialized_json,
        confidence,
        RESOLUTION_VERSION,
        evidence_ids,
        origin_keys,
        reasons,
        sha256(fingerprint_payload.encode()).hexdigest(),
    )


def _filter_changed_decisions(
    decisions: list[tuple[Any, ...]],
    current_fingerprints: dict[tuple[str, str], str],
) -> list[tuple[Any, ...]]:
    """Append a decision event only when it changes the current projection.

    Fingerprints are content identities, not globally unique event identities. A field may
    legitimately return to a previously seen decision after evidence expires or is superseded.
    """
    changed: list[tuple[Any, ...]] = []
    latest = dict(current_fingerprints)
    for decision in decisions:
        key = (str(decision[0]), str(decision[1]))
        fingerprint = str(decision[-1])
        if latest.get(key) == fingerprint:
            continue
        changed.append(decision)
        latest[key] = fingerprint
    return changed


def _review_reason(
    field_name: str,
    rows: list[dict[str, Any]],
    selected: dict[str, Any] | None,
    high_risk: bool,
) -> str | None:
    distinct_values = {
        str(row.get("normalized_value") or row.get("value_text"))
        for row in rows
        if row.get("normalized_value") or row.get("value_text")
    }
    if selected is None and len(distinct_values) > 1:
        return "conflicting_admissible_evidence"
    if field_name == "hours" and selected and len(distinct_values) > 1:
        return "authoritative_hours_disagreement"
    if field_name == "hours" and selected:
        provenance = hours_observation_provenance(selected)
        if provenance and provenance.source_kind in {"first_party", "merchant"}:
            return None
    if high_risk and selected and len(selected.get("independent_origin_keys") or ()) < 2:
        return "single_origin_high_risk_field"
    return None


def _conflict_evidence_ids(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {str(row["evidence_id"]) for row in rows if row.get("evidence_id")}
    )


def _manual_review_covers_current_evidence(
    reviewed_evidence_ids: set[str] | None,
    rows: list[dict[str, Any]],
) -> bool:
    if reviewed_evidence_ids is None:
        return False
    current_ids = set(_conflict_evidence_ids(rows))
    return bool(current_ids) and current_ids <= reviewed_evidence_ids


def _reapply_manual_projections(conn: Any) -> None:
    """Keep reviewed durable values stable across catalog materialization/resolution."""
    conn.execute(
        """
        with decisions as (
          select * from catalog.current_field_decisions
          where resolver_version like 'manual-review-%'
            and field_name = 'phone_e164'
        )
        update public.establishments e
        set phone_e164 = case when d.decision_status = 'selected' then d.value_text end,
            phone_source = case when d.decision_status = 'selected' then 'manual' end,
            phone_confidence = case when d.decision_status = 'selected' then d.confidence end,
            updated_at = now()
        from decisions d where d.establishment_id = e.id
        """
    )
    conn.execute(
        """
        with decisions as (
          select * from catalog.current_field_decisions
          where resolver_version like 'manual-review-%'
            and field_name = 'website_url'
        )
        update public.establishments e
        set website_url = case when d.decision_status = 'selected' then d.value_text end,
            website_source = case when d.decision_status = 'selected' then 'manual' end,
            website_confidence = case when d.decision_status = 'selected' then d.confidence end,
            updated_at = now()
        from decisions d where d.establishment_id = e.id
        """
    )
    conn.execute(
        """
        with decisions as (
          select * from catalog.current_field_decisions
          where resolver_version like 'manual-review-%'
            and field_name = 'address' and decision_status = 'selected'
        )
        update public.establishments e
        set address = d.value_text,
            normalized_address = coalesce(d.normalized_value, d.value_text),
            updated_at = now()
        from decisions d where d.establishment_id = e.id
        """
    )
    conn.execute(
        """
        with decisions as (
          select * from catalog.current_field_decisions
          where resolver_version like 'manual-review-%'
            and field_name = 'latitude' and decision_status = 'selected'
        )
        update public.establishments e
        set location = st_setsrid(
              st_makepoint(st_x(e.location::geometry), d.value_text::double precision),
              4326
            )::geography,
            updated_at = now()
        from decisions d where d.establishment_id = e.id
        """
    )
    conn.execute(
        """
        with decisions as (
          select * from catalog.current_field_decisions
          where resolver_version like 'manual-review-%'
            and field_name = 'longitude' and decision_status = 'selected'
        )
        update public.establishments e
        set location = st_setsrid(
              st_makepoint(d.value_text::double precision, st_y(e.location::geometry)),
              4326
            )::geography,
            updated_at = now()
        from decisions d where d.establishment_id = e.id
        """
    )
    conn.execute(
        """
        with decisions as (
          select * from catalog.current_field_decisions
          where resolver_version like 'manual-review-%'
            and field_name = 'operating_status' and decision_status = 'selected'
        )
        update public.establishments e
        set status = d.value_text, updated_at = now()
        from decisions d where d.establishment_id = e.id
        """
    )
    conn.execute(
        """
        with decisions as (
          select * from catalog.current_field_decisions
          where resolver_version like 'manual-review-%'
            and field_name = 'neighborhood'
        )
        update public.establishments e
        set neighborhood = case when d.decision_status = 'selected' then d.value_text end,
            neighborhood_source = case when d.decision_status = 'selected' then 'manual' end,
            neighborhood_confidence = case when d.decision_status = 'selected' then d.confidence end,
            updated_at = now()
        from decisions d where d.establishment_id = e.id
        """
    )
    conn.execute(
        """
        with decisions as (
          select * from catalog.current_field_decisions
          where resolver_version like 'manual-review-%'
            and field_name = 'hours'
        ), selected as (
          select d.*,
                 observation.id as observation_id,
                 observation.observed_at,
                 observation.expires_at,
                 observation.source,
                 observation.source_items,
                 observation.metadata as observation_metadata,
                 source_link.url as source_url
          from decisions d
          left join lateral (
            select evidence.*
            from catalog.field_observations evidence
            where evidence.id = any(d.evidence_ids)
              and evidence.field_name = 'hours'
              and evidence.observation_status = 'asserted'
              and evidence.expires_at > now()
              and evidence.value_json = d.value_json
            order by
              (evidence.metadata->>'evidence_kind' = 'first_party') desc,
              (evidence.source = 'merchant') desc,
              evidence.authority desc,
              evidence.evidence_confidence desc,
              evidence.observed_at desc,
              evidence.id desc
            limit 1
          ) observation on true
          left join lateral (
            select item->>'url' as url
            from jsonb_array_elements(coalesce(observation.source_items, '[]'::jsonb)) item
            where item->>'url' ~ '^https://[^[:space:]]+$'
            order by (item->>'kind' = 'first_party') desc, item->>'url'
            limit 1
          ) source_link on true
        )
        update public.establishments e
        set hours = case
              when d.decision_status = 'selected' and d.observation_id is not null
                then d.value_json
            end,
            hours_source = case
              when d.decision_status = 'selected' and d.observation_id is not null
                then 'manual'
            end,
            hours_confidence = case
              when d.decision_status = 'selected' and d.observation_id is not null
                then d.confidence
            end,
            hours_verified_at = case
              when d.decision_status = 'selected' and d.observation_id is not null
                then d.observed_at
            end,
            hours_expires_at = case
              when d.decision_status = 'selected' and d.observation_id is not null
                then d.expires_at
            end,
            hours_source_url = case
              when d.decision_status = 'selected' and d.observation_id is not null
                then d.source_url
            end,
            hours_source_kind = case
              when d.decision_status <> 'selected' or d.observation_id is null then null
              when d.observation_metadata->>'evidence_kind' = 'first_party'
                or exists (
                  select 1
                  from jsonb_array_elements(coalesce(d.source_items, '[]'::jsonb)) item
                  where item->>'kind' = 'first_party'
                ) then 'first_party'
              when d.source = 'merchant' then 'merchant'
              when d.source = 'firsthand' then 'firsthand'
              when d.source = 'manual' then 'manual_review'
              when d.source in (
                'ca_abc', 'datasf', 'datasf_neighborhoods', 'fsq',
                'osm', 'overture', 'wikidata'
              ) then 'open_data'
              else 'other'
            end,
            updated_at = now()
        from selected d where d.establishment_id = e.id
        """
    )
    conn.execute(
        """
        with decisions as (
          select * from catalog.current_field_decisions
          where resolver_version like 'manual-review-%'
            and field_name = 'price_level'
        )
        update public.establishments e
        set price_level = case
              when d.decision_status = 'selected' then d.value_text::smallint
            end,
            price_source = case when d.decision_status = 'selected' then 'manual' end,
            price_confidence = case when d.decision_status = 'selected' then d.confidence end,
            updated_at = now()
        from decisions d where d.establishment_id = e.id
        """
    )


def _bounded(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _freshness_factor(value: Any, field_name: str = "") -> float:
    if value is None or not isinstance(value, datetime):
        return 0.70 if field_name in {"hours", "price_level"} else 0.92
    timestamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    age_days = max(0, (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).days)
    if field_name == "hours":
        if age_days <= 180:
            return 1.0
        if age_days <= 365:
            return 0.90
        if age_days <= 730:
            return 0.70
        return 0.50
    if field_name == "price_level":
        if age_days <= 365:
            return 1.0
        if age_days <= 730:
            return 0.80
        return 0.60
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
        "osm": "community",
        "overture_divisions": "community_boundary",
        "official_web": "first_party",
        "canonical_seed": "canonical_seed",
    }.get(source, source)


def _direct_origin_for_source(source: str) -> str:
    return {"fsq": "foursquare"}.get(source, source)
