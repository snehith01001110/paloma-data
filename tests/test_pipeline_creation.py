from paloma_data.models import SourceRecord
from paloma_data.pipeline import Pipeline, _combine_for_creation, _safe_to_create


def _record(**overrides) -> SourceRecord:
    values = {
        "source": "overture",
        "source_record_id": "poi-1",
        "name": "The Public House",
        "address": "1 Market St",
        "city": "San Francisco",
        "region": "CA",
        "latitude": 37.79,
        "longitude": -122.39,
        "source_status": "open",
        "primary_type_slug": "bar",
        "classification_confidence": 0.94,
        "source_family": "consumer_poi",
        "consumer_facing": True,
        "public_access": "walk_in",
    }
    values.update(overrides)
    return SourceRecord(**values)


def test_manufacturer_license_can_never_create_a_consumer_establishment():
    abc = _record(
        source="ca_abc",
        source_record_id="123:23",
        name="OGDEN BREWING LLC",
        primary_type_slug="brewery",
        classification_confidence=0.99,
        source_family="government_regulator",
        consumer_facing=False,
        public_access="unknown",
    )

    assert _safe_to_create(abc, 0.99) is False


def test_consumer_bar_can_create_only_a_hidden_canonical_candidate():
    assert _safe_to_create(_record(), 0.94) is True


def test_community_enrichment_can_never_create_a_canonical_establishment():
    osm = _record(source="osm", source_family="community_enrichment")

    assert _safe_to_create(osm, 0.99) is False


def test_public_license_and_consumer_subtype_combine_under_consumer_name():
    abc = _record(
        source="ca_abc",
        source_record_id="456:48",
        name="CRANE ASSEMBLY THE",
        primary_type_slug="bar",
        classification_confidence=0.90,
        source_family="government_regulator",
        consumer_facing=False,
    )
    poi = _record(name="The Crane Assembly", primary_type_slug="cocktail_bar")

    combined = _combine_for_creation(abc, poi)

    assert combined is not None
    assert combined.name == "The Crane Assembly"
    assert combined.primary_type_slug == "cocktail_bar"
    assert combined.consumer_facing is True


def test_two_consumer_aggregators_do_not_count_as_independent_corroboration():
    fsq = _record(source="fsq", source_record_id="fsq-1")
    overture = _record(source="overture", source_record_id="overture-1")
    assert fsq.source_family == overture.source_family == "consumer_poi"


class _AlreadyLinkedCorroboration:
    def __init__(self, corroborating):
        self.corroborating = corroborating
        self.links = []

    def find_candidates(self, *_):
        return []

    def find_source_corroboration(self, *_):
        return self.corroborating, 0.98

    def linked_establishment_id(self, _, source, source_record_id):
        if (source, source_record_id) == (
            self.corroborating.source,
            self.corroborating.source_record_id,
        ):
            return "existing-venue"
        return None

    def upsert_source_link(self, _, establishment_id, record, confidence, method):
        self.links.append((establishment_id, record.source_record_id, confidence, method))

    def create_establishment(self, *_):
        raise AssertionError("an already-linked corroborating source must not create a duplicate")


def test_already_linked_corroboration_attaches_to_the_existing_canonical_id():
    consumer = _record()
    database = _AlreadyLinkedCorroboration(consumer)
    pipeline = Pipeline(
        database,
        allowed_cities=frozenset({"San Francisco"}),
        allowed_regions=frozenset({"CA"}),
        allowed_countries=frozenset({"US"}),
    )
    abc = _record(
        source="ca_abc",
        source_record_id="abc-48",
        source_family="government_regulator",
        consumer_facing=False,
        public_access="walk_in",
    )
    counters = {"created": 0, "updated": 0, "review": 0, "closed": 0}

    pipeline._resolve_record(None, abc, counters)

    assert counters["created"] == 0
    assert counters["updated"] == 1
    assert database.links == [
        ("existing-venue", "abc-48", 0.98, "linked_source_corroboration")
    ]
