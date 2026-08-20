from datetime import datetime, timedelta, timezone

from paloma_data.catalog import (
    LinkedSource,
    VerificationEvidence,
    decide_candidate,
    decide_identity,
    manual_attestation,
    provider_verification,
)
from paloma_data.models import SourceRecord


NOW = datetime(2026, 8, 17, tzinfo=timezone.utc)


def _fsq(**overrides) -> SourceRecord:
    values = {
        "source": "fsq",
        "source_record_id": "fsq-1",
        "name": "El Lopo",
        "address": "1327 Polk St",
        "city": "San Francisco",
        "region": "CA",
        "country_code": "US",
        "latitude": 37.789,
        "longitude": -122.42,
        "phone": "+14155551212",
        "website_url": "https://ellopo.example",
        "source_status": "open",
        "source_updated_at": NOW - timedelta(days=10),
        "primary_type_slug": "wine_bar",
        "classification_confidence": 0.99,
        "source_family": "consumer_poi",
        "consumer_facing": True,
        "public_access": "walk_in",
        "origin_keys": ("foursquare",),
        "data_license": "Apache-2.0",
    }
    values.update(overrides)
    return SourceRecord(**values)


def _abc(**overrides) -> SourceRecord:
    values = {
        "source": "ca_abc",
        "source_record_id": "123:42",
        "name": "EL LOPO",
        "address": "1327 POLK ST",
        "city": "SAN FRANCISCO",
        "region": "CA",
        "country_code": "US",
        "latitude": 37.789,
        "longitude": -122.42,
        "source_status": "open",
        "source_updated_at": NOW - timedelta(days=30),
        "primary_type_slug": "bar",
        "classification_confidence": 0.99,
        "source_family": "government_regulator",
        "consumer_facing": False,
        "public_access": "walk_in",
        "origin_keys": ("ca_abc",),
        "permitted_metadata": {
            "license_type": "42",
            "type_status": "ACTIVE",
            "license_or_application": "LIC",
        },
    }
    values.update(overrides)
    return SourceRecord(**values)


def _links(fsq: SourceRecord | None = None, abc: SourceRecord | None = None):
    result = []
    if fsq:
        result.append(LinkedSource(fsq, 1.0, "anchor_source_id"))
    if abc:
        result.append(LinkedSource(abc, 0.985, "exact_address_strong_name"))
    return result


def _verification(**overrides) -> VerificationEvidence:
    values = {
        "verifier": "fsq_premium",
        "verifier_record_id": "fsq-1",
        "outcome": "pass",
        "verification_tier": "provider",
        "checks": {
            "identity": True,
            "currently_operating": True,
            "public_access": True,
            "display_name": True,
            "venue_type": True,
        },
        "permitted_snapshot": {
            "name": "El Lopo",
            "primary_type_slug": "wine_bar",
            "address": "1327 Polk St",
            "city": "San Francisco",
            "region": "CA",
            "country_code": "US",
            "latitude": 37.789,
            "longitude": -122.42,
            "hours": {"friday": [["16:00", "02:00"]]},
            "price_level": 2,
        },
        "storage_policy": "contract",
        "verified_at": NOW,
        "expires_at": NOW + timedelta(days=45),
    }
    values.update(overrides)
    return VerificationEvidence(**values)


def test_all_hard_gates_publish_a_bar_and_resolve_only_evidenced_fields():
    decision = decide_candidate(_links(_fsq(), _abc()), [_verification()], now=NOW)

    assert decision.state == "verified"
    assert decision.reason == "all_hard_gates_passed:v7"
    assert decision.resolved["name"] == "El Lopo"
    assert decision.resolved["hours"] == {"friday": [["16:00", "02:00"]]}
    assert decision.resolved["price_level"] == 2
    assert decision.resolved["cover_image_url"] is None


def test_open_fsq_and_direct_public_premises_license_can_verify_without_api_cache():
    decision = decide_candidate(_links(_fsq(), _abc()), [], now=NOW)

    assert decision.state == "verified"
    assert decision.verification_tier == "open_evidence"
    assert decision.expires_at == NOW + timedelta(days=45)
    assert decision.resolved["name"] == "El Lopo"
    assert decision.resolved["phone_e164"] is None
    assert decision.resolved["website_url"] is None
    assert decision.resolved["hours"] is None


