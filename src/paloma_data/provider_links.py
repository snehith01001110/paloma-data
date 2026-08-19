from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import json
import math
import uuid
from typing import Any

import psycopg

from paloma_data.adapters.yelp import (
    YELP_MATCH_METHOD,
    YelpAPIError,
    YelpMatchInput,
    YelpPlacesAPI,
)
from paloma_data.db import Database


@dataclass(frozen=True, slots=True)
class ProviderMatchLease:
    token: str
    identity_fingerprint: str


class ProviderLinkRepository:
    """Private persistence for durable IDs and Paloma-owned match decisions only."""

    def candidates(
        self,
        conn: psycopg.Connection,
        *,
        city: str | None,
        scan_limit: int,
    ) -> list[YelpMatchInput]:
        rows = conn.execute(
            """
            select
              establishment.id::text as establishment_id,
              establishment.name,
              establishment.address,
              establishment.city,
              establishment.region,
              establishment.postal_code,
              establishment.country_code,
              ST_Y(establishment.location::geometry)::float as latitude,
              ST_X(establishment.location::geometry)::float as longitude,
              establishment.phone_e164
            from public.establishments establishment
            join ingest.catalog_candidates candidate
              on candidate.id = establishment.catalog_candidate_id
            left join runtime.runtime_provider_links yelp_link
              on yelp_link.establishment_id = establishment.id
             and yelp_link.provider = 'yelp'
             and yelp_link.retired_at is null
            left join runtime.provider_match_state match_state
              on match_state.establishment_id = establishment.id
             and match_state.provider = 'yelp'
            where establishment.publication_state = 'published'
              and establishment.status = 'open'
              and establishment.access_mode = 'walk_in'
              and establishment.verification_tier in ('open_evidence', 'provider', 'manual')
              and establishment.verification_expires_at > now()
              and candidate.candidate_state in ('verified', 'published')
              and candidate.identity_confidence >= 0.96
              and (%s::text is null or lower(establishment.city) = lower(%s::text))
            order by
              (yelp_link.id is null) desc,
              (match_state.establishment_id is null) desc,
              match_state.retry_after asc nulls first,
              establishment.updated_at,
              establishment.id
            limit %s
            """,
            (city, city, scan_limit),
        ).fetchall()
        return [
            YelpMatchInput(
                establishment_id=str(row["establishment_id"]),
                name=str(row["name"]),
                address=str(row["address"]),
                city=str(row["city"]),
                region=_optional_text(row.get("region")),
                postal_code=_optional_text(row.get("postal_code")),
                country_code=str(row["country_code"]),
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                phone_e164=_optional_text(row.get("phone_e164")),
            )
            for row in rows
        ]

    def active_yelp_links(
        self,
        conn: psycopg.Connection,
        *,
        city: str | None,
        limit: int,
    ) -> list[tuple[YelpMatchInput, str]]:
        rows = conn.execute(
            """
            select
              establishment.id::text as establishment_id,
              establishment.name, establishment.address, establishment.city,
              establishment.region, establishment.postal_code,
              establishment.country_code,
              ST_Y(establishment.location::geometry)::float as latitude,
              ST_X(establishment.location::geometry)::float as longitude,
              establishment.phone_e164,
              yelp_link.provider_place_id
            from public.establishments establishment
            join runtime.runtime_provider_links yelp_link
              on yelp_link.establishment_id = establishment.id
             and yelp_link.provider = 'yelp'
             and yelp_link.retired_at is null
            where establishment.publication_state = 'published'
              and establishment.status = 'open'
              and (%s::text is null or lower(establishment.city) = lower(%s::text))
            order by establishment.name, establishment.id
            limit %s
            """,
            (city, city, limit),
        ).fetchall()
        return [(_yelp_match_input(row), str(row["provider_place_id"])) for row in rows]

    def rejected_yelp_candidates(
        self,
        conn: psycopg.Connection,
        *,
        city: str | None,
        limit: int,
    ) -> list[tuple[YelpMatchInput, str]]:
        rows = conn.execute(
            """
            select
              establishment.id::text as establishment_id,
              establishment.name, establishment.address, establishment.city,
              establishment.region, establishment.postal_code,
              establishment.country_code,
              ST_Y(establishment.location::geometry)::float as latitude,
              ST_X(establishment.location::geometry)::float as longitude,
              establishment.phone_e164,
              match_state.decision_reason
            from public.establishments establishment
            join runtime.provider_match_state match_state
              on match_state.establishment_id = establishment.id
             and match_state.provider = 'yelp'
             and match_state.outcome = 'rejected'
            left join runtime.runtime_provider_links yelp_link
              on yelp_link.establishment_id = establishment.id
             and yelp_link.provider = 'yelp'
             and yelp_link.retired_at is null
            where establishment.publication_state = 'published'
              and establishment.status = 'open'
              and yelp_link.id is null
              and (%s::text is null or lower(establishment.city) = lower(%s::text))
            order by establishment.name, establishment.id
            limit %s
            """,
            (city, city, limit),
        ).fetchall()
        return [
            (_yelp_match_input(row), str(row.get("decision_reason") or "unknown"))
            for row in rows
        ]

    def claim(
        self,
        conn: psycopg.Connection,
        place: YelpMatchInput,
        identity_fingerprint: str,
    ) -> ProviderMatchLease | None:
        token = str(uuid.uuid4())
        row = conn.execute(
            """
            insert into runtime.provider_match_state as match_state (
              establishment_id, provider, identity_fingerprint, outcome,
              attempted_at, retry_after, lease_token, lease_expires_at,
              last_error_code, decision_reason, updated_at
            ) values (
              %s::uuid, 'yelp', %s, 'pending',
              now(), now(), %s::uuid, now() + interval '25 seconds',
              null, null, now()
            )
            on conflict (establishment_id, provider) do update set
              identity_fingerprint = excluded.identity_fingerprint,
              outcome = 'pending',
              attempted_at = now(),
              retry_after = now(),
              lease_token = excluded.lease_token,
              lease_expires_at = excluded.lease_expires_at,
              last_error_code = null,
              decision_reason = null,
              updated_at = now()
            where match_state.identity_fingerprint <> excluded.identity_fingerprint
               or (
                 match_state.retry_after <= now()
                 and (
                   match_state.lease_expires_at is null
                   or match_state.lease_expires_at <= now()
                 )
               )
            returning lease_token::text, identity_fingerprint
            """,
            (place.establishment_id, identity_fingerprint, token),
        ).fetchone()
        if not row or str(row["lease_token"]) != token:
            return None
        return ProviderMatchLease(token, str(row["identity_fingerprint"]))

    def store_match(
        self,
        conn: psycopg.Connection,
        place: YelpMatchInput,
        lease: ProviderMatchLease,
        *,
        provider_place_id: str,
        confidence: float,
    ) -> bool:
        row = conn.execute(
            """
            with owned_lease as (
              select establishment_id, provider
              from runtime.provider_match_state
              where establishment_id = %s::uuid
                and provider = 'yelp'
                and identity_fingerprint = %s
                and lease_token = %s::uuid
                and lease_expires_at > now()
            ), upserted as (
              insert into runtime.runtime_provider_links as runtime_link (
                establishment_id, provider, provider_place_id, match_method,
                match_confidence, matched_at, last_validated_at, retired_at, updated_at
              )
              select
                owned_lease.establishment_id, owned_lease.provider, %s,
                %s, %s, now(), now(), null, now()
              from owned_lease
              on conflict (establishment_id, provider) do update set
                provider_place_id = excluded.provider_place_id,
                match_method = excluded.match_method,
                match_confidence = excluded.match_confidence,
                matched_at = excluded.matched_at,
                last_validated_at = excluded.last_validated_at,
                retired_at = null,
                updated_at = now()
              returning establishment_id
            ), finished as (
              update runtime.provider_match_state
              set outcome = 'matched',
                  retry_after = now() + interval '90 days',
                  lease_token = null,
                  lease_expires_at = null,
                  last_error_code = null,
                  decision_reason = 'matched',
                  updated_at = now()
              where establishment_id = %s::uuid
                and provider = 'yelp'
                and identity_fingerprint = %s
                and lease_token = %s::uuid
                and exists (select 1 from upserted)
              returning establishment_id
            )
            select establishment_id::text from finished
            """,
            (
                place.establishment_id,
                lease.identity_fingerprint,
                lease.token,
                provider_place_id,
                YELP_MATCH_METHOD,
                confidence,
                place.establishment_id,
                lease.identity_fingerprint,
                lease.token,
            ),
        ).fetchone()
        return row is not None

    def complete(
        self,
        conn: psycopg.Connection,
        place: YelpMatchInput,
        lease: ProviderMatchLease,
        *,
        outcome: str,
        decision_reason: str,
        retry_after: timedelta,
        error_code: str | None = None,
    ) -> bool:
        row = conn.execute(
            """
            update runtime.provider_match_state
            set outcome = %s,
                retry_after = now() + %s::interval,
                lease_token = null,
                lease_expires_at = null,
                last_error_code = %s,
                decision_reason = %s,
                updated_at = now()
            where establishment_id = %s::uuid
              and provider = 'yelp'
              and identity_fingerprint = %s
              and lease_token = %s::uuid
            returning establishment_id::text
            """,
            (
                outcome,
                _postgres_interval(retry_after),
                error_code if outcome == "error" else None,
                decision_reason,
                place.establishment_id,
                lease.identity_fingerprint,
                lease.token,
            ),
        ).fetchone()
        return row is not None


