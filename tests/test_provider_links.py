from contextlib import contextmanager

from paloma_data.adapters.yelp import YelpMatchInput, YelpMatchSelection
from paloma_data.provider_links import (
    ProviderLinkRepository,
    ProviderLinkSync,
    ProviderMatchLease,
    provider_match_identity_fingerprint,
)


PLACE = YelpMatchInput(
    establishment_id="00000000-0000-0000-0000-000000000001",
    name="Revision Test Bar",
    address="1 Market St",
    city="San Francisco",
    region="CA",
    postal_code="94105",
    country_code="US",
    latitude=37.7936,
    longitude=-122.3958,
    phone_e164=None,
)


class _Cursor:
    def __init__(self, *, one=None, all_rows=None):
        self.one = one
        self.all_rows = all_rows or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all_rows


class _RecordingConnection:
    def __init__(self, cursor=None):
        self.query = ""
        self.params = ()
        self.cursor = cursor or _Cursor()

    def execute(self, query, params):
        self.query = query
        self.params = params
        return self.cursor


def test_candidate_query_is_fail_closed_to_publishable_walk_in_places():
    connection = _RecordingConnection()

    assert ProviderLinkRepository().candidates(
        connection,
        city="San Francisco",
        scan_limit=50,
    ) == []

    assert "publication_state = 'published'" in connection.query
    assert "access_mode = 'walk_in'" in connection.query
    assert "verification_expires_at > now()" in connection.query
    assert "candidate.identity_confidence >= 0.96" in connection.query
    assert connection.params == ("San Francisco", "San Francisco", 50)


def test_identity_fingerprint_matches_the_edge_function_contract():
    assert provider_match_identity_fingerprint(PLACE) == (
        "v1:14525ca31fb80f28d0c56c8bd98785c59d9573747c1a6d5a936a896d3aa7a8c2"
    )


class _Connection:
    def commit(self):
        pass

    def rollback(self):
        pass


class _DB:
    @contextmanager
    def connection(self):
        yield _Connection()


class _Repository:
    def __init__(self):
        self.completed = []

    def candidates(self, _conn, *, city, scan_limit):
        assert city == "San Francisco"
        assert scan_limit == 100
        return [PLACE]

    def claim(self, _conn, _place, fingerprint):
        return ProviderMatchLease("lease", fingerprint)

    def store_match(self, _conn, _place, _lease, *, provider_place_id, confidence):
        assert provider_place_id == "WavvLdfdP6g8aZTtbBQHTw"
        assert confidence == 0.99
        return True

    def complete(self, *_args, **kwargs):
        self.completed.append(kwargs)
        return True


class _API:
    def match(self, _place):
        return YelpMatchSelection(
            "matched",
            "matched",
            provider_place_id="WavvLdfdP6g8aZTtbBQHTw",
            confidence=0.99,
        )


def test_sync_persists_only_the_durable_match_and_reports_api_calls():
    repository = _Repository()

    result = ProviderLinkSync(_DB(), repository=repository).run(
        _API(), city="San Francisco", limit=5
    )

    assert result["api_calls"] == 1
    assert result["matched"] == 1
    assert result["stored_provider_attributes"] is False
    assert repository.completed == []
