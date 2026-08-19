from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from paloma_data.models import SourceRecord
from paloma_data.normalizers import (
    haversine_meters,
    normalize_address,
    normalize_name,
    normalize_phone,
    normalize_url,
    similarity,
    website_host,
)
from paloma_data.taxonomy import (
    BAR_TYPES,
    CONSUMER_VENUE_TYPES,
    GENERIC_MANUFACTURER_TYPES,
)


CATALOG_DECISION_VERSION = "v7"
FSQ_OS_FRESHNESS_DAYS = 365
DEFAULT_PROVIDER_LEASE_DAYS = 45
DEFAULT_MANUAL_LEASE_DAYS = 90
DEFAULT_OPEN_EVIDENCE_LEASE_DAYS = 45

PUBLIC_BAR_LICENSES = frozenset({"40", "42", "48", "61"})
PUBLIC_EATING_PLACE_LICENSES = frozenset({"41", "47", "87"})
BREWERY_LICENSES = frozenset({"1", "23"})
WINERY_LICENSES = frozenset({"2"})
DISTILLERY_LICENSES = frozenset({"74"})
BREWPUB_LICENSES = frozenset({"75"})
HARD_NEGATIVE_FLAGS = frozenset(
    {
        "closed",
        "delete",
        "doesnt_exist",
        "does_not_exist",
        "duplicate",
        "inappropriate",
        "privatevenue",
        "private_venue",
    }
)
REQUIRED_VERIFICATION_CHECKS = frozenset(
    {"identity", "currently_operating", "public_access", "display_name", "venue_type"}
)


@dataclass(frozen=True, slots=True)
class IdentityDecision:
    action: str
    score: float
    reason: str
    features: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LinkedSource:
    record: SourceRecord
    identity_confidence: float
    match_method: str


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    verifier: str
    verifier_record_id: str
    outcome: str
    verification_tier: str
    checks: dict[str, bool]
    permitted_snapshot: dict[str, Any]
    storage_policy: str
    verified_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CatalogDecision:
    state: str
    reason: str
    reasons: tuple[str, ...]
    identity_confidence: float
    verification_tier: str = "unverified"
    verified_at: datetime | None = None
    expires_at: datetime | None = None
    resolved: dict[str, Any] = field(default_factory=dict)


def decide_identity(anchor: SourceRecord, other: SourceRecord) -> IdentityDecision:
    """Conservative entity linkage using conjunctive signals and explicit conflict bands."""
    if anchor.source == other.source and anchor.source_record_id == other.source_record_id:
        return IdentityDecision("match", 1.0, "exact_source_id")
    if anchor.country_code.strip().casefold() != other.country_code.strip().casefold():
        return IdentityDecision("distinct", 0.0, "country_conflict")
    if anchor.city.casefold() != other.city.casefold():
        return IdentityDecision("distinct", 0.0, "city_conflict")

    name_score = similarity(normalize_name(anchor.name), normalize_name(other.name))
    address_score = similarity(
        normalize_address(anchor.address), normalize_address(other.address)
    )
    anchor_phone = normalize_phone(anchor.phone, anchor.country_code)
    other_phone = normalize_phone(other.phone, other.country_code)
    phone_exact = bool(anchor_phone and other_phone and anchor_phone == other_phone)
    anchor_host = website_host(anchor.website_url)
    other_host = website_host(other.website_url)
    website_exact = bool(anchor_host and other_host and anchor_host == other_host)
    distance: float | None = None
    if None not in (
        anchor.latitude,
        anchor.longitude,
        other.latitude,
        other.longitude,
    ):
        distance = haversine_meters(
            float(anchor.latitude),
            float(anchor.longitude),
            float(other.latitude),
            float(other.longitude),
        )
    features: dict[str, float | None] = {
        "name": name_score,
        "address": address_score,
        "phone": 1.0 if phone_exact else 0.0,
        "website": 1.0 if website_exact else 0.0,
        "distance_m": distance,
    }

    near_50 = distance is not None and distance <= 50
    near_25 = distance is not None and distance <= 25
    same_door = distance is not None and distance <= 15
    exact_address = address_score >= 0.985

    if phone_exact and (exact_address or near_25) and name_score >= 0.55:
        return IdentityDecision("match", 0.995, "exact_phone_same_location", features)
    if website_exact and (address_score >= 0.94 or near_25) and name_score >= 0.65:
        return IdentityDecision("match", 0.99, "exact_website_same_location", features)
    if exact_address and name_score >= 0.90:
        return IdentityDecision("match", 0.985, "exact_address_strong_name", features)
    if near_25 and name_score >= 0.94:
        return IdentityDecision("match", 0.975, "same_door_strong_name", features)
    if near_50 and address_score >= 0.96 and name_score >= 0.92:
        return IdentityDecision("match", 0.97, "nearby_address_and_name", features)

    # ABC's DBA may be absent, but an exact licensed premise and a reasonably similar name can
    # still link conservatively. Divergent names at the same address remain human work.
    if other.source == "ca_abc" or anchor.source == "ca_abc":
        if exact_address and name_score >= 0.84:
            return IdentityDecision("match", 0.965, "abc_exact_premise_name", features)

    if (exact_address or same_door) and name_score < 0.75:
        return IdentityDecision("review", 0.85, "same_location_name_conflict", features)
    if address_score >= 0.95 and name_score >= 0.78:
        return IdentityDecision("review", 0.84, "probable_identity_needs_review", features)
    return IdentityDecision("distinct", 0.0, "insufficient_identity_evidence", features)


