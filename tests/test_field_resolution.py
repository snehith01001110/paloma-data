from datetime import datetime, timezone

from paloma_data.field_resolution import FieldResolver, SOURCE_POLICIES


def test_government_names_are_legal_not_display_names():
    assert SOURCE_POLICIES["ca_abc"].name_kind == "legal"
    assert SOURCE_POLICIES["datasf"].name_kind == "legal"
    assert SOURCE_POLICIES["overture"].name_kind == "display"


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