class ProviderLinkSync:
    def __init__(
        self,
        db: Database,
        *,
        repository: ProviderLinkRepository | None = None,
    ) -> None:
        self.db = db
        self.repository = repository or ProviderLinkRepository()

    def run(
        self,
        api: YelpPlacesAPI,
        *,
        city: str | None,
        limit: int,
    ) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be positive")
        counts: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        scan_limit = min(max(limit * 10, 100), 5_000)

        with self.db.connection() as conn:
            places = self.repository.candidates(conn, city=city, scan_limit=scan_limit)
            conn.commit()
            for place in places:
                if counts["api_calls"] >= limit:
                    break
                counts["scanned"] += 1
                fingerprint = provider_match_identity_fingerprint(place)
                lease = self.repository.claim(conn, place, fingerprint)
                conn.commit()
                if not lease:
                    counts["not_due"] += 1
                    continue

                counts["api_calls"] += 1
                try:
                    selection = api.match(place)
                    if selection.outcome == "matched":
                        assert selection.provider_place_id is not None
                        assert selection.confidence is not None
                        stored = self.repository.store_match(
                            conn,
                            place,
                            lease,
                            provider_place_id=selection.provider_place_id,
                            confidence=selection.confidence,
                        )
                        conn.commit()
                        if stored:
                            counts["matched"] += 1
                            reasons["matched"] += 1
                        else:
                            counts["lost_lease"] += 1
                            reasons["lost_lease"] += 1
                        continue

                    outcome = "not_found" if selection.outcome == "not_found" else "rejected"
                    retry_after = (
                        timedelta(days=30)
                        if selection.outcome == "not_found"
                        else timedelta(days=90)
                    )
                    self.repository.complete(
                        conn,
                        place,
                        lease,
                        outcome=outcome,
                        decision_reason=selection.reason,
                        retry_after=retry_after,
                    )
                    conn.commit()
                    counts[outcome] += 1
                    reasons[selection.reason] += 1
                except YelpAPIError as error:
                    conn.rollback()
                    retry_after = _retry_after_for_error(error.code)
                    self.repository.complete(
                        conn,
                        place,
                        lease,
                        outcome="error",
                        decision_reason="provider_error",
                        retry_after=retry_after,
                        error_code=error.code,
                    )
                    conn.commit()
                    counts["errors"] += 1
                    reasons[f"error_{error.code}"] += 1
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
                    self.repository.complete(
                        conn,
                        place,
                        lease,
                        outcome="rejected",
                        decision_reason="duplicate_provider_identity",
                        retry_after=timedelta(days=90),
                    )
                    conn.commit()
                    counts["rejected"] += 1
                    reasons["duplicate_provider_identity"] += 1

        return {
            "provider": "yelp",
            "city": city,
            "call_limit": limit,
            "eligible_scanned": counts["scanned"],
            "api_calls": counts["api_calls"],
            "matched": counts["matched"],
            "not_found": counts["not_found"],
            "rejected": counts["rejected"],
            "errors": counts["errors"],
            "not_due": counts["not_due"],
            "lost_lease": counts["lost_lease"],
            "decision_counts": dict(sorted(reasons.items())),
            "stored_provider_attributes": False,
        }