def decide_candidate(
    links: list[LinkedSource],
    verifications: list[VerificationEvidence],
    *,
    now: datetime | None = None,
    mode: str = "production",
) -> CatalogDecision:
    """Fail-closed publication decision; no weighted score can bypass a missing fact."""
    current_time = _utc(now or datetime.now(timezone.utc))
    if not links:
        return _decision("rejected", "no_linked_sources")

    records = [link.record for link in links]
    consumer = [record for record in records if record.consumer_facing]
    flags = {flag for record in consumer for flag in record.quality_flags}
    if flags & HARD_NEGATIVE_FLAGS:
        return _decision("withdrawn", "consumer_hard_negative")

    abc_records = [record for record in records if record.source == "ca_abc"]
    if abc_records and not any(_abc_raw_active(record) for record in abc_records):
        return _decision("withdrawn", "abc_license_not_active")
    active_abc = [record for record in abc_records if _abc_raw_active(record)]
    if not active_abc:
        return _decision("needs_verification", "missing_exact_active_abc")

    fsq_records = [
        record
        for record in consumer
        if record.source == "fsq"
        and record.source_status == "open"
        and not (set(record.quality_flags) & HARD_NEGATIVE_FLAGS)
    ]
    if not fsq_records:
        return _decision("needs_verification", "missing_current_fsq_os_anchor")
    chosen = max(
        fsq_records,
        key=lambda record: (
            _fresh(record.source_updated_at, current_time, FSQ_OS_FRESHNESS_DAYS),
            record.primary_type_slug in CONSUMER_VENUE_TYPES,
            record.classification_confidence or 0.0,
            _timestamp(record.source_updated_at),
        ),
    )
    if chosen.primary_type_slug not in CONSUMER_VENUE_TYPES:
        return _decision("rejected", "consumer_type_not_supported")

    compatible_abc = [
        record
        for record in active_abc
        if _license_supports_type(_license_code(record), chosen.primary_type_slug)
    ]
    if not compatible_abc:
        return _decision("rejected", "abc_license_incompatible_with_venue_type")

    fsq_link = _link_for(links, chosen)
    abc_links = [_link_for(links, record) for record in compatible_abc]
    identity_confidence = min(
        [fsq_link.identity_confidence, *(link.identity_confidence for link in abc_links)]
    )
    if identity_confidence < 0.96:
        return _decision(
            "needs_review", "identity_below_publication_threshold", identity=identity_confidence
        )

    # A verification is evidence about one exact provider identity.  Never let a pass for an
    # old/merged Foursquare ID silently carry over to a newly linked place.
    latest_verifications = [
        item
        for item in _latest_verifications(verifications)
        if _verification_applies(item, chosen)
    ]
    failures = [
        item
        for item in latest_verifications
        if item.outcome == "fail" and _utc(item.expires_at) > current_time
    ]
    if failures:
        return _decision(
            "withdrawn", "current_verifier_failure", identity=identity_confidence
        )
    passing = [
        item
        for item in latest_verifications
        if item.outcome == "pass"
        and _utc(item.expires_at) > current_time
        and REQUIRED_VERIFICATION_CHECKS.issubset(
            {key for key, value in item.checks.items() if value is True}
        )
        and (mode == "trial" or item.storage_policy in {"contract", "manual"})
    ]
    # The monthly FSQ OS timestamp is the default current-operation signal. A current durable
    # verification bound to this exact FSQ ID may supersede that timestamp: a contracted provider
    # check or reviewed manual attestation is stronger, fresher evidence than an unchanged bulk
    # row. Ephemeral API observations can do so only in trial mode and are never persisted.
    if (
        not _fresh(chosen.source_updated_at, current_time, FSQ_OS_FRESHNESS_DAYS)
        and not passing
    ):
        return _decision(
            "needs_verification",
            "missing_current_fsq_os_anchor",
            identity=identity_confidence,
        )
    if passing:
        verification = max(passing, key=lambda item: _utc(item.verified_at))
    else:
        # FSQ OS + an exact ACTIVE *public-premises* ABC license is independently sufficient
        # for ordinary bars.  This is not a license-only rule: FSQ supplies the current public
        # identity/type while ABC supplies the legal walk-in premise.  Restaurant licenses and
        # manufacturer licenses deliberately cannot use this shortcut.
        verification = _open_evidence_verification(
            chosen,
            compatible_abc,
            now=current_time,
        )
        if verification is None:
            if any(item.outcome == "pass" for item in latest_verifications):
                return _decision(
                    "needs_verification",
                    "verification_expired_or_not_storable",
                    identity=identity_confidence,
                )
            return _decision(
                "needs_verification",
                "missing_high_quality_verification",
                identity=identity_confidence,
            )
    resolved = _resolve_fields(chosen, records, verification)
    # A generic producer identity plus posted business hours can still be a bonded warehouse,
    # production office, or appointment-only facility.  Provider automation may verify an
    # explicitly classified consumer venue (taproom/tasting room); generic manufacturers need a
    # human attestation of ordinary public access.
    if (
        chosen.primary_type_slug in GENERIC_MANUFACTURER_TYPES
        and verification.verification_tier != "manual"
    ):
        return _decision(
            "needs_review",
            "generic_manufacturer_requires_manual_public_access",
            identity=identity_confidence,
        )
    if chosen.primary_type_slug in {"taproom", "tasting_room"}:
        if not _has_hours(resolved.get("hours")) and verification.verification_tier != "manual":
            return _decision(
                "needs_review",
                "manufacturer_access_requires_hours_or_manual_attestation",
                identity=identity_confidence,
            )

    missing_required = [
        field
        for field in (
            "name",
            "primary_type_slug",
            "address",
            "city",
            "country_code",
            "latitude",
            "longitude",
        )
        if resolved.get(field) in (None, "")
    ]
    if missing_required:
        return _decision(
            "needs_review",
            "missing_required_materialization_fields",
            reasons=tuple(f"missing:{field}" for field in missing_required),
            identity=identity_confidence,
        )

    return CatalogDecision(
        state="verified",
        reason=f"all_hard_gates_passed:{CATALOG_DECISION_VERSION}",
        reasons=("all_hard_gates_passed",),
        identity_confidence=identity_confidence,
        verification_tier=verification.verification_tier,
        verified_at=_utc(verification.verified_at),
        expires_at=_utc(verification.expires_at),
        resolved=resolved,
    )


