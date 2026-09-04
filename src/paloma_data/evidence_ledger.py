from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any
from urllib.parse import urlsplit

from paloma_data.db import execute_many
from paloma_data.hours import normalize_hours
from paloma_data.hours_provenance import MANUAL_EVIDENCE_KINDS
from paloma_data.normalizers import normalize_name, normalize_phone, normalize_url


MANUAL_CANDIDATE_FIELDS = frozenset(
    {
        "phone_e164",
        "website_url",
        "neighborhood",
        "hours",
        "price_level",
        "setting_slug",
    }
)
MANUAL_ESTABLISHMENT_FIELDS = MANUAL_CANDIDATE_FIELDS | frozenset({"operating_status"})


@dataclass(frozen=True, slots=True)
class ObservationClaim:
    field_name: str
    value_text: str | None
    normalized_value: str | None
    value_json: Any
    evidence_confidence: float
    claim_kind: str = "observed"
    source_record_suffix: str = ""


def append_linked_source_observations(conn: Any) -> int:
    """Append legally admissible observations from currently linked source records."""
    policies = {
        (str(row["source"]), str(row["field_name"])): dict(row)
        for row in conn.execute(
            "select * from governance.current_source_field_policies"
        ).fetchall()
    }
    linked = conn.execute(
        """
        select es.establishment_id::text, es.source, es.source_record_id,
               coalesce(es.match_confidence, 0.75)::float as identity_confidence,
               sr.source_status, sr.source_updated_at, sr.payload_hash,
               sr.last_seen_run_id::text, sr.name, sr.normalized_name,
               sr.address, sr.normalized_address, sr.phone_e164, sr.website_url,
               sr.primary_type_slug, sr.latitude, sr.longitude, sr.neighborhood,
               sr.hours, sr.price_level, sr.setting_slugs,
               sr.classification_confidence::float, sr.origin_keys, sr.data_license,
               sr.field_provenance
        from ingest.establishment_sources es
        join ingest.source_records sr
          on sr.source = es.source and sr.source_record_id = es.source_record_id
        order by es.establishment_id, es.source, es.source_record_id
        """
    ).fetchall()

    rows = _linked_observation_rows(
        linked,
        policies,
        entity_column="establishment_id",
    )
    _append_observation_rows(conn, rows, entity_column="establishment_id")
    civic_rows = _append_civic_neighborhood_observations(
        conn,
        policies.get(("datasf_neighborhoods", "neighborhood")),
    )
    return len(rows) + civic_rows


def append_candidate_source_observations(conn: Any, candidate_id: str) -> int:
    """Append admissible linked facts while an entity is still private.

    Candidate observations use the same rights trigger and append-only ledger as public
    establishment observations.  The candidate UUID becomes the establishment UUID at
    materialization, so this evidence remains attached without mutation or copying.
    """
    policies = {
        (str(row["source"]), str(row["field_name"])): dict(row)
        for row in conn.execute(
            "select * from governance.current_source_field_policies"
        ).fetchall()
    }
    linked = conn.execute(
        """
        select csl.candidate_id::text, csl.source, csl.source_record_id,
               coalesce(csl.identity_confidence, 0.75)::float as identity_confidence,
               sr.source_status, sr.source_updated_at, sr.payload_hash,
               sr.last_seen_run_id::text, sr.name, sr.normalized_name,
               sr.address, sr.normalized_address, sr.phone_e164, sr.website_url,
               sr.primary_type_slug, sr.latitude, sr.longitude, sr.neighborhood,
               sr.hours, sr.price_level, sr.setting_slugs,
               sr.classification_confidence::float, sr.origin_keys, sr.data_license,
               sr.field_provenance
        from ingest.candidate_source_links csl
        join ingest.source_records sr
          on sr.source = csl.source and sr.source_record_id = csl.source_record_id
        where csl.candidate_id = %s::uuid
          and sr.retired_at is null
        order by csl.source, csl.source_record_id
        """,
        (candidate_id,),
    ).fetchall()
    rows = _linked_observation_rows(
        linked,
        policies,
        entity_column="candidate_id",
    )
    _append_observation_rows(conn, rows, entity_column="candidate_id")
    return len(rows)


