from paloma_data.catalog_repository import (
    POTENTIAL_SOURCE_EXCLUDED_FLAGS,
    CatalogRepository,
    _overlay_public_field_projection,
)
from paloma_data.catalog import CatalogDecision
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


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _NeighborhoodConnection:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.queries = []
        self.params = []

    def execute(self, query, params):
        self.queries.append(query)
        self.params.append(params)
        return _Cursor(next(self.rows))


class _PublishedCandidateEvaluationConnection:
    def __init__(self):
        self.queries = []
        self.params = []

    def execute(self, query, params):
        self.queries.append(query)
        self.params.append(params)
        if "from public.establishments" in query:
            return _Cursor(
                {"establishment_id": "candidate-id", "publication_state": "published"}
            )
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

    assert "insert into runtime.runtime_provider_links" in connection.query
    assert "'foursquare'" in connection.query
    assert "csl.source_record_id" in connection.query
    assert "source_record.consumer_facing" in connection.query
    assert "source_record.public_access = 'walk_in'" in connection.query
    assert "csl.identity_confidence >= 0.96" in connection.query
    assert "reviewed_identity_exception:anchor_source_id" in connection.query
    assert "candidate_verifications" in connection.query
    assert "establishment.publication_state = 'published'" in connection.query
    assert "not exists (select 1 from eligible)" in connection.query
    assert connection.params[0] == "candidate-id"
    assert set(connection.params[1]) == POTENTIAL_SOURCE_EXCLUDED_FLAGS
    assert connection.params[2:] == (
        "candidate-id",
        "candidate-id",
        "candidate-id",
        "candidate-id",
        "candidate-id",
    )


def test_materialized_candidate_selection_is_scoped_to_publication_state():
    connection = _RecordingConnection()
    repository = CatalogRepository(db=None)

    assert repository.materialized_candidate_ids(
        connection,
        city="San Francisco",
        limit=200,
        publication_states=("published", "suppressed"),
    ) == []

    assert "join public.establishments" in connection.query
    assert "e.publication_state = any(%s::text[])" in connection.query
    assert connection.params == (
        "San Francisco",
        "San Francisco",
        ["published", "suppressed"],
        200,
    )


def test_new_publication_candidate_selection_is_scoped_to_release_cities():
    connection = _RecordingConnection()
    repository = CatalogRepository(db=None)

    assert repository.candidate_ids(
        connection,
        cities=("Berkeley", "Oakland"),
        limit=25,
        states=("verified",),
        decision_version="v7",
    ) == []

    assert "lower(city) = any(%s::text[])" in connection.query
    assert connection.params == (
        None,
        None,
        ["berkeley", "oakland"],
        ["berkeley", "oakland"],
        ["verified"],
        ["verified"],
        "v7",
        "v7",
        25,
    )


def test_unpublished_verified_candidate_selection_excludes_materialized_rows():
    connection = _RecordingConnection()
    repository = CatalogRepository(db=None)

    assert repository.unpublished_verified_candidate_ids(
        connection,
        cities=("Mountain View", "San Jose"),
        limit=1,
        decision_version="v7",
    ) == []

    assert "not exists" in connection.query
    assert "public.establishments" in connection.query
    assert "c.candidate_state = 'verified'" in connection.query
    assert connection.params == (["mountain view", "san jose"], "v7", 1)


def test_materialize_allows_a_published_candidate_for_an_existing_identity_refresh():
    connection = _RecordingConnection()
    repository = CatalogRepository(db=None)

    assert repository.materialize(connection, "candidate-id") is False

    assert "candidate_state = 'verified'" in connection.query
    assert "candidate_state = 'published'" in connection.query
    assert "from public.establishments" in connection.query
    assert "catalog_candidate_id = ingest.catalog_candidates.id" in connection.query