def provider_verification(
    record: SourceRecord,
    *,
    candidate_anchor: SourceRecord,
    observed_at: datetime | None = None,
    storage_policy: str,
    lease_days: int = DEFAULT_PROVIDER_LEASE_DAYS,
) -> VerificationEvidence:
    """Convert a contracted provider detail into explicit, auditable hard checks."""
    timestamp = _utc(observed_at or datetime.now(timezone.utc))
    identity = decide_identity(candidate_anchor, record)
    quality_flags = set(record.quality_flags)
    current = record.source_status == "open" and not (
        quality_flags & HARD_NEGATIVE_FLAGS
    )
    explicit_type = record.primary_type_slug in CONSUMER_VENUE_TYPES
    explicit_access_type = (
        explicit_type and record.primary_type_slug not in GENERIC_MANUFACTURER_TYPES
    )
    # The API has no explicit public-access field.  An access-specific consumer category plus a
    # non-empty current schedule is the minimum automated proxy. Generic manufacturer categories
    # are deliberately excluded; a manual attestation can cover those and venues without hours.
    access = bool(
        explicit_access_type
        and record.public_access == "walk_in"
        and _has_hours(record.hours)
    )
    checks = {
        "identity": identity.action == "match" and identity.score >= 0.96,
        "currently_operating": current,
        "public_access": access,
        "display_name": bool(record.name.strip()),
        "venue_type": explicit_type
        and _consumer_types_compatible(
            candidate_anchor.primary_type_slug, record.primary_type_slug
        ),
        "provider_veracity": bool(
            record.provider_veracity is not None and record.provider_veracity >= 4
        ),
    }
    checks.update(
        {
            "has_phone": bool(record.phone),
            "has_website": bool(record.website_url),
            "has_neighborhood": bool(record.neighborhood),
            "has_hours": _has_hours(record.hours),
            "has_price": record.price_level is not None,
        }
    )
    passed = all(checks[key] for key in REQUIRED_VERIFICATION_CHECKS) and checks[
        "provider_veracity"
    ]
    # A missing field, low-veracity payload, provider reclassification, or stale provider ID is
    # not evidence that the establishment closed. Only an identity-matched record with an
    # explicit operating/access hard negative can withdraw an otherwise verified candidate.
    explicit_hard_negative = checks["identity"] and (
        record.source_status == "closed"
        or bool(
            quality_flags
            & {
                "closed",
                "doesnt_exist",
                "does_not_exist",
                "privatevenue",
                "private_venue",
            }
        )
    )
    outcome = "pass" if passed else "fail" if explicit_hard_negative else "inconclusive"
    return VerificationEvidence(
        verifier=record.source,
        verifier_record_id=record.source_record_id,
        outcome=outcome,
        verification_tier="provider",
        checks=checks,
        # Ephemeral trials may evaluate this snapshot in process, but repository methods refuse
        # to persist it.  This lets the trial assess manufacturer hours/field coverage without
        # violating Foursquare's no-server-caching rule for self-service API attributes.
        permitted_snapshot=_record_snapshot(record),
        storage_policy=storage_policy,
        verified_at=timestamp,
        expires_at=timestamp + timedelta(days=lease_days),
    )