def append_manual_candidate_observation(
    conn: Any,
    candidate_id: str,
    *,
    field_name: str,
    value: Any,
    reviewer: str,
    evidence_urls: tuple[str, ...],
    note: str | None = None,
    lease_days: int | None = None,
    observed_at: datetime | None = None,
    idempotency_key: str | None = None,
    evidence_kind: str = "factual_reference",
) -> dict[str, Any]:
    """Append one independently reviewed atomic fact for a private candidate."""
    field = field_name.strip()
    if field not in MANUAL_CANDIDATE_FIELDS:
        raise ValueError(f"Unsupported manual candidate field: {field}")
    candidate = conn.execute(
        "select 1 from ingest.catalog_candidates where id = %s::uuid",
        (candidate_id,),
    ).fetchone()
    if candidate is None:
        raise ValueError(f"Unknown catalog candidate: {candidate_id}")
    return _append_manual_observation(
        conn,
        entity_column="candidate_id",
        entity_id=candidate_id,
        field_name=field,
        value=value,
        reviewer=reviewer,
        evidence_urls=evidence_urls,
        note=note,
        lease_days=lease_days,
        observed_at=observed_at,
        idempotency_key=idempotency_key,
        evidence_kind=evidence_kind,
    )


def append_manual_establishment_observation(
    conn: Any,
    establishment_id: str,
    *,
    field_name: str,
    value: Any,
    reviewer: str,
    evidence_urls: tuple[str, ...],
    note: str | None = None,
    lease_days: int | None = None,
    observed_at: datetime | None = None,
    idempotency_key: str | None = None,
    evidence_kind: str = "factual_reference",
) -> dict[str, Any]:
    """Append a bounded staff observation for an already materialized establishment."""
    field = field_name.strip()
    if field not in MANUAL_ESTABLISHMENT_FIELDS:
        raise ValueError(f"Unsupported manual establishment field: {field}")
    establishment = conn.execute(
        """
        select name, city
        from public.establishments
        where id = %s::uuid and publication_state in ('published', 'suppressed')
        """,
        (establishment_id,),
    ).fetchone()
    if establishment is None:
        raise ValueError(f"Unknown materialized establishment: {establishment_id}")
    result = _append_manual_observation(
        conn,
        entity_column="establishment_id",
        entity_id=establishment_id,
        field_name=field,
        value=value,
        reviewer=reviewer,
        evidence_urls=evidence_urls,
        note=note,
        lease_days=lease_days,
        observed_at=observed_at,
        idempotency_key=idempotency_key,
        evidence_kind=evidence_kind,
    )
    return {
        **result,
        "establishment_id": establishment_id,
        "establishment_name": str(establishment["name"]),
        "city": str(establishment["city"]),
    }