class YelpProviderAudit:
    """Read-only, attribute-free audit of active and rejected Yelp identities."""

    def __init__(
        self,
        db: Database,
        *,
        repository: ProviderLinkRepository | None = None,
    ) -> None:
        self.db = db
        self.repository = repository or ProviderLinkRepository()

    def run(
        self,
        api: YelpPlacesAPI,
        *,
        city: str | None,
        limit: int,
    ) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be positive")
        return {
            "provider": "yelp",
            "city": city,
            "details": self._audit_details(api, city=city, limit=limit),
            "rejected_matches": self._audit_rejected(api, city=city, limit=limit),
            "production_state_mutated": False,
            "stored_provider_attributes": False,
        }

    def _audit_details(
        self,
        api: YelpPlacesAPI,
        *,
        city: str | None,
        limit: int,
    ) -> dict[str, Any]:
        with self.db.connection() as conn:
            linked = self.repository.active_yelp_links(conn, city=city, limit=limit)
        counts: Counter[str] = Counter()
        errors: Counter[str] = Counter()
        exceptions: list[dict[str, Any]] = []
        for place, provider_place_id in linked:
            counts["api_calls"] += 1
            try:
                audit = api.audit_details(provider_place_id, place)
            except YelpAPIError as error:
                errors[error.code] += 1
                exceptions.append(
                    {
                        "establishment_id": place.establishment_id,
                        "name": place.name,
                        "outcome": "error",
                        "reason": error.code,
                    }
                )
                continue
            for field in ("phone", "hours", "price"):
                counts[f"has_{field}"] += int(getattr(audit, f"has_{field}"))
            counts["identity_compatible"] += int(audit.identity_compatible)
            counts["currently_operating"] += int(audit.currently_operating)
            if not audit.identity_compatible or not audit.currently_operating:
                exceptions.append(
                    {
                        "establishment_id": place.establishment_id,
                        "name": place.name,
                        "outcome": "failed",
                        "reason": audit.identity_reason,
                    }
                )
        return {
            "linked_considered": len(linked),
            "api_calls": counts["api_calls"],
            "identity_compatible": counts["identity_compatible"],
            "currently_operating": counts["currently_operating"],
            "attribute_availability": {
                "phone": counts["has_phone"],
                "hours": counts["has_hours"],
                "price": counts["has_price"],
                "venue_website": 0,
            },
            "venue_website_note": "Yelp does not expose the venue website in this endpoint",
            "errors": dict(sorted(errors.items())),
            "exceptions": exceptions,
        }

    def _audit_rejected(
        self,
        api: YelpPlacesAPI,
        *,
        city: str | None,
        limit: int,
    ) -> dict[str, Any]:
        with self.db.connection() as conn:
            rejected = self.repository.rejected_yelp_candidates(
                conn, city=city, limit=limit
            )
        counts: Counter[str] = Counter()
        results: list[dict[str, Any]] = []
        for place, prior_reason in rejected:
            try:
                selection = api.match(place)
            except YelpAPIError as error:
                outcome = "error"
                reason = error.code
            else:
                outcome = selection.outcome
                reason = selection.reason
            counts[outcome] += 1
            results.append(
                {
                    "establishment_id": place.establishment_id,
                    "name": place.name,
                    "prior_reason": prior_reason,
                    "current_outcome": outcome,
                    "current_reason": reason,
                }
            )
        return {
            "considered": len(rejected),
            "api_calls": len(rejected),
            "decision_counts": dict(sorted(counts.items())),
            "results": results,
        }