def _resolve_fields(
    anchor: SourceRecord,
    records: list[SourceRecord],
    verification: VerificationEvidence,
) -> dict[str, Any]:
    snapshot = dict(verification.permitted_snapshot)
    verified_name = str(snapshot.get("name") or anchor.name).strip()
    address = str(snapshot.get("address") or anchor.address).strip()
    city = str(snapshot.get("city") or anchor.city).strip()
    country = str(snapshot.get("country_code") or anchor.country_code or "US").strip()

    if verification.verification_tier == "open_evidence":
        # FSQ's date_refreshed means at least one reference for the place was refreshed; it is
        # not field-level proof that a phone number or website is still current.  Keep optional
        # contact data only when an independent durable source agrees exactly.
        phone, phone_source = _corroborated_phone(records, country)
        website, website_source = _corroborated_website(records)
        # Neighborhoods are attached later from a reviewed civic polygon.  Do not preserve an
        # old free-text POI label as if it came from that boundary source.
        neighborhood = None
    else:
        phone = snapshot.get("phone")
        phone_source = verification.verifier if phone else None
        website = snapshot.get("website_url")
        website_source = verification.verifier if website else None
        neighborhood = snapshot.get("neighborhood")
    hours = snapshot.get("hours") if "hours" in snapshot else anchor.hours
    price = snapshot.get("price_level") if "price_level" in snapshot else anchor.price_level
    settings = set(anchor.setting_slugs)
    settings.update(snapshot.get("setting_slugs") or ())
    for record in records:
        settings.update(record.setting_slugs)

    field_source = (
        anchor.source
        if verification.verification_tier == "open_evidence"
        else verification.verifier
    )
    return {
        "name": verified_name,
        "normalized_name": normalize_name(verified_name),
        "primary_type_slug": snapshot.get("primary_type_slug") or anchor.primary_type_slug,
        "address": address,
        "normalized_address": normalize_address(address),
        "city": city,
        "region": snapshot.get("region") or anchor.region,
        "postal_code": snapshot.get("postal_code") or anchor.postal_code,
        "country_code": country,
        "latitude": snapshot.get("latitude", anchor.latitude),
        "longitude": snapshot.get("longitude", anchor.longitude),
        "phone_e164": normalize_phone(str(phone), country) if phone else None,
        "website_url": normalize_url(str(website)) if website else None,
        "neighborhood": neighborhood,
        "hours": hours,
        "price_level": price if price in {1, 2, 3, 4} else None,
        "setting_slugs": sorted(settings),
        "cover_image_url": None,
        "field_sources": {
            "name": field_source,
            "type": field_source,
            "address": field_source,
            "location": field_source,
            "phone": phone_source,
            "website": website_source,
            "hours": field_source if hours is not None else None,
            "price": field_source if price is not None else None,
            "neighborhood": field_source if neighborhood is not None else None,
            "settings": field_source if settings else None,
        },
    }


