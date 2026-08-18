from paloma_data.catalog_repository import (
    POTENTIAL_SOURCE_EXCLUDED_FLAGS,
    CatalogRepository,
)
from paloma_data.models import SourceRecord


class _EmptyCursor:
    def fetchall(self):
        return []


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
