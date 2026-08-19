from paloma_data.catalog_repository import (
    POTENTIAL_SOURCE_EXCLUDED_FLAGS,
    CatalogRepository,
)
from paloma_data.models import SourceRecord


class _EmptyCursor:
    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _RecordingConnection:
    def __init__(self):
        self.query = ""
        self.params = ()

    def execute(self, query, params):
        self.query = query
        self.params = params
        return _EmptyCursor()


def test_potential_sources_only_queries_current_eligible_evidence():
    connection = _RecordingConnection()
    repository = CatalogRepository(db=None)
    anchor = SourceRecord(
        source="fsq",
        source_record_id="anchor",
        name="Example Bar",
        address="123 Main St",
        city="San Francisco",
        latitude=37.78,
        longitude=-122.42,
    )

    assert repository.potential_sources(connection, anchor) == []

    assert "sr.source_status = 'open'" in connection.query
    assert "not (sr.quality_flags && %s::text[])" in connection.query
    assert set(connection.params[0]) == POTENTIAL_SOURCE_EXCLUDED_FLAGS
    assert {"stale", "consumer_identity_conflict"}.issubset(connection.params[0])


def test_refresh_candidate_anchor_updates_denormalized_identity_fields():
    connection = _RecordingConnection()
    repository = CatalogRepository(db=None)
    anchor = SourceRecord(
        source="fsq",
        source_record_id="anchor",
        name="Lost Marbles Brewpub",
        address="823 Clement St",
        city="San Francisco",
        region="CA",
        postal_code="94118",
        latitude=37.782,
        longitude=-122.467,
        primary_type_slug="brewpub",
    )

    repository.refresh_candidate_anchor(connection, "candidate-id", anchor)

    assert "primary_type_slug = %s" in connection.query
    assert "anchor_source_record_id = %s" in connection.query
    assert "brewpub" in connection.params
    assert "candidate-id" in connection.params


def test_candidate_id_for_source_rechecks_exact_source_identity():
    connection = _RecordingConnection()
    repository = CatalogRepository(db=None)
    anchor = SourceRecord(
        source="fsq",
        source_record_id="anchor",
        name="Example Bar",
        address="123 Main St",
        city="San Francisco",
    )

    assert repository.candidate_id_for_source(connection, anchor) is None

    assert "from ingest.candidate_source_links" in connection.query
    assert connection.params == ("fsq", "anchor")


def test_runtime_provider_link_stores_only_the_allowed_foursquare_identifier():
    connection = _RecordingConnection()
    repository = CatalogRepository(db=None)

    repository.upsert_runtime_provider_links(connection, "candidate-id")

    assert "insert into ingest.runtime_provider_links" in connection.query
    assert "'foursquare'" in connection.query
    assert "csl.source_record_id" in connection.query
    assert "source_record.consumer_facing" in connection.query
    assert "source_record.public_access = 'walk_in'" in connection.query
    assert "csl.identity_confidence >= 0.96" in connection.query
    assert "not exists (select 1 from eligible)" in connection.query
    assert connection.params[0] == "candidate-id"
    assert set(connection.params[1]) == POTENTIAL_SOURCE_EXCLUDED_FLAGS
    assert connection.params[2:] == ("candidate-id", "candidate-id")