def test_open_evidence_keeps_contact_fields_only_when_independent_sources_agree():
    fsq = _fsq()
    osm = _fsq(
        source="osm",
        source_record_id="osm-1",
        origin_keys=("openstreetmap",),
    )
    links = [
        LinkedSource(fsq, 1.0, "anchor_source_id"),
        LinkedSource(_abc(), 0.985, "exact_address_strong_name"),
        LinkedSource(osm, 0.985, "exact_address_strong_name"),
    ]

    decision = decide_candidate(links, [], now=NOW)

    assert decision.state == "verified"
    assert decision.resolved["phone_e164"] == "+14155551212"
    assert decision.resolved["website_url"] == "https://ellopo.example"
    assert decision.resolved["field_sources"]["phone"] == "fsq+osm"


def test_shared_foursquare_lineage_does_not_corroborate_contact_fields():
    fsq = _fsq()
    copied = _fsq(
        source="overture",
        source_record_id="overture-copy",
        origin_keys=("foursquare", "meta"),
    )
    links = [
        LinkedSource(fsq, 1.0, "anchor_source_id"),
        LinkedSource(_abc(), 0.985, "exact_address_strong_name"),
        LinkedSource(copied, 0.985, "exact_address_strong_name"),
    ]

    decision = decide_candidate(links, [], now=NOW)

    assert decision.state == "verified"
    assert decision.resolved["phone_e164"] is None
    assert decision.resolved["website_url"] is None


def test_unknown_legacy_overture_lineage_does_not_corroborate_contact_fields():
    fsq = _fsq()
    copied = _fsq(
        source="overture",
        source_record_id="legacy-overture-copy",
        origin_keys=("overture",),
    )
    links = [
        LinkedSource(fsq, 1.0, "anchor_source_id"),
        LinkedSource(_abc(), 0.985, "exact_address_strong_name"),
        LinkedSource(copied, 0.985, "exact_address_strong_name"),
    ]

    decision = decide_candidate(links, [], now=NOW)

    assert decision.state == "verified"
    assert decision.resolved["phone_e164"] is None
    assert decision.resolved["website_url"] is None


def test_property_level_overture_lineage_can_independently_corroborate_contacts():
    fsq = _fsq()
    overture = _fsq(
        source="overture",
        source_record_id="overture-meta",
        origin_keys=("meta", "overture:overture-signals"),
        field_provenance={
            "phone_e164": {"origin_keys": ["meta"]},
            "website_url": {"origin_keys": ["meta"]},
        },
    )
    links = [
        LinkedSource(fsq, 1.0, "anchor_source_id"),
        LinkedSource(_abc(), 0.985, "exact_address_strong_name"),
        LinkedSource(overture, 0.985, "exact_address_strong_name"),
    ]

    decision = decide_candidate(links, [], now=NOW)

    assert decision.state == "verified"
    assert decision.resolved["phone_e164"] == "+14155551212"
    assert decision.resolved["website_url"] == "https://ellopo.example"


def test_eating_place_license_needs_provider_or_manual_access_verification():
    abc = _abc(
        source_record_id="123:47",
        permitted_metadata={
            "license_type": "47",
            "type_status": "ACTIVE",
            "license_or_application": "LIC",
        },
    )

    decision = decide_candidate(_links(_fsq(), abc), [], now=NOW)

    assert decision.state == "needs_verification"
    assert decision.reason == "missing_high_quality_verification:v7"


def test_exact_premise_similarity_gap_is_queued_for_review():
    anchor = _fsq(name="Slainte", address="131 Broadway", primary_type_slug="pub")
    abc = _abc(name="SLAINTE PUB", address="131 BROADWAY")

    decision = decide_identity(anchor, abc)

    assert 0.75 <= decision.features["name"] < 0.78
    assert decision.action == "review"
    assert decision.reason == "same_location_name_conflict"


