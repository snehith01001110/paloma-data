from datetime import datetime, timezone

from paloma_data.publication import LinkedObservation, decide_publication


NOW = datetime(2026, 8, 17, tzinfo=timezone.utc)


def _establishment(**overrides):
    values = {
        "status": "open",
        "name": "The Public House",
        "normalized_name": "the public house",
        "identity_confidence": 0.98,
        "display_name_confidence": 0.80,
        "display_name_source": "fsq",
        "primary_type_slug": "bar",
    }
    values.update(overrides)
    return values


def _consumer(**overrides):
    values = {
        "source": "fsq",
        "source_family": "consumer_poi",
        "name": "The Public House",
        "source_status": "open",
        "source_updated_at": NOW,
        "primary_type_slug": "bar",
        "consumer_facing": True,
        "public_access": "walk_in",
        "quality_flags": (),
        "match_confidence": 0.97,
        "permitted_metadata": {},
    }
    values.update(overrides)
    return LinkedObservation(**values)


def _abc(code: str = "48", **overrides):
    values = {
        "source": "ca_abc",
        "source_family": "government_regulator",
        "name": "PUBLIC HOUSE HOLDINGS LLC",
        "source_status": "open",
        "source_updated_at": NOW,
        "primary_type_slug": "bar",
        "consumer_facing": False,
        "public_access": "walk_in",
        "quality_flags": (),
        "match_confidence": 0.97,
        "permitted_metadata": {"license_type": code},
    }
    values.update(overrides)
    return LinkedObservation(**values)


def test_public_bar_requires_both_current_consumer_poi_and_active_license():
    decision = decide_publication(_establishment(), [_consumer(), _abc()], now=NOW)
    assert decision.state == "published"
    assert decision.primary_type_slug == "bar"
    assert decision.reason == "public_bar_license_and_current_consumer_poi:v1"


def test_state_license_alone_never_publishes():
    decision = decide_publication(_establishment(), [_abc()], now=NOW)
    assert decision.state == "candidate"
    assert decision.reason == "missing_consumer_access_evidence:v1"


def test_generic_manufacturer_poi_does_not_prove_a_taproom():
    generic = _consumer(
        primary_type_slug="brewery", consumer_facing=False, public_access="unknown"
    )
    decision = decide_publication(_establishment(), [generic, _abc("23")], now=NOW)
    assert decision.state == "candidate"
    assert decision.reason == "manufacturer_without_access_evidence:v1"


def test_explicit_taproom_plus_brewery_license_publishes_as_taproom():
    taproom = _consumer(primary_type_slug="taproom")
    decision = decide_publication(_establishment(), [taproom, _abc("23")], now=NOW)
    assert decision.state == "published"
    assert decision.primary_type_slug == "taproom"


def test_consumer_hard_negative_suppresses_even_with_active_license():
    private = _consumer(quality_flags=("privatevenue",), public_access="members_or_private")
    decision = decide_publication(_establishment(), [private, _abc()], now=NOW)
    assert decision.state == "suppressed"
    assert decision.reason == "consumer_hard_negative:v1"


def test_consumer_poi_without_abc_remains_hidden():
    decision = decide_publication(_establishment(), [_consumer()], now=NOW)
    assert decision.state == "candidate"
    assert decision.reason == "missing_active_abc_license:v1"
