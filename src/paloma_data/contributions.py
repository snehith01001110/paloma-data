from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from paloma_data.db import Database
from paloma_data.hours import normalize_hours
from paloma_data.normalizers import normalize_name, normalize_phone, normalize_url


class ContributionReviewer:
    def __init__(self, db: Database) -> None:
        self.db = db

    def review_merchant_claim(
        self, claim_id: str, *, decision: str, reviewer: str, reason: str
    ) -> dict[str, str]:
        if decision not in {"verified", "rejected"}:
            raise ValueError("decision must be verified or rejected")
        with self.db.connection() as conn:
            row = conn.execute(
                """
                select id::text from public.merchant_claim_requests
                where id = %s::uuid and state = 'pending'
                for update
                """,
                (claim_id,),
            ).fetchone()
            if row is None:
                raise ValueError("merchant claim is missing or no longer pending")
            conn.execute(
                """
                update public.merchant_claim_requests
                set state = %s, reviewed_at = now(), reviewed_by = %s
                where id = %s::uuid
                """,
                (decision, reviewer, claim_id),
            )
            conn.execute(
                """
                insert into catalog.merchant_claim_reviews (
                  merchant_claim_id, decision, reviewer, reason
                ) values (%s::uuid, %s, %s, %s)
                """,
                (claim_id, decision, reviewer, reason),
            )
            conn.commit()
        return {"claim_id": claim_id, "decision": decision}

    def review_contribution(
        self, contribution_id: str, *, decision: str, reviewer: str, reason: str
    ) -> dict[str, str | None]:
        if decision not in {"accepted", "rejected"}:
            raise ValueError("decision must be accepted or rejected")
        with self.db.connection() as conn:
            contribution = conn.execute(
                """
                select id::text, establishment_id::text, contributor_id::text,
                       contribution_kind, field_name, proposed_value, observed_at,
                       terms_version
                from public.establishment_contributions
                where id = %s::uuid and state = 'pending'
                for update
                """,
                (contribution_id,),
            ).fetchone()
            if contribution is None:
                raise ValueError("contribution is missing or no longer pending")
            observation_id = None
            if decision == "accepted":
                normalized = _normalize_value(
                    str(contribution["field_name"]), contribution["proposed_value"]
                )
                policy = conn.execute(
                    """
                    select source_policy_id, authority
                    from governance.current_source_field_policies
                    where source = %s and field_name = %s
                      and durable_storage_allowed and canonical_derivation_allowed
                    """,
                    (contribution["contribution_kind"], contribution["field_name"]),
                ).fetchone()
                if policy is None:
                    raise ValueError("no active rights policy permits this contribution")
                source = str(contribution["contribution_kind"])
                license_id = f"Paloma-contributor-terms-{contribution['terms_version']}"
                value_hash = _hash(
                    {"text": normalized["value_text"], "json": normalized["value_json"]}
                )
                fingerprint = _hash(
                    {
                        "contribution_id": contribution_id,
                        "field": contribution["field_name"],
                        "value_hash": value_hash,
                        "policy_id": int(policy["source_policy_id"]),
                    }
                )
                inserted = conn.execute(
                    """
                    insert into catalog.field_observations (
                      establishment_id, field_name, value_text, normalized_value,
                      value_json, value_hash, source, source_record_id, source_property,
                      claim_kind, evidence_confidence, identity_confidence, authority,
                      upstream_origin_keys, license_ids, source_items, source_policy_id,
                      source_updated_at, observed_at, expires_at,
                      observation_fingerprint, metadata
                    ) values (
                      %s::uuid, %s, %s, %s, %s::jsonb, %s, %s, %s, %s,
                      %s, %s, 1.0, %s, %s, %s, '[]'::jsonb, %s, %s, %s,
                      %s + case when %s in ('hours','operating_status') then interval '90 days'
                                else interval '180 days' end,
                      %s, %s::jsonb
                    )
                    returning id::text
                    """,
                    (
                        contribution["establishment_id"],
                        contribution["field_name"],
                        normalized["value_text"],
                        normalized["normalized_value"],
                        json.dumps(normalized["value_json"], sort_keys=True)
                        if normalized["value_json"] is not None
                        else None,
                        value_hash,
                        source,
                        contribution_id,
                        contribution["field_name"],
                        "owner_attested" if source == "merchant" else "firsthand",
                        1.0 if source == "merchant" else 0.80,
                        float(policy["authority"]),
                        [f"{source}:{contribution['contributor_id']}"],
                        [license_id],
                        int(policy["source_policy_id"]),
                        contribution["observed_at"],
                        contribution["observed_at"],
                        contribution["observed_at"],
                        contribution["field_name"],
                        fingerprint,
                        json.dumps({"reviewer": reviewer, "review_reason": reason}, sort_keys=True),
                    ),
                ).fetchone()
                observation_id = str(inserted["id"])
            conn.execute(
                """
                update public.establishment_contributions
                set state = %s, reviewed_at = now(), reviewed_by = %s, review_reason = %s
                where id = %s::uuid
                """,
                (decision, reviewer, reason, contribution_id),
            )
            conn.execute(
                """
                insert into catalog.contribution_reviews (
                  contribution_id, decision, reviewer, reason, observation_id
                ) values (%s::uuid, %s, %s, %s, %s::uuid)
                """,
                (contribution_id, decision, reviewer, reason, observation_id),
            )
            conn.commit()
        return {
            "contribution_id": contribution_id,
            "decision": decision,
            "observation_id": observation_id,
        }


def _normalize_value(field_name: str, proposed: Any) -> dict[str, Any]:
    value = proposed
    if isinstance(proposed, dict) and set(proposed) == {"value"}:
        value = proposed["value"]
    if field_name == "hours":
        hours = normalize_hours(value)
        if hours is None:
            raise ValueError("hours contribution is empty")
        return {
            "value_text": json.dumps(hours, sort_keys=True, separators=(",", ":")),
            "normalized_value": json.dumps(hours, sort_keys=True, separators=(",", ":")),
            "value_json": hours,
        }
    if field_name == "phone_e164":
        text = normalize_phone(str(value), "US")
    elif field_name == "website_url":
        text = normalize_url(str(value))
    else:
        text = str(value).strip()
    if not text:
        raise ValueError(f"invalid {field_name} contribution")
    if field_name == "price_level" and text not in {"1", "2", "3", "4"}:
        raise ValueError("price_level must be 1 through 4")
    normalized = normalize_name(text) if field_name in {"display_name", "address"} else text
    value_json = int(text) if field_name == "price_level" else None
    return {"value_text": text, "normalized_value": normalized, "value_json": value_json}


def _hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode()).hexdigest()
