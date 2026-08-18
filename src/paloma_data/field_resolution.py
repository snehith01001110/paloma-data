from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from typing import Any

from paloma_data.db import Database
from paloma_data.normalizers import normalize_name

RESOLUTION_VERSION = "v4"


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
        policies = [
            {"source": source, **asdict(policy)} for source, policy in SOURCE_POLICIES.items()
        ]
        cursor = conn.execute(
            """
            with policies as (
              select *
              from jsonb_to_recordset(%s::jsonb) as p(
                source text,
                name_kind text,
                name_authority double precision,
                address_authority double precision,
                type_authority double precision,
                status_authority double precision,
                phone_authority double precision,
                website_authority double precision,
                location_authority double precision,
                neighborhood_authority double precision,
                hours_authority double precision,
                price_authority double precision,
                setting_authority double precision
              )
            ), linked as (
              select es.establishment_id, es.source as claim_source, es.source_record_id,
                     coalesce(es.match_confidence, 0.75)::double precision as identity_confidence,
                     sr.name, sr.normalized_name, sr.address, sr.normalized_address,
                     sr.phone_e164, sr.website_url, sr.primary_type_slug, sr.source_status,
                     sr.latitude, sr.longitude, sr.neighborhood, sr.hours, sr.price_level,
                     sr.setting_slugs, sr.source_updated_at, sr.classification_confidence,
                     p.name_kind, p.name_authority, p.address_authority, p.type_authority,
                     p.status_authority, p.phone_authority, p.website_authority,
                     p.location_authority, p.neighborhood_authority, p.hours_authority,
                     p.price_authority, p.setting_authority
              from ingest.establishment_sources es
              join ingest.source_records sr
                on sr.source = es.source and sr.source_record_id = es.source_record_id
              join policies p on p.source = es.source
            ), scalar_claims as (
              select l.establishment_id, claim.field_name, claim.value_text,
                     claim.normalized_value, claim.value_json, l.claim_source as source,
                     l.source_record_id, claim.claim_kind,
                     claim.evidence_confidence, l.identity_confidence,
                     claim.authority, l.source_updated_at, claim.metadata
              from linked l
              cross join lateral (
                values
                  (
                    case when l.name_kind = 'display' then 'display_name'
                         when l.name_kind = 'legal' then 'legal_name' end,
                    l.name,
                    l.normalized_name,
                    null::jsonb,
                    coalesce(l.name_kind, 'observed'),
                    case when l.name_kind = 'display' then 0.92 else 0.98 end,
                    l.name_authority,
                    jsonb_build_object('role', coalesce(l.name_kind, 'observed') || '_name')
                  ),
                  ('address', l.address, l.normalized_address, null::jsonb, 'observed', 0.97, l.address_authority, '{}'::jsonb),
                  ('phone_e164', l.phone_e164, l.phone_e164, null::jsonb, 'observed', 0.96, l.phone_authority, '{}'::jsonb),
                  ('website_url', l.website_url, l.website_url, null::jsonb, 'observed', 0.92, l.website_authority, '{}'::jsonb),
                  ('primary_type_slug', l.primary_type_slug, l.primary_type_slug, null::jsonb, 'observed', coalesce(l.classification_confidence, 0.75)::double precision, l.type_authority, '{}'::jsonb),
                  ('status', l.source_status, l.source_status, null::jsonb, 'observed', 0.94, l.status_authority, '{}'::jsonb),
                  ('latitude', l.latitude::text, l.latitude::text, null::jsonb, 'observed', 0.98, l.location_authority, '{}'::jsonb),
                  ('longitude', l.longitude::text, l.longitude::text, null::jsonb, 'observed', 0.98, l.location_authority, '{}'::jsonb),
                  ('neighborhood', l.neighborhood, lower(l.neighborhood), null::jsonb, 'observed', 0.96, l.neighborhood_authority, '{}'::jsonb),
                  ('hours', l.hours::text, l.hours::text, l.hours, 'observed', 0.92, l.hours_authority, '{}'::jsonb),
                  ('price_level', l.price_level::text, l.price_level::text, to_jsonb(l.price_level), 'observed', 0.92, l.price_authority, '{}'::jsonb)
              ) as claim(
                field_name, value_text, normalized_value, value_json, claim_kind,
                evidence_confidence, authority, metadata
              )
              where claim.field_name is not null
                and claim.value_text is not null
                and claim.value_text <> ''
                and claim.authority > 0
            ), setting_claims as (
              select l.establishment_id, 'setting_slug'::text as field_name,
                     setting_slug as value_text, setting_slug as normalized_value,
                     null::jsonb as value_json, l.claim_source as source,
                     l.source_record_id || '#setting:' || setting_slug as source_record_id,
                     'observed'::text as claim_kind, 0.90::double precision as evidence_confidence,
                     l.identity_confidence, l.setting_authority as authority,
                     l.source_updated_at, '{}'::jsonb as metadata
              from linked l
              cross join lateral unnest(l.setting_slugs) as setting_slug
              where l.setting_authority > 0
            ), claims as (
              select * from scalar_claims
              union all
              select * from setting_claims
            )
            insert into ingest.establishment_field_evidence (
              establishment_id, field_name, value_text, normalized_value, value_json,
              source, source_record_id, claim_kind, evidence_confidence,
              identity_confidence, authority, source_updated_at, metadata
            )
            select establishment_id, field_name, value_text, normalized_value, value_json,
                   source, source_record_id, claim_kind, evidence_confidence,
                   identity_confidence, authority, source_updated_at, metadata
            from claims
            on conflict (establishment_id, field_name, source, source_record_id) do update set
              value_text = excluded.value_text,
              normalized_value = excluded.normalized_value,
              value_json = excluded.value_json,
              claim_kind = excluded.claim_kind,
              evidence_confidence = excluded.evidence_confidence,
              identity_confidence = excluded.identity_confidence,
              authority = excluded.authority,
              source_updated_at = excluded.source_updated_at,
              metadata = excluded.metadata,
              selected = false,
              resolution_score = null,
              updated_at = now()
            """,
            (json.dumps(policies, sort_keys=True),),
        )
        count = max(0, int(cursor.rowcount or 0))

        conn.execute(
            """
            insert into ingest.establishment_field_evidence (
                establishment_id, field_name, value_text, normalized_value, source,
                source_record_id, claim_kind, evidence_confidence, identity_confidence,
                authority, metadata
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
            select e.id::text, e.name, e.normalized_name, e.phone_e164,
                   e.neighborhood, e.hours, e.price_level,
                   e.phone_source, e.neighborhood_source, e.hours_source, e.price_source,
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
            select id::text as evidence_id, establishment_id::text, field_name,
                   value_text, normalized_value, value_json, source,
                   evidence_confidence::float, identity_confidence::float,
                   authority::float, source_updated_at
            from ingest.establishment_field_evidence
            where field_name in (
              'display_name', 'primary_type_slug', 'phone_e164', 'neighborhood',
              'hours', 'price_level', 'setting_slug'
            )
            """
        ).fetchall()
        setting_rows = conn.execute("select id, slug from public.settings").fetchall()

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

        conn.execute(
            """
            update ingest.establishment_field_evidence
            set selected = false, resolution_score = null
            where establishment_id in (
              select distinct establishment_id from ingest.establishment_sources
            )
            """
        )
        conn.execute(
            "delete from public.establishment_settings where source <> 'manual'"
        )

        metrics = {
            "resolved": 0,
            "renamed": 0,
            "low_name_confidence": 0,
            "name_conflicts": 0,
            "phones": 0,
            "neighborhoods": 0,
            "hours": 0,
            "prices": 0,
            "settings": 0,
        }
        updates: list[tuple[Any, ...]] = []
        selections: list[tuple[float, str]] = []
        resolved_settings: list[tuple[str, int, str, float]] = []

        for establishment in establishments:
            establishment_id = str(establishment["id"])
            by_field = evidence[establishment_id]
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
                    chosen_name = str(best["value_text"])
                    chosen_source = str(best["best_source"])
                    name_confidence = min(0.995, best_score)
                    selections.append((best_score, str(best["evidence_id"])))
                else:
                    name_confidence = min(0.79, best_score)
                    metrics["name_conflicts"] += 1

            if chosen_name != current_name:
                metrics["renamed"] += 1
            if name_confidence < 0.85:
                metrics["low_name_confidence"] += 1

            phone = self._select_attribute(by_field["phone_e164"], 0.68)
            neighborhood = self._select_attribute(by_field["neighborhood"], 0.68)
            hours = self._select_attribute(by_field["hours"], 0.58)
            price = self._select_attribute(by_field["price_level"], 0.75)
            for selected, metric in (
                (phone, "phones"),
                (neighborhood, "neighborhoods"),
                (hours, "hours"),
                (price, "prices"),
            ):
                if selected:
                    metrics[metric] += 1
                    selections.append((float(selected["score"]), str(selected["evidence_id"])))

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
                selections.append((float(candidate["score"]), str(candidate["evidence_id"])))
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
                        neighborhood, establishment, "neighborhood", "neighborhood_source"
                    ),
                    _selected_source(neighborhood, establishment, "neighborhood_source"),
                    _selected_score(neighborhood),
                    _selected_json(hours, establishment, "hours", "hours_source"),
                    _selected_source(hours, establishment, "hours_source"),
                    _selected_score(hours),
                    _selected_price(price, establishment),
                    _selected_source(price, establishment, "price_source"),
                    _selected_score(price),
                    establishment_id,
                )
            )
            metrics["resolved"] += 1

        conn.executemany(
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
                neighborhood = %s,
                neighborhood_source = %s,
                neighborhood_confidence = %s,
                hours = %s::jsonb,
                hours_source = %s,
                hours_confidence = %s,
                price_level = %s::smallint,
                price_source = %s,
                price_confidence = %s,
                updated_at = now()
            where id = %s::uuid
            """,
            updates,
        )
        if selections:
            conn.executemany(
                """
                update ingest.establishment_field_evidence
                set selected = true, resolution_score = %s, updated_at = now()
                where id = %s::uuid
                """,
                selections,
            )
        if resolved_settings:
            conn.executemany(
                """
                insert into public.establishment_settings (
                  establishment_id, setting_id, source, confidence, last_verified_at
                ) values (%s::uuid, %s, %s, %s, now())
                on conflict (establishment_id, setting_id) do nothing
                """,
                resolved_settings,
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
            independent_families = {
                _source_family(str(row["source"]))
                for row in matching
                if row["source"] != "canonical_seed"
            }
            agreement_bonus = min(0.08, 0.04 * max(0, len(independent_families) - 1))
            candidates.append(
                {
                    "normalized_value": normalized_value,
                    "value_text": best["value_text"],
                    "value_json": best.get("value_json"),
                    "evidence_id": best["evidence_id"],
                    "best_source": best["source"],
                    "source_count": len(independent_families),
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

    def _select_attribute(
        self, rows: list[dict[str, Any]], minimum_score: float
    ) -> dict[str, Any] | None:
        ranked = self._rank_evidence(rows)
        if not ranked or float(ranked[0]["score"]) < minimum_score:
            return None
        if len(ranked) > 1 and float(ranked[0]["score"]) - float(ranked[1]["score"]) < 0.06:
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


def _selected_json(
    selected: dict[str, Any] | None,
    current: dict[str, Any],
    value_key: str,
    source_key: str,
) -> str | None:
    if selected:
        value = selected.get("value_json")
        if value is None:
            value = selected.get("value_text")
        return json.dumps(value, sort_keys=True)
    if current.get(source_key) == "manual" and current.get(value_key) is not None:
        return json.dumps(current[value_key], sort_keys=True)
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
) -> str | None:
    if selected:
        return str(selected["best_source"])
    return "manual" if current.get(source_key) == "manual" else None


def _selected_score(selected: dict[str, Any] | None) -> float | None:
    return round(float(selected["score"]), 3) if selected else None


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