def _append_manual_observation(
    conn: Any,
    *,
    entity_column: str,
    entity_id: str,
    field_name: str,
    value: Any,
    reviewer: str,
    evidence_urls: tuple[str, ...],
    note: str | None,
    lease_days: int | None,
    observed_at: datetime | None,
    idempotency_key: str | None,
    evidence_kind: str,
) -> dict[str, Any]:
    if entity_column not in {"establishment_id", "candidate_id"}:
        raise ValueError(f"Unsupported observation entity column: {entity_column}")
    field = field_name.strip()
    reviewer_name = reviewer.strip()
    evidence_kind = evidence_kind.strip().casefold()
    urls = tuple(dict.fromkeys(url.strip() for url in evidence_urls if url.strip()))
    if not reviewer_name or len(reviewer_name) > 200:
        raise ValueError("Manual field observations require an identified reviewer")
    if not urls or len(urls) > 10 or any(
        urlsplit(url).scheme != "https" or not urlsplit(url).netloc for url in urls
    ):
        raise ValueError("Manual field evidence requires 1-10 absolute HTTPS URLs")
    if note and len(note) > 1_000:
        raise ValueError("Manual field observation note is too long")
    if evidence_kind not in MANUAL_EVIDENCE_KINDS:
        allowed = ", ".join(sorted(MANUAL_EVIDENCE_KINDS))
        raise ValueError(f"Manual evidence kind must be one of: {allowed}")

    normalized = _normalize_manual_value(field, value)
    if field == "setting_slug":
        exists = conn.execute(
            "select 1 from public.settings where slug = %s",
            (normalized["normalized_value"],),
        ).fetchone()
        if exists is None:
            raise ValueError(f"Unknown Paloma setting slug: {normalized['normalized_value']}")

    policy = conn.execute(
        """
        select *
        from governance.current_source_field_policies
        where source = 'manual' and field_name = %s
        """,
        (field,),
    ).fetchone()
    if policy is None or not _policy_allows(policy):
        raise ValueError(f"No active manual rights policy permits {field}")

    timestamp = observed_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    recommended = policy.get("recommended_max_age")
    requested = timedelta(days=lease_days) if lease_days is not None else recommended
    if requested is None:
        requested = timedelta(days=365)
    if requested <= timedelta(0):
        raise ValueError("Manual field observation lease must be positive")
    if recommended is not None and requested > recommended:
        raise ValueError(
            f"Manual {field} lease exceeds the policy maximum of {recommended.days} days"
        )
    expires_at = timestamp + requested
    value_json = normalized["value_json"]
    value_hash = _digest({"text": normalized["value_text"], "json": value_json})
    stable_key = idempotency_key.strip() if idempotency_key else None
    if stable_key:
        existing = conn.execute(
            f"""
            select id::text, field_name, value_text, value_json, value_hash,
                   expires_at
            from catalog.field_observations
            where {entity_column} = %s::uuid
              and source = 'manual'
              and metadata->>'idempotency_key' = %s
              and (expires_at is null or expires_at > now())
            order by observed_at desc, id desc
            limit 1
            """,
            (entity_id, stable_key),
        ).fetchone()
        if existing is not None:
            if str(existing["field_name"]) != field or str(existing["value_hash"]) != value_hash:
                raise ValueError("Manual observation idempotency key was reused for a new fact")
            return {
                "observation_id": str(existing["id"]),
                "field_name": field,
                "value_text": existing.get("value_text"),
                "value_json": existing.get("value_json"),
                "expires_at": existing["expires_at"].isoformat(),
                "idempotent_replay": True,
            }
    source_record_id = f"{entity_id}:{field}:{reviewer_name}"
    fingerprint = _digest(
        {
            entity_column: entity_id,
            "field": field,
            "source": "manual",
            "source_record_id": source_record_id,
            "observed_at": timestamp.isoformat(),
            "value_hash": value_hash,
            "policy_id": int(policy["source_policy_id"]),
            "evidence_kind": evidence_kind,
        }
    )
    source_items = [
        {"kind": evidence_kind, "url": url} for url in urls
    ]
    row = conn.execute(
        f"""
        insert into catalog.field_observations (
          {entity_column}, field_name, value_text, normalized_value, value_json,
          value_hash, source, source_record_id, source_property, claim_kind,
          evidence_confidence, identity_confidence, authority,
          upstream_origin_keys, license_ids, source_items, source_policy_id,
          source_updated_at, observed_at, valid_from, expires_at,
          observation_fingerprint, metadata
        ) values (
          %s::uuid, %s, %s, %s, %s::jsonb,
          %s, 'manual', %s, %s, 'manual',
          0.98, 1.0, %s,
          %s, array['Paloma-manual-verification'], %s::jsonb, %s,
          %s, %s, %s, %s,
          %s, %s::jsonb
        )
        returning id::text
        """,
        (
            entity_id,
            field,
            normalized["value_text"],
            normalized["normalized_value"],
            json.dumps(value_json, sort_keys=True) if value_json is not None else None,
            value_hash,
            source_record_id,
            field,
            float(policy["authority"]),
            [f"manual:{reviewer_name}"],
            json.dumps(source_items, sort_keys=True),
            int(policy["source_policy_id"]),
            timestamp,
            timestamp,
            timestamp,
            expires_at,
            fingerprint,
            json.dumps(
                {
                    "evidence_urls": list(urls),
                    "evidence_kind": evidence_kind,
                    "idempotency_key": stable_key,
                    "note": note.strip() if note and note.strip() else None,
                    "policy_version": policy["policy_version"],
                    "reviewer": reviewer_name,
                },
                sort_keys=True,
            ),
        ),
    ).fetchone()
    return {
        "observation_id": str(row["id"]),
        "field_name": field,
        "value_text": normalized["value_text"],
        "value_json": value_json,
        "expires_at": expires_at.isoformat(),
        "idempotent_replay": False,
    }