def test_manual_attestation_retains_evidence_but_no_optional_provider_fields():
    anchor = _fsq(
        phone="+14155551212",
        website_url="https://provider.example",
        hours={"friday": [["16:00", "02:00"]]},
        price_level=3,
    )

    verification = manual_attestation(
        anchor,
        reviewer="github:reviewer",
        evidence_urls=("https://official.example/location",),
        note="Current first-party location page confirms walk-in service.",
        observed_at=NOW,
    )

    assert verification.verification_tier == "manual"
    assert verification.storage_policy == "manual"
    assert verification.expires_at == NOW + timedelta(days=90)
    assert verification.permitted_snapshot["phone"] is None
    assert verification.permitted_snapshot["website_url"] is None
    assert verification.permitted_snapshot["hours"] is None
    assert verification.permitted_snapshot["price_level"] is None
    assert verification.permitted_snapshot["_attestation"]["evidence_urls"] == [
        "https://official.example/location"
    ]


def test_manual_attestation_can_correct_coarse_type_but_still_requires_compatible_abc():
    fsq = _fsq(primary_type_slug="pub")
    abc = _abc(
        source_record_id="123:23",
        primary_type_slug="brewery",
        permitted_metadata={
            "license_type": "23",
            "type_status": "ACTIVE",
            "license_or_application": "LIC",
        },
    )
    manual = manual_attestation(
        fsq,
        reviewer="github:reviewer",
        evidence_urls=("https://brewery.example/taproom",),
        venue_type="taproom",
        observed_at=NOW,
    )

    decision = decide_candidate(_links(fsq, abc), [manual], now=NOW)

    assert decision.state == "verified"
    assert decision.resolved["primary_type_slug"] == "taproom"
    assert decision.resolved["hours"] is None


def test_current_manual_hard_negative_withdraws_the_exact_candidate():
    fsq = _fsq(primary_type_slug="winery")
    abc = _abc(
        source_record_id="123:02",
        primary_type_slug="winery",
        permitted_metadata={
            "license_type": "02",
            "type_status": "ACTIVE",
            "license_or_application": "LIC",
        },
    )
    closed = manual_attestation(
        fsq,
        reviewer="github:reviewer",
        evidence_urls=("https://news.example/confirmed-closure",),
        outcome="fail",
        note="The establishment left this premise and no longer offers public access.",
        observed_at=NOW,
    )

    decision = decide_candidate(_links(fsq, abc), [closed], now=NOW)

    assert closed.checks["identity"] is True
    assert closed.checks["currently_operating"] is False
    assert decision.state == "withdrawn"
    assert decision.reason == "current_verifier_failure:v7"


def test_raw_abc_status_must_be_exactly_active_even_if_canonical_status_is_open():
    bad_abc = _abc(permitted_metadata={"license_type": "42", "type_status": "SUREND"})
    decision = decide_candidate(_links(_fsq(), bad_abc), [_verification()], now=NOW)

    assert decision.state == "withdrawn"
    assert decision.reason == "abc_license_not_active:v7"


def test_overture_and_a_license_cannot_replace_the_required_fsq_anchor():
    overture = _fsq(
        source="overture",
        source_record_id="overture-1",
        origin_keys=("foursquare", "meta"),
    )
    decision = decide_candidate(_links(overture, _abc()), [_verification()], now=NOW)

    assert decision.state == "needs_verification"
    assert decision.reason == "missing_current_fsq_os_anchor:v7"


def test_stale_fsq_anchor_requires_current_durable_verification():
    stale_fsq = _fsq(source_updated_at=NOW - timedelta(days=366))

    decision = decide_candidate(_links(stale_fsq, _abc()), [], now=NOW)

    assert decision.state == "needs_verification"
    assert decision.reason == "missing_current_fsq_os_anchor:v7"


def test_current_manual_attestation_supersedes_stale_fsq_timestamp():
    stale_fsq = _fsq(source_updated_at=NOW - timedelta(days=366))
    manual = _verification(
        verifier="manual",
        verification_tier="manual",
        storage_policy="manual",
    )

    decision = decide_candidate(_links(stale_fsq, _abc()), [manual], now=NOW)

    assert decision.state == "verified"
    assert decision.verification_tier == "manual"


def test_expired_manual_attestation_cannot_supersede_stale_fsq_timestamp():
    stale_fsq = _fsq(source_updated_at=NOW - timedelta(days=366))
    manual = _verification(
        verifier="manual",
        verification_tier="manual",
        storage_policy="manual",
        verified_at=NOW - timedelta(days=91),
        expires_at=NOW - timedelta(days=1),
    )

    decision = decide_candidate(_links(stale_fsq, _abc()), [manual], now=NOW)

    assert decision.state == "needs_verification"
    assert decision.reason == "missing_current_fsq_os_anchor:v7"