def test_save_evaluation_preserves_published_candidate_state_when_still_verified():
    connection = _PublishedCandidateEvaluationConnection()
    repository = CatalogRepository(db=None)
    decision = CatalogDecision(
        state="verified",
        reason="all_hard_gates_passed:v7",
        reasons=("all_hard_gates_passed",),
        identity_confidence=0.99,
    )

    repository.save_evaluation(connection, "candidate-id", decision, mode="production")

    update_params = connection.params[-1]
    assert "update ingest.catalog_candidates" in connection.queries[-1]
    assert update_params[0] == "published"


def test_boundary_adjacent_neighborhood_uses_independent_coordinate_consensus():
    connection = _NeighborhoodConnection(
        [
            None,
            {
                "name": "Inner Sunset",
                "source": "datasf_neighborhoods:linked_coordinate_consensus",
                "authority": 0.96,
                "independent_votes": 2,
                "origin_keys": ["ca_abc", "overture"],
            },
        ]
    )
    repository = CatalogRepository(db=None)
    resolved = {
        "city": "San Francisco",
        "latitude": 37.766,
        "longitude": -122.466,
        "field_sources": {},
        "field_confidences": {},
    }

    assert repository._attach_civic_neighborhood(
        connection, "candidate-id", resolved
    )

    assert resolved["neighborhood"] == "Inner Sunset"
    assert (
        resolved["field_sources"]["neighborhood"]
        == "datasf_neighborhoods:linked_coordinate_consensus"
    )
    assert resolved["field_confidences"]["neighborhood"] == 0.96
    consensus_query = connection.queries[1]
    assert "having count(distinct name) = 1" in consensus_query
    assert "independent_votes >= 2" in consensus_query
    assert "independent_votes > coalesce" in consensus_query
    assert connection.params[1][0:2] == ("San Francisco", "candidate-id")


def test_neighborhood_stays_blank_when_direct_and_consensus_resolution_fail():
    connection = _NeighborhoodConnection([None, None])
    repository = CatalogRepository(db=None)
    resolved = {
        "city": "San Francisco",
        "latitude": 37.80,
        "longitude": -122.41,
    }

    assert not repository._attach_civic_neighborhood(
        connection, "candidate-id", resolved
    )
    assert "neighborhood" not in resolved


def test_materialization_preserves_the_rights_aware_public_field_projection():
    resolved = {
        "name": "Legal Entity Name",
        "normalized_name": "legal entity name",
        "phone_e164": "+14155550000",
        "website_url": "https://stale.example",
        "neighborhood": "Candidate Neighborhood",
        "hours": {"stale": True},
        "price_level": 4,
        "field_sources": {"phone": "candidate"},
        "field_confidences": {"phone": 0.95},
    }
    current = {
        "name": "The Display Name",
        "normalized_name": "the display name",
        "display_name_source": "overture",
        "display_name_confidence": 0.91,
        "field_resolution_version": "v5-rights-aware",
        "phone_e164": "+14155550123",
        "phone_source": "overture",
        "phone_confidence": 0.88,
        "website_url": "https://example.com",
        "website_source": "overture",
        "website_confidence": 0.84,
        "neighborhood": None,
        "neighborhood_source": None,
        "neighborhood_confidence": None,
        "hours": None,
        "hours_source": None,
        "hours_confidence": None,
        "price_level": None,
        "price_source": None,
        "price_confidence": None,
    }

    assert _overlay_public_field_projection(resolved, current)

    assert resolved["name"] == "The Display Name"
    assert resolved["phone_e164"] == "+14155550123"
    assert resolved["website_url"] == "https://example.com"
    assert resolved["neighborhood"] is None
    assert resolved["hours"] is None
    assert resolved["price_level"] is None
    assert resolved["field_sources"]["phone"] == "overture"
    assert resolved["field_confidences"]["website"] == 0.84


def test_initial_catalog_materialization_still_uses_candidate_fields():
    resolved = {"name": "Example Bar", "phone_e164": "+14155550123"}

    assert not _overlay_public_field_projection(resolved, None)
    assert not _overlay_public_field_projection(
        resolved,
        {"field_resolution_version": "v7"},
    )
    assert resolved == {"name": "Example Bar", "phone_e164": "+14155550123"}