def _linked_observation_rows(
    linked: Iterable[Mapping[str, Any]],
    policies: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    entity_column: str,
) -> list[tuple[Any, ...]]:
    if entity_column not in {"establishment_id", "candidate_id"}:
        raise ValueError(f"Unsupported observation entity column: {entity_column}")
    rows: list[tuple[Any, ...]] = []
    for record in linked:
        source = str(record["source"])
        for claim in _record_claims(record):
            policy = policies.get((source, claim.field_name))
            if not policy or not _policy_allows(policy):
                continue
            provenance = _field_provenance(record, claim.field_name)
            licenses = tuple(sorted(set(provenance["license_ids"])))
            origins = tuple(sorted(set(provenance["origin_keys"])))
            allowed = set(policy.get("allowed_license_ids") or ())
            if not licenses or not origins or (allowed and not set(licenses) <= allowed):
                continue
            value_json = (
                json.dumps(claim.value_json, sort_keys=True, separators=(",", ":"))
                if claim.value_json is not None
                else None
            )
            value_hash = _digest(
                {"text": claim.value_text, "json": claim.value_json}
            )
            source_record_id = str(record["source_record_id"]) + claim.source_record_suffix
            fingerprint = _digest(
                {
                    entity_column: str(record[entity_column]),
                    "field": claim.field_name,
                    "source": source,
                    "source_record_id": source_record_id,
                    "payload_hash": record.get("payload_hash"),
                    "value_hash": value_hash,
                    "normalized_value": claim.normalized_value,
                    "policy_id": int(policy["source_policy_id"]),
                }
            )
            rows.append(
                (
                    str(record[entity_column]),
                    claim.field_name,
                    claim.value_text,
                    claim.normalized_value,
                    value_json,
                    value_hash,
                    source,
                    source_record_id,
                    claim.field_name,
                    record.get("last_seen_run_id"),
                    record.get("payload_hash"),
                    claim.claim_kind,
                    claim.evidence_confidence,
                    float(record.get("identity_confidence") or 0.75),
                    float(policy["authority"]),
                    list(origins),
                    list(licenses),
                    json.dumps(provenance["source_items"], sort_keys=True),
                    int(policy["source_policy_id"]),
                    record.get("source_updated_at"),
                    policy.get("recommended_max_age"),
                    fingerprint,
                    json.dumps(
                        {"policy_version": policy["policy_version"]}, sort_keys=True
                    ),
                )
            )
    return rows


def _append_observation_rows(
    conn: Any,
    rows: list[tuple[Any, ...]],
    *,
    entity_column: str,
) -> None:
    if not rows:
        return
    if entity_column not in {"establishment_id", "candidate_id"}:
        raise ValueError(f"Unsupported observation entity column: {entity_column}")
    execute_many(
        conn,
        f"""
        insert into catalog.field_observations (
          {entity_column}, field_name, value_text, normalized_value, value_json,
          value_hash, source, source_record_id, source_property, source_run_id,
          source_record_payload_hash, claim_kind, evidence_confidence,
          identity_confidence, authority, upstream_origin_keys, license_ids,
          source_items, source_policy_id, source_updated_at, expires_at,
          observation_fingerprint, metadata
        ) values (
          %s::uuid, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::uuid,
          %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
          case when %s::interval is null then null else now() + %s::interval end,
          %s, %s::jsonb
        )
        on conflict (observation_fingerprint) do nothing
        """,
        [row[:20] + (row[20], row[20]) + row[21:] for row in rows],
    )