def test_ephemeral_api_result_can_pass_a_trial_but_never_production():
    verification = _verification(storage_policy="ephemeral", permitted_snapshot={})
    abc = _abc(
        source_record_id="123:47",
        permitted_metadata={
            "license_type": "47",
            "type_status": "ACTIVE",
            "license_or_application": "LIC",
        },
    )

    trial = decide_candidate(
        _links(_fsq(), abc), [verification], now=NOW, mode="trial"
    )
    production = decide_candidate(
        _links(_fsq(), abc), [verification], now=NOW, mode="production"
    )

    assert trial.state == "verified"
    assert production.state == "needs_verification"
    assert production.reason == "verification_expired_or_not_storable:v7"


def test_latest_provider_result_supersedes_an_older_failure():
    old_failure = _verification(
        outcome="fail",
        verified_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=44),
    )
    latest_pass = _verification(verified_at=NOW, expires_at=NOW + timedelta(days=45))

    decision = decide_candidate(
        _links(_fsq(), _abc()), [old_failure, latest_pass], now=NOW
    )

    assert decision.state == "verified"


def test_inconclusive_provider_result_never_withdraws_a_candidate():
    abc = _abc(
        source_record_id="123:47",
        permitted_metadata={
            "license_type": "47",
            "type_status": "ACTIVE",
            "license_or_application": "LIC",
        },
    )
    verification = _verification(outcome="inconclusive")

    decision = decide_candidate(_links(_fsq(), abc), [verification], now=NOW)

    assert decision.state == "needs_verification"
    assert decision.reason == "missing_high_quality_verification:v7"


def test_provider_pass_for_a_different_foursquare_id_never_carries_over():
    stale_pass = _verification(verifier_record_id="old-fsq-id")
    abc = _abc(
        source_record_id="123:47",
        permitted_metadata={
            "license_type": "47",
            "type_status": "ACTIVE",
            "license_or_application": "LIC",
        },
    )

    decision = decide_candidate(_links(_fsq(), abc), [stale_pass], now=NOW)

    assert decision.state == "needs_verification"
    assert decision.reason == "missing_high_quality_verification:v7"


def test_tasting_room_requires_hours_or_manual_attestation():
    fsq = _fsq(primary_type_slug="tasting_room")
    abc = _abc(
        source_record_id="123:2",
        primary_type_slug="winery",
        public_access="unknown",
        permitted_metadata={
            "license_type": "02",
            "type_status": "ACTIVE",
            "license_or_application": "LIC",
        },
    )
    verification = _verification(
        permitted_snapshot={
            "name": "El Lopo",
            "primary_type_slug": "tasting_room",
            "address": "1327 Polk St",
            "city": "San Francisco",
            "country_code": "US",
            "latitude": 37.789,
            "longitude": -122.42,
        }
    )

    decision = decide_candidate(_links(fsq, abc), [verification], now=NOW)

    assert decision.state == "needs_review"
    assert "manufacturer_access_requires_hours" in decision.reason


def test_same_address_different_business_is_never_auto_linked():
    anchor = _fsq()
    plaza = _fsq(
        source="overture",
        source_record_id="plaza",
        name="Lower Polk Plaza",
        website_url=None,
        phone=None,
    )

    decision = decide_identity(anchor, plaza)

    assert decision.action == "review"
    assert decision.reason == "same_location_name_conflict"


def test_provider_verification_requires_veracity_four_or_five():
    details = _fsq(
        source="fsq_premium",
        provider_veracity=3,
        hours={"friday": [["16:00", "02:00"]]},
        storage_scope="contract",
    )

    verification = provider_verification(
        details,
        candidate_anchor=_fsq(),
        observed_at=NOW,
        storage_policy="contract",
    )

    assert verification.outcome == "inconclusive"
    assert verification.checks["provider_veracity"] is False
    assert verification.checks["has_hours"] is True


def test_provider_category_without_current_hours_does_not_claim_public_access():
    details = _fsq(
        source="fsq_premium",
        provider_veracity=5,
        hours=None,
        storage_scope="contract",
    )

    verification = provider_verification(
        details,
        candidate_anchor=_fsq(),
        observed_at=NOW,
        storage_policy="contract",
    )

    assert verification.outcome == "inconclusive"
    assert verification.checks["public_access"] is False


