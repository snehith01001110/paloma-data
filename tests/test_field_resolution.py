from datetime import datetime, timedelta, timezone

from paloma_data.field_resolution import (
    SOURCE_POLICIES,
    FieldResolver,
    _conflict_evidence_ids,
    _filter_changed_decisions,
    _manual_review_covers_current_evidence,
    _require_candidate_contact_corroboration,
    _reapply_manual_projections,
    _review_reason,
)


def test_government_names_are_legal_not_display_names():
    assert SOURCE_POLICIES["ca_abc"].name_kind == "legal"
    assert SOURCE_POLICIES["datasf"].name_kind == "legal"
    assert SOURCE_POLICIES["overture"].name_kind == "display"
    assert SOURCE_POLICIES["fsq"].name_kind == "display"


def test_verified_first_party_name_outranks_aggregator_name():
    resolver = FieldResolver(None)  # scoring is pure and does not touch the database
    now = datetime.now(timezone.utc)
    official = {
        "authority": 1.0,
        "evidence_confidence": 0.995,
        "identity_confidence": 0.995,
        "source_updated_at": now,
    }
    overture = {
        "authority": SOURCE_POLICIES["overture"].name_authority,
        "evidence_confidence": 0.92,
        "identity_confidence": 1.0,
        "source_updated_at": now,
    }
    assert resolver._evidence_score(official) > 0.98
    assert resolver._evidence_score(official) > resolver._evidence_score(overture)


def test_aggregator_name_cannot_look_like_099_field_confidence():
    resolver = FieldResolver(None)
    score = resolver._evidence_score(
        {
            "authority": SOURCE_POLICIES["overture"].name_authority,
            "evidence_confidence": 0.92,
            "identity_confidence": 1.0,
            "source_updated_at": datetime.now(timezone.utc),
        }
    )
    assert score < 0.85


def test_direct_upstream_wins_over_conflicting_overture_copy():
    resolver = FieldResolver(None)
    rows = [
        {
            "evidence_id": "direct",
            "field_name": "phone_e164",
            "value_text": "+14155550100",
            "normalized_value": "+14155550100",
            "value_json": None,
            "source": "fsq",
            "upstream_origin_keys": ["foursquare"],
            "authority": 0.90,
            "evidence_confidence": 0.96,
            "identity_confidence": 1.0,
            "source_updated_at": None,
        },
        {
            "evidence_id": "copy",
            "field_name": "phone_e164",
            "value_text": "+14155550999",
            "normalized_value": "+14155550999",
            "value_json": None,
            "source": "overture",
            "upstream_origin_keys": ["foursquare"],
            "authority": 0.88,
            "evidence_confidence": 0.96,
            "identity_confidence": 1.0,
            "source_updated_at": None,
        },
    ]

    selected = resolver._select_attribute(rows, 0.68)

    assert selected is not None
    assert selected["best_source"] == "fsq"


def test_reviewed_civic_polygon_outranks_a_broader_registration_label():
    resolver = FieldResolver(None)
    base = {
        "field_name": "neighborhood",
        "value_json": None,
        "identity_confidence": 0.985,
        "source_updated_at": None,
        "upstream_origin_keys": ["datasf"],
    }
    rows = [
        {
            **base,
            "evidence_id": "registration",
            "value_text": "Sunset/Parkside",
            "normalized_value": "sunset/parkside",
            "source": "datasf",
            "authority": 0.90,
            "evidence_confidence": 0.96,
        },
        {
            **base,
            "evidence_id": "boundary",
            "value_text": "Outer Sunset",
            "normalized_value": "outer sunset",
            "source": "datasf_neighborhoods",
            "authority": 0.94,
            "evidence_confidence": 0.98,
        },
    ]

    selected = resolver._select_neighborhood(rows)

    assert selected is not None
    assert selected["value_text"] == "Outer Sunset"
    assert selected["best_source"] == "datasf_neighborhoods"


def test_private_candidate_contact_requires_two_independent_origins():
    provider_only = {
        "best_source": "overture",
        "source_count": 1,
        "value_text": "+15105550100",
    }
    corroborated = {**provider_only, "source_count": 2}

    assert (
        _require_candidate_contact_corroboration("phone_e164", provider_only) is None
    )
    assert (
        _require_candidate_contact_corroboration("phone_e164", corroborated)
        is corroborated
    )


def test_reviewed_candidate_contact_can_use_one_first_party_observation():
    reviewed = {
        "best_source": "manual",
        "source_count": 1,
        "value_text": "https://example.com",
    }

    assert (
        _require_candidate_contact_corroboration("website_url", reviewed) is reviewed
    )


def test_unchanged_decisions_are_deduplicated_but_a_return_is_appended():
    first = ("venue", "hours", "selected", "old-fingerprint")
    second = ("venue", "hours", "unknown", "new-fingerprint")

    assert _filter_changed_decisions(
        [first], {("venue", "hours"): "old-fingerprint"}
    ) == []
    assert _filter_changed_decisions(
        [first], {("venue", "hours"): "new-fingerprint"}
    ) == [first]
    assert _filter_changed_decisions(
        [first, second], {("venue", "hours"): "old-fingerprint"}
    ) == [second]


def test_conflict_retains_every_evidence_id():
    rows = [
        {"evidence_id": "second"},
        {"evidence_id": "first"},
        {"evidence_id": "second"},
    ]

    assert _conflict_evidence_ids(rows) == ["first", "second"]


def test_manual_review_is_preserved_until_new_evidence_arrives():
    rows = [{"evidence_id": "first"}, {"evidence_id": "second"}]

    assert _manual_review_covers_current_evidence({"first", "second"}, rows)
    assert not _manual_review_covers_current_evidence({"first"}, rows)
    assert not _manual_review_covers_current_evidence(None, rows)


def test_manual_projection_reconciles_all_mutable_durable_fields():
    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)

    connection = Connection()
    _reapply_manual_projections(connection)

    sql = "\n".join(connection.statements)
    assert len(connection.statements) == 9
    for field in (
        "phone_e164",
        "website_url",
        "address",
        "latitude",
        "longitude",
        "operating_status",
        "neighborhood",
        "hours",
        "price_level",
    ):
        assert field in sql
    assert "hours_expires_at" in sql
    assert "evidence.expires_at > now()" in sql
    assert "observation.metadata as observation_metadata" in sql
    assert "d.observation_metadata->>'evidence_kind'" in sql


def test_first_party_hours_do_not_require_a_second_origin() -> None:
    now = datetime.now(timezone.utc)
    selected = {
        "best_source": "manual",
        "observed_at": now,
        "expires_at": now + timedelta(days=30),
        "source_items": [
            {"kind": "first_party", "url": "https://example.com/hours"}
        ],
        "metadata": {"evidence_kind": "first_party"},
        "independent_origin_keys": ["manual:reviewer"],
    }

    assert _review_reason("hours", [], selected, True) is None


def test_disagreeing_current_hours_enter_the_conflict_queue() -> None:
    selected = {"independent_origin_keys": ["merchant"]}
    rows = [
        {"normalized_value": "schedule-a"},
        {"normalized_value": "schedule-b"},
    ]

    assert (
        _review_reason("hours", rows, selected, True)
        == "authoritative_hours_disagreement"
    )
