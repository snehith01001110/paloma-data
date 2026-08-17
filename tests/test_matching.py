from paloma_data.matching import decide_match, score_match
from paloma_data.models import CanonicalCandidate, SourceRecord


def candidate(**overrides):
    values = dict(
        id="11111111-1111-1111-1111-111111111111",
        name="The Alembic",
        normalized_name="the alembic",
        address="1725 Haight St",
        normalized_address="1725 haight st",
        city="San Francisco",
        region="CA",
        postal_code="94117",
        country_code="US",
        latitude=37.7694,
        longitude=-122.4510,
        phone_e164=None,
        website_url=None,
        status="open",
    )
    values.update(overrides)
    return CanonicalCandidate(**values)


def test_exact_address_and_name_auto_match():
    record = SourceRecord(
        source="datasf",
        source_record_id="x",
        name="The Alembic",
        address="1725 Haight Street",
        city="San Francisco",
        region="CA",
        latitude=37.7694,
        longitude=-122.4510,
    )
    decision = decide_match(record, [candidate()])
    assert decision.action == "auto_match"
    assert decision.candidate_id is not None


def test_different_venue_is_distinct():
    record = SourceRecord(
        source="datasf",
        source_record_id="x",
        name="Completely Different Place",
        address="1 Market St",
        city="San Francisco",
        region="CA",
        latitude=37.7936,
        longitude=-122.3958,
    )
    score, _ = score_match(record, candidate())
    assert score < 0.8
    assert decide_match(record, [candidate()]).action == "distinct"