def test_identity_matched_explicit_provider_closure_is_a_hard_failure():
    details = _fsq(
        source="fsq_premium",
        source_status="closed",
        provider_veracity=5,
        hours={"friday": [["16:00", "02:00"]]},
        storage_scope="contract",
    )

    verification = provider_verification(
        details,
        candidate_anchor=_fsq(),
        observed_at=NOW,
        storage_policy="contract",
    )

    assert verification.outcome == "fail"
    assert verification.checks["identity"] is True
    assert verification.checks["currently_operating"] is False


def test_generic_brewery_requires_manual_public_access_even_with_provider_hours():
    fsq = _fsq(primary_type_slug="brewery")
    abc = _abc(
        source_record_id="123:23",
        primary_type_slug="brewery",
        permitted_metadata={
            "license_type": "23",
            "type_status": "ACTIVE",
            "license_or_application": "LIC",
        },
    )
    provider = _verification(
        permitted_snapshot={
            "name": "El Lopo Brewing",
            "primary_type_slug": "brewery",
            "address": "1327 Polk St",
            "city": "San Francisco",
            "country_code": "US",
            "latitude": 37.789,
            "longitude": -122.42,
        }
    )
    provider_with_hours = _verification(
        permitted_snapshot={
            **provider.permitted_snapshot,
            "hours": {"friday": [["16:00", "22:00"]]},
        }
    )
    manual = _verification(
        verifier="manual",
        verification_tier="manual",
        storage_policy="manual",
        permitted_snapshot=provider.permitted_snapshot,
    )

    without_hours = decide_candidate(_links(fsq, abc), [provider], now=NOW)
    with_hours = decide_candidate(_links(fsq, abc), [provider_with_hours], now=NOW)

    assert without_hours.state == "needs_review"
    assert "generic_manufacturer_requires_manual_public_access" in without_hours.reason
    assert with_hours.state == "needs_review"
    assert "generic_manufacturer_requires_manual_public_access" in with_hours.reason
    assert decide_candidate(_links(fsq, abc), [manual], now=NOW).state == "verified"


def test_provider_hours_do_not_claim_public_access_for_generic_manufacturer():
    details = _fsq(
        source="fsq_premium",
        primary_type_slug="winery",
        provider_veracity=5,
        hours={"friday": [["11:00", "17:00"]]},
        storage_scope="contract",
    )

    verification = provider_verification(
        details,
        candidate_anchor=_fsq(primary_type_slug="winery"),
        observed_at=NOW,
        storage_policy="contract",
    )

    assert verification.outcome == "inconclusive"
    assert verification.checks["has_hours"] is True
    assert verification.checks["public_access"] is False


def test_provider_can_verify_explicit_tasting_room_with_current_hours():
    details = _fsq(
        source="fsq_premium",
        primary_type_slug="tasting_room",
        provider_veracity=5,
        hours={"friday": [["11:00", "17:00"]]},
        storage_scope="contract",
    )

    verification = provider_verification(
        details,
        candidate_anchor=_fsq(primary_type_slug="tasting_room"),
        observed_at=NOW,
        storage_policy="contract",
    )

    assert verification.outcome == "pass"
    assert verification.checks["public_access"] is True


def test_eating_place_license_can_validate_but_not_classify_a_bar():
    abc = _abc(
        source_record_id="123:47",
        permitted_metadata={
            "license_type": "47",
            "type_status": "ACTIVE",
            "license_or_application": "LIC",
        },
    )

    decision = decide_candidate(_links(_fsq(), abc), [_verification()], now=NOW)

    assert decision.state == "verified"


def test_empty_provider_regular_hours_do_not_prove_manufacturer_access():
    details = _fsq(
        source="fsq_premium",
        primary_type_slug="brewery",
        provider_veracity=5,
        hours={"regular": []},
        storage_scope="contract",
    )

    verification = provider_verification(
        details,
        candidate_anchor=_fsq(primary_type_slug="brewery"),
        observed_at=NOW,
        storage_policy="contract",
    )

    assert verification.outcome == "inconclusive"
    assert verification.checks["public_access"] is False