def provider_match_identity_fingerprint(place: YelpMatchInput) -> str:
    canonical = _canonical_json(
        {
            "provider": "yelp",
            "endpoint": "business_match_identity",
            "api_version": "v1",
            "parameters": {
                "matcher_revision": YELP_MATCH_METHOD,
                "name": place.name.strip(),
                "address": place.address.strip(),
                "city": place.city.strip(),
                "region": place.region.strip() if place.region else None,
                "postal_code": place.postal_code.strip() if place.postal_code else None,
                "country_code": place.country_code.strip().upper(),
                "latitude": _javascript_round_coordinate(place.latitude),
                "longitude": _javascript_round_coordinate(place.longitude),
                "phone_e164": place.phone_e164,
            },
        }
    )
    return f"v1:{sha256(canonical.encode()).hexdigest()}"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _javascript_round_coordinate(value: float) -> float | int:
    if not math.isfinite(value):
        raise ValueError("provider match coordinates must be finite")
    rounded = math.floor(value * 100_000 + 0.5) / 100_000
    return int(rounded) if rounded.is_integer() else rounded


def _retry_after_for_error(code: str) -> timedelta:
    if code in {"unauthorized", "forbidden"}:
        return timedelta(hours=1)
    if code == "invalid_request":
        return timedelta(days=1)
    if code == "rate_limited":
        return timedelta(minutes=15)
    if code == "invalid_payload":
        return timedelta(minutes=30)
    return timedelta(minutes=5)


def _postgres_interval(value: timedelta) -> str:
    seconds = max(60, min(int(value.total_seconds()), 90 * 24 * 60 * 60))
    return f"{seconds} seconds"


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _yelp_match_input(row: dict[str, Any]) -> YelpMatchInput:
    return YelpMatchInput(
        establishment_id=str(row["establishment_id"]),
        name=str(row["name"]),
        address=str(row["address"]),
        city=str(row["city"]),
        region=_optional_text(row.get("region")),
        postal_code=_optional_text(row.get("postal_code")),
        country_code=str(row["country_code"]),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        phone_e164=_optional_text(row.get("phone_e164")),
    )
