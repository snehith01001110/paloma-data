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


def test_rebrand_with_same_phone_and_location_auto_matches():
    old = candidate(
        name="Faultline Brewing Company",
        normalized_name="faultline brewing company",
        address="1235 Oakmead Pkwy",
        normalized_address="1235 oakmead pkwy",
        city="Sunnyvale",
        postal_code="94085",
        latitude=37.38749,
        longitude=-121.99263,
        phone_e164="+14087362739",
    )
    record = SourceRecord(
        source="overture",
        source_record_id="new-brand-id",
        name="Laughing Monk Brewing",
        address="1235 Oakmead Pkwy",
        city="Sunnyvale",
        region="CA",
        postal_code="94085",
        latitude=37.38749,
        longitude=-121.99263,
        phone="+14087362739",
    )
    decision = decide_match(record, [old])
    assert decision.action == "auto_match"
    assert decision.reason == "exact_phone_location"


def test_same_location_name_conflict_is_review_not_new_entity():
    old = candidate(
        name="Old Brand Brewing",
        normalized_name="old brand brewing",
        address="1235 Oakmead Pkwy",
        normalized_address="1235 oakmead pkwy",
        city="Sunnyvale",
        latitude=37.38749,
        longitude=-121.99263,
    )
    record = SourceRecord(
        source="overture",
        source_record_id="new-brand-id",
        name="Completely New Operator Name",
        address="1235 Oakmead Pkwy",
        city="Sunnyvale",
        region="CA",
        latitude=37.38749,
        longitude=-121.99263,
    )
    decision = decide_match(record, [old])
    assert decision.action == "review"
    assert decision.reason == "same_location_name_conflict"
