from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any
from urllib.parse import urlsplit

from paloma_data.db import execute_many


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
                    "establishment_id": str(record["establishment_id"]),
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
                    str(record["establishment_id"]),
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
    if not rows:
        return 0
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
          %s::uuid, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::uuid,
          %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
          case when %s::interval is null then null else now() + %s::interval end,
          %s, %s::jsonb
        )
        on conflict (observation_fingerprint) do nothing
        """,
        [row[:20] + (row[20], row[20]) + row[21:] for row in rows],
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
    path = parsed.path.rstrip("/")
    return f"{host}{path}" if host else None


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