def _corroborated_phone(
    records: list[SourceRecord], country_code: str
) -> tuple[str | None, str | None]:
    observations: list[tuple[str, SourceRecord, frozenset[str]]] = []
    for record in records:
        normalized = normalize_phone(record.phone, country_code)
        origins = _origins(record)
        if normalized and origins:
            observations.append((normalized, record, origins))
    return _independently_agreed_value(observations)


def _corroborated_website(
    records: list[SourceRecord],
) -> tuple[str | None, str | None]:
    observations: list[tuple[str, SourceRecord, frozenset[str]]] = []
    display_values: dict[tuple[str, str], str] = {}
    for record in records:
        normalized = normalize_url(record.website_url)
        host = website_host(normalized)
        origins = _origins(record)
        if normalized and host and origins:
            observations.append((host, record, origins))
            display_values[(host, record.source)] = normalized
    value, source = _independently_agreed_value(observations)
    if not value or not source:
        return None, None
    matching = sorted(
        normalized
        for (host, _), normalized in display_values.items()
        if host == value
    )
    return (matching[0], source) if matching else (None, None)


def _independently_agreed_value(
    observations: list[tuple[str, SourceRecord, frozenset[str]]],
) -> tuple[str | None, str | None]:
    """Require equal values from two records with non-overlapping upstream lineage."""
    for index, (value, left, left_origins) in enumerate(observations):
        for other_value, right, right_origins in observations[index + 1 :]:
            if value != other_value or left_origins & right_origins:
                continue
            sources = "+".join(sorted({left.source, right.source}))
            return value, sources
    return None, None


def _origins(record: SourceRecord) -> frozenset[str]:
    origins = frozenset(record.origin_keys or (record.source,))
    if record.source == "overture" and any(
        origin == "overture" or origin.startswith("overture:")
        for origin in origins
    ):
        # A generic/unknown Overture lineage may conceal a copy of FSQ or another observation.
        # It remains useful for identity review but cannot count as independent field evidence.
        return frozenset()
    return origins


def _open_evidence_verification(
    anchor: SourceRecord,
    compatible_abc: list[SourceRecord],
    *,
    now: datetime,
) -> VerificationEvidence | None:
    """Build a short lease only where two open sources prove complementary hard facts."""
    codes = {_license_code(record) for record in compatible_abc}
    direct_public_bar = anchor.primary_type_slug in BAR_TYPES and bool(
        codes & PUBLIC_BAR_LICENSES
    )
    explicit_brewpub = anchor.primary_type_slug == "brewpub" and bool(
        codes & BREWPUB_LICENSES
    )
    if not (direct_public_bar or explicit_brewpub):
        return None
    if anchor.source_updated_at is None:
        return None

    freshness_deadline = _utc(anchor.source_updated_at) + timedelta(
        days=FSQ_OS_FRESHNESS_DAYS
    )
    expires_at = min(
        now + timedelta(days=DEFAULT_OPEN_EVIDENCE_LEASE_DAYS),
        freshness_deadline,
    )
    if expires_at <= now:
        return None
    return VerificationEvidence(
        verifier="fsq_os_plus_abc",
        verifier_record_id=anchor.source_record_id,
        outcome="pass",
        verification_tier="open_evidence",
        checks={key: True for key in REQUIRED_VERIFICATION_CHECKS},
        permitted_snapshot=_record_snapshot(anchor),
        storage_policy="open",
        verified_at=now,
        expires_at=expires_at,
    )