def _append_civic_neighborhood_observations(
    conn: Any, policy: Mapping[str, Any] | None
) -> int:
    """Project reviewed SF Find polygons into durable per-establishment observations."""
    if not policy or not _policy_allows(policy):
        return 0
    matches = conn.execute(
        """
        with current_establishments as (
          select e.id, e.location, e.identity_confidence,
                 e.neighborhood as resolved_neighborhood,
                 e.neighborhood_source as resolved_source
          from public.establishments e
          where e.publication_state in ('published', 'suppressed')
            and e.verification_expires_at > now()
        ), direct_ranked as (
          select c.id, c.location, c.identity_confidence,
                 nb.source_record_id, nb.name, nb.normalized_name,
                 nb.authority::float, nb.source_updated_at, nb.payload_hash,
                 nb.last_seen_run_id::text as source_run_id,
                 ST_Distance(
                   ST_Boundary(nb.boundary)::geography,
                   c.location
                 )::float as boundary_distance_m,
                 row_number() over (
                   partition by c.id
                   order by nb.authority desc, ST_Area(nb.boundary::geography),
                            nb.source_record_id
                 ) as rank
          from current_establishments c
          join ingest.neighborhood_boundaries nb
            on nb.source = 'datasf_neighborhoods'
           and nb.retired_at is null
           and ST_Covers(nb.boundary, c.location::geometry)
           and ST_Distance(
                 ST_Boundary(nb.boundary)::geography,
                 c.location
               ) >= 10
        ), direct as (
          select id, location, identity_confidence, source_record_id, name,
                 normalized_name, authority, source_updated_at, payload_hash,
                 source_run_id, 'point_in_polygon'::text as match_method,
                 boundary_distance_m,
                 array['datasf:gfpk-269f']::text[] as origin_keys
          from direct_ranked where rank = 1
        ), consensus as (
          select c.id, c.location, c.identity_confidence,
                 nb.source_record_id, nb.name, nb.normalized_name,
                 least(nb.authority, 0.94)::float as authority,
                 nb.source_updated_at, nb.payload_hash,
                 nb.last_seen_run_id::text as source_run_id,
                 'linked_coordinate_consensus'::text as match_method,
                 null::float as boundary_distance_m,
                 array_prepend(
                   'datasf:gfpk-269f',
                   coalesce(origins.origin_keys, '{}'::text[])
                 ) as origin_keys
          from current_establishments c
          join ingest.neighborhood_boundaries nb
            on nb.source = 'datasf_neighborhoods'
           and nb.retired_at is null
           and nb.name = c.resolved_neighborhood
          left join lateral (
            select array_agg(distinct origin order by origin) as origin_keys
            from ingest.establishment_sources link
            join ingest.source_records source_record
              on source_record.source = link.source
             and source_record.source_record_id = link.source_record_id
            cross join lateral unnest(
              coalesce(nullif(source_record.origin_keys, '{}'), array[link.source])
            ) origin
            where link.establishment_id = c.id
          ) origins on true
          where c.resolved_source = 'datasf_neighborhoods:linked_coordinate_consensus'
            and not exists (select 1 from direct where direct.id = c.id)
        )
        select id::text as establishment_id,
               ST_Y(location::geometry)::float as latitude,
               ST_X(location::geometry)::float as longitude,
               identity_confidence::float, source_record_id, name, normalized_name,
               authority, source_updated_at, payload_hash, source_run_id,
               match_method, boundary_distance_m, origin_keys
        from direct
        union all
        select id::text, ST_Y(location::geometry)::float,
               ST_X(location::geometry)::float, identity_confidence::float,
               source_record_id, name, normalized_name, authority,
               source_updated_at, payload_hash, source_run_id,
               match_method, boundary_distance_m, origin_keys
        from consensus
        order by establishment_id
        """
    ).fetchall()
    if not matches:
        return 0

    rows: list[tuple[Any, ...]] = []
    for match in matches:
        source_item = {
            "dataset_id": "gfpk-269f",
            "license_id": "Public-Domain-US-Government",
            "record_id": str(match["source_record_id"]),
            "property": "name",
        }
        metadata = {
            "boundary_distance_m": match.get("boundary_distance_m"),
            "derivation": str(match["match_method"]),
            "latitude": float(match["latitude"]),
            "longitude": float(match["longitude"]),
            "policy_version": policy["policy_version"],
        }
        fingerprint = _digest(
            {
                "establishment_id": str(match["establishment_id"]),
                "field": "neighborhood",
                "source": "datasf_neighborhoods",
                "source_record_id": str(match["source_record_id"]),
                "payload_hash": str(match["payload_hash"]),
                "value": str(match["name"]),
                "latitude": round(float(match["latitude"]), 7),
                "longitude": round(float(match["longitude"]), 7),
                "derivation": str(match["match_method"]),
                "policy_id": int(policy["source_policy_id"]),
            }
        )
        rows.append(
            (
                str(match["establishment_id"]),
                str(match["name"]),
                str(match["normalized_name"]),
                _digest({"text": str(match["name"]), "json": None}),
                str(match["source_record_id"]),
                match.get("source_run_id"),
                str(match["payload_hash"]),
                0.98 if match["match_method"] == "point_in_polygon" else 0.94,
                float(match.get("identity_confidence") or 0.90),
                min(float(match["authority"]), float(policy["authority"])),
                list(match.get("origin_keys") or ("datasf:gfpk-269f",)),
                json.dumps([source_item], sort_keys=True),
                int(policy["source_policy_id"]),
                match.get("source_updated_at"),
                policy.get("recommended_max_age"),
                fingerprint,
                json.dumps(metadata, sort_keys=True),
            )
        )
    execute_many(
        conn,
        """
        insert into catalog.field_observations (
          establishment_id, field_name, value_text, normalized_value, value_json,
          value_hash, source, source_record_id, source_property, source_run_id,
          source_record_payload_hash, claim_kind, evidence_confidence,
          identity_confidence, authority, upstream_origin_keys, license_ids,
          source_items, source_policy_id, source_updated_at, expires_at,
          observation_fingerprint, metadata
        ) values (
          %s::uuid, 'neighborhood', %s, %s, null,
          %s, 'datasf_neighborhoods', %s, 'name', %s::uuid,
          %s, 'derived', %s, %s, %s, %s,
          array['Public-Domain-US-Government'], %s::jsonb, %s, %s,
          case when %s::interval is null then null else now() + %s::interval end,
          %s, %s::jsonb
        )
        on conflict (observation_fingerprint) do nothing
        """,
        [row[:14] + (row[14], row[14]) + row[15:] for row in rows],
    )
    return len(rows)