def _record_snapshot(record: SourceRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "primary_type_slug": record.primary_type_slug,
        "address": record.address,
        "city": record.city,
        "region": record.region,
        "postal_code": record.postal_code,
        "country_code": record.country_code,
        "latitude": record.latitude,
        "longitude": record.longitude,
        "phone": record.phone,
        "website_url": record.website_url,
        "neighborhood": record.neighborhood,
        "hours": record.hours,
        "price_level": record.price_level,
        "setting_slugs": list(record.setting_slugs),
        "provider_veracity": record.provider_veracity,
    }


def _abc_raw_active(record: SourceRecord) -> bool:
    status = str(record.permitted_metadata.get("type_status") or "").strip().upper()
    kind = str(record.permitted_metadata.get("license_or_application") or "").strip().upper()
    return record.source_status == "open" and status == "ACTIVE" and not kind.startswith("APP")


def _license_code(record: SourceRecord) -> str:
    value = record.permitted_metadata.get("license_type")
    text = str(value or "").strip().lstrip("0")
    return text or "0"


def _license_supports_type(code: str, primary_type: str) -> bool:
    if primary_type in BAR_TYPES:
        return code in PUBLIC_BAR_LICENSES | PUBLIC_EATING_PLACE_LICENSES
    if primary_type == "brewery":
        return code in BREWERY_LICENSES
    if primary_type == "taproom":
        return code in BREWERY_LICENSES
    if primary_type == "winery":
        return code in WINERY_LICENSES
    if primary_type == "distillery":
        return code in DISTILLERY_LICENSES
    if primary_type == "tasting_room":
        return code in WINERY_LICENSES | DISTILLERY_LICENSES
    if primary_type == "brewpub":
        return code in BREWERY_LICENSES | BREWPUB_LICENSES
    return False


def _link_for(links: list[LinkedSource], record: SourceRecord) -> LinkedSource:
    return next(
        link
        for link in links
        if link.record.source == record.source
        and link.record.source_record_id == record.source_record_id
    )


def _latest_verifications(
    verifications: list[VerificationEvidence],
) -> list[VerificationEvidence]:
    latest: dict[tuple[str, str], VerificationEvidence] = {}
    for item in verifications:
        key = (item.verifier, item.verifier_record_id)
        if key not in latest or _utc(item.verified_at) > _utc(latest[key].verified_at):
            latest[key] = item
    return list(latest.values())


def _verification_applies(
    verification: VerificationEvidence,
    anchor: SourceRecord,
) -> bool:
    """Bind every stored pass/fail to the exact consumer identity being evaluated."""
    if verification.verification_tier not in {"provider", "manual"}:
        return False
    return verification.verifier_record_id == anchor.source_record_id


def _consumer_types_compatible(left: str | None, right: str | None) -> bool:
    if not left or not right or left == right:
        return True
    if left in BAR_TYPES and right in BAR_TYPES:
        return True
    compatible = {
        frozenset({"brewery", "taproom"}),
        frozenset({"brewery", "brewpub"}),
        frozenset({"winery", "tasting_room"}),
        frozenset({"distillery", "tasting_room"}),
    }
    return frozenset({left, right}) in compatible


def _fresh(value: datetime | None, now: datetime, days: int) -> bool:
    return value is not None and _utc(value) >= now - timedelta(days=days)


def _has_hours(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, dict):
        if "regular" in value:
            return isinstance(value["regular"], list) and bool(value["regular"])
        return any(_has_hours(item) for item in value.values())
    return False


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _timestamp(value: datetime | None) -> float:
    return _utc(value).timestamp() if value is not None else 0.0


def _decision(
    state: str,
    reason: str,
    *,
    reasons: tuple[str, ...] = (),
    identity: float = 0.0,
) -> CatalogDecision:
    return CatalogDecision(
        state=state,
        reason=f"{reason}:{CATALOG_DECISION_VERSION}",
        reasons=(reason, *reasons),
        identity_confidence=identity,
    )