def _record_claims(record: Mapping[str, Any]) -> Iterable[ObservationClaim]:
    source = str(record["source"])
    name_field = "legal_name" if source in {"ca_abc", "datasf"} else "display_name"
    yield from _scalar_claim(
        name_field,
        record.get("name"),
        record.get("normalized_name"),
        None,
        0.98 if name_field == "legal_name" else 0.92,
    )
    for field, value, normalized, value_json, confidence in (
        ("address", record.get("address"), record.get("normalized_address"), None, 0.97),
        ("phone_e164", record.get("phone_e164"), record.get("phone_e164"), None, 0.96),
        (
            "website_url",
            record.get("website_url"),
            _website_identity(record.get("website_url")),
            None,
            0.92,
        ),
        (
            "primary_type_slug",
            record.get("primary_type_slug"),
            record.get("primary_type_slug"),
            None,
            float(record.get("classification_confidence") or 0.75),
        ),
        ("latitude", _text(record.get("latitude")), _coordinate(record.get("latitude")), None, 0.98),
        ("longitude", _text(record.get("longitude")), _coordinate(record.get("longitude")), None, 0.98),
        ("neighborhood", record.get("neighborhood"), _lower(record.get("neighborhood")), None, 0.96),
        ("hours", _json_text(record.get("hours")), _json_text(record.get("hours")), record.get("hours"), 0.92),
        ("price_level", _text(record.get("price_level")), _text(record.get("price_level")), record.get("price_level"), 0.92),
    ):
        yield from _scalar_claim(field, value, normalized, value_json, confidence)

    status_field = {
        "ca_abc": "license_status",
        "datasf": "registration_status",
    }.get(source, "operating_status")
    yield from _scalar_claim(
        status_field,
        record.get("source_status"),
        record.get("source_status"),
        None,
        0.94,
    )
    for setting in record.get("setting_slugs") or ():
        value = str(setting).strip()
        if value:
            yield ObservationClaim(
                "setting_slug", value, value, None, 0.90, source_record_suffix=f"#setting:{value}"
            )


def _scalar_claim(
    field: str,
    value: Any,
    normalized: Any,
    value_json: Any,
    confidence: float,
) -> Iterable[ObservationClaim]:
    if value is None or str(value).strip() == "":
        return ()
    return (
        ObservationClaim(
            field,
            str(value),
            str(normalized) if normalized is not None else None,
            value_json,
            confidence,
        ),
    )


def _field_provenance(record: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    all_fields = record.get("field_provenance") or {}
    field = all_fields.get(field_name, {}) if isinstance(all_fields, dict) else {}
    return {
        "origin_keys": field.get("origin_keys") or list(record.get("origin_keys") or ()),
        "license_ids": field.get("license_ids") or [record.get("data_license")],
        "source_items": field.get("source_items") or [],
    }


def _policy_allows(policy: Mapping[str, Any]) -> bool:
    return bool(
        policy.get("normalized_persistence_allowed")
        and policy.get("source_derivation_allowed")
        and policy.get("durable_storage_allowed")
        and policy.get("canonical_derivation_allowed")
    )


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode()).hexdigest()


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _lower(value: Any) -> str | None:
    return str(value).lower() if value is not None else None


def _coordinate(value: Any) -> str | None:
    return f"{float(value):.4f}" if value is not None else None


def _website_identity(value: Any) -> str | None:
    if not value:
        return None
    parsed = urlsplit(str(value))
    host = (parsed.hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]
    # A location page and the root page are compatible website observations when the
    # independently observed registrable host is identical.  Preserve the best full URL in
    # value_text, but group evidence by host so harmless path differences are not conflicts.
    return host or None


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _normalize_manual_value(field_name: str, value: Any) -> dict[str, Any]:
    if field_name == "hours":
        hours = normalize_hours(value)
        if hours is None:
            raise ValueError("Manual hours observation is empty")
        encoded = json.dumps(hours, sort_keys=True, separators=(",", ":"))
        return {
            "value_text": encoded,
            "normalized_value": encoded,
            "value_json": hours,
        }
    if field_name == "phone_e164":
        text = normalize_phone(str(value), "US")
    elif field_name == "website_url":
        text = normalize_url(str(value))
    elif field_name == "price_level":
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("price_level must be 1 through 4") from exc
        if parsed not in range(1, 5):
            raise ValueError("price_level must be 1 through 4")
        text = str(parsed)
    elif field_name == "setting_slug":
        text = str(value).strip().casefold().replace(" ", "_")
    elif field_name == "operating_status":
        text = str(value).strip().casefold()
        if text not in {"open", "temporarily_closed", "closed"}:
            raise ValueError(
                "operating_status must be open, temporarily_closed, or closed"
            )
    else:
        text = str(value).strip()
    if not text:
        raise ValueError(f"Manual {field_name} observation is empty")
    normalized_value = (
        _website_identity(text)
        if field_name == "website_url"
        else normalize_name(text)
        if field_name == "neighborhood"
        else text
    )
    return {
        "value_text": text,
        "normalized_value": normalized_value,
        "value_json": int(text) if field_name == "price_level" else None,
    }
