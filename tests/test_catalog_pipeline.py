from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from paloma_data.catalog import (
    CATALOG_DECISION_VERSION,
    CatalogDecision,
    decide_identity,
)
from paloma_data.adapters.foursquare_api import FoursquarePlaceUnusableError
from paloma_data.catalog_pipeline import CatalogPipeline, _review_evidence
from paloma_data.models import SourceRecord


NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


class _Connection:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class _Database:
    def __init__(self):
        self.conn = _Connection()

    @contextmanager
    def connection(self):
        yield self.conn


class _PublicationRepository:
    def __init__(self):
        self.materialized = []
        self.requested_version = None

    def withdraw_expired(self, _):
        return 0

    def candidate_ids(self, _, **kwargs):
        self.requested_version = kwargs.get("decision_version")
        return ["candidate-1"]

    def materialize(self, _, candidate_id):
        self.materialized.append(candidate_id)
        return True


class _ExpansionGate:
    manifest = SimpleNamespace(sha256="a" * 64)

    def arm(self, _connection, release_id):
        assert release_id == "east-bay-pilot-v1"
        release = SimpleNamespace(
            cities=("Berkeley", "Oakland"),
            maximum_new_publications=25,
        )
        return release, {"available_slots": 25}


class _ReviewRepository:
    def __init__(self, blocking: bool):
        self.blocking = blocking

    def has_blocking_match_review(self, _, candidate_id):
        assert candidate_id == "candidate-1"
        return self.blocking


class _ResolutionConnection(_Connection):
    def __init__(self):
        super().__init__()
        self.executed = []

    def execute(self, query, params):
        self.executed.append((query, params))


class _ResolutionDatabase(_Database):
    def __init__(self):
        self.conn = _ResolutionConnection()


class _ResolutionRepository:
    def __init__(self):
        self.resolutions = []
        self.links = []
        self.anchor = SourceRecord(
            source="fsq",
            source_record_id="fsq-1",
            name="Little Bird",
            address="435 13th St",
            city="Oakland",
            country_code="US",
            latitude=37.803295,
            longitude=-122.271321,
            primary_type_slug="cocktail_bar",
        )
        self.record = SourceRecord(
            source="ca_abc",
            source_record_id="abc-1",
            name="Radio Bar",
            address="435 13th St",
            city="Oakland",
            country_code="US",
            latitude=37.80327,
            longitude=-122.27086,
            primary_type_slug="bar",
        )

    def pending_match_review_candidate_id(self, _, review_id):
        assert review_id == 123
        return "candidate-1"

    def pending_match_review(self, _, review_id):
        assert review_id == 123
        identity = decide_identity(self.anchor, self.record)
        return {
            "candidate_id": "candidate-1",
            "reason": identity.reason,
            "score": identity.score,
            "evidence": _review_evidence(self.anchor, self.record, identity),
            "record": self.record,
        }

    def fsq_anchor(self, _, candidate_id):
        assert candidate_id == "candidate-1"
        return self.anchor

    def resolve_match_review(self, _, review_id, *, resolution, resolved_by, note=None):
        self.resolutions.append((review_id, resolution, resolved_by, note))
        return "candidate-1"

    def link_source(self, _, candidate_id, record, **kwargs):
        self.links.append((candidate_id, record.source_record_id, kwargs))
        return True


class _AlreadyLinkedDiscoveryRepository:
    def __init__(self, anchor):
        self.anchor = anchor

    def discoverable_anchors(self, *_args, **_kwargs):
        return [self.anchor]

    def candidate_id_for_source(self, _, record):
        assert record is self.anchor
        return "candidate-that-already-owns-source"


class _UnusableVerificationRepository:
    def __init__(self, anchor):
        self.anchor = anchor
        self.evaluations = []

    def fsq_anchor(self, _, candidate_id):
        assert candidate_id == "candidate-1"
        return self.anchor

    def verifications(self, _, candidate_id):
        assert candidate_id == "candidate-1"
        return []

    def save_evaluation(self, _, candidate_id, decision, **kwargs):
        self.evaluations.append((candidate_id, decision.state, kwargs))


class _UnusableAPI:
    storage_policy = "ephemeral"

    def details(self, fsq_place_id):
        raise FoursquarePlaceUnusableError(f"unusable {fsq_place_id}")


class _NoVerificationFallbackRepository:
    def verification_candidate_ids(self, *_args, **_kwargs):
        raise AssertionError("an explicit empty scope must not expand to other candidates")


class _VerificationConnection(_Connection):
    def execute(self, *_args, **_kwargs):
        return None


class _VerificationDatabase(_Database):
    def __init__(self):
        self.conn = _VerificationConnection()


class _RowCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _RefreshConnection(_Connection):
    def execute(self, query, _params=None):
        if "select candidate_state" in query:
            return _RowCursor({"candidate_state": "published"})
        if "select c.candidate_state" in query:
            return _RowCursor(
                {"candidate_state": "published", "publication_state": "published"}
            )
        return _RowCursor(None)


class _RefreshDatabase(_Database):
    def __init__(self):
        self.conn = _RefreshConnection()


class _UnmaterializedRefreshConnection(_RefreshConnection):
    def execute(self, query, params=None):
        if "select c.candidate_state" in query:
            return _RowCursor(
                {"candidate_state": "verified", "publication_state": None}
            )
        return super().execute(query, params)


class _UnmaterializedRefreshDatabase(_Database):
    def __init__(self):
        self.conn = _UnmaterializedRefreshConnection()


class _RefreshRepository:
    def __init__(self):
        self.materialized = []

    def materialized_publication(self, _, candidate_id):
        assert candidate_id == "candidate-1"
        return {"establishment_id": "candidate-1", "publication_state": "published"}

    def materialize(self, _, candidate_id):
        self.materialized.append(candidate_id)
        return True


class _UnmaterializedRefreshRepository(_RefreshRepository):
    def materialized_publication(self, _, candidate_id):
        assert candidate_id == "candidate-1"
        return None

    def materialize(self, *_):
        raise AssertionError("refresh must not publish an unmaterialized candidate")


def _decision(state: str) -> CatalogDecision:
    return CatalogDecision(
        state=state,
        reason=f"test:{CATALOG_DECISION_VERSION}",
        reasons=("test",),
        identity_confidence=0.99,
        verification_tier="open_evidence" if state == "verified" else "unverified",
        verified_at=NOW if state == "verified" else None,
        expires_at=NOW + timedelta(days=45) if state == "verified" else None,
    )


def test_publish_rechecks_current_decision_and_skips_a_new_failure():
    pipeline = CatalogPipeline(_Database())
    repository = _PublicationRepository()
    pipeline.repo = repository
    pipeline.expansion_gate = _ExpansionGate()
    pipeline._evaluate_candidate = lambda *_: _decision("needs_verification")

    result = pipeline.publish(release_id="east-bay-pilot-v1", limit=10)

    assert repository.requested_version == CATALOG_DECISION_VERSION
    assert repository.materialized == []
    assert result["release_id"] == "east-bay-pilot-v1"
    assert result["manifest_sha256"] == "a" * 64
    assert result["scope_cities"] == ["Berkeley", "Oakland"]
    assert result["authorized_limit"] == 25
    assert result["available_slots_before"] == 25
    assert result["considered"] == 1
    assert result["published"] == 0
    assert result["skipped"] == 1
    assert result["expired_withdrawn"] == 0


def test_publish_materializes_only_after_current_decision_passes():
    pipeline = CatalogPipeline(_Database())
    repository = _PublicationRepository()
    pipeline.repo = repository
    pipeline.expansion_gate = _ExpansionGate()
    pipeline._evaluate_candidate = lambda *_: _decision("verified")

    result = pipeline.publish(release_id="east-bay-pilot-v1", limit=10)

    assert repository.requested_version == CATALOG_DECISION_VERSION
    assert repository.materialized == ["candidate-1"]
    assert result["published"] == 1


def test_single_candidate_refresh_updates_an_existing_public_identity():
    database = _RefreshDatabase()
    pipeline = CatalogPipeline(database)
    repository = _RefreshRepository()
    pipeline.repo = repository
    pipeline._evaluate_candidate = lambda *_: _decision("verified")

    result = pipeline.refresh_candidate("candidate-1")

    assert repository.materialized == ["candidate-1"]
    assert result["candidate_state"] == "published"
    assert result["publication_action"] == "refreshed"
    assert database.conn.commits == 1


def test_single_candidate_refresh_never_publishes_a_new_identity():
    pipeline = CatalogPipeline(_UnmaterializedRefreshDatabase())
    pipeline.repo = _UnmaterializedRefreshRepository()
    pipeline._evaluate_candidate = lambda *_: _decision("verified")

    result = pipeline.refresh_candidate("candidate-1")

    assert result["candidate_state"] == "verified"
    assert result["publication_after"] is None
    assert result["publication_action"] == "unchanged"


def test_exact_address_name_conflict_demotes_an_otherwise_verified_candidate(
    monkeypatch,
):
    pipeline = CatalogPipeline(_Database())
    pipeline.repo = _ReviewRepository(blocking=True)
    monkeypatch.setattr(
        "paloma_data.catalog_pipeline.decide_candidate",
        lambda *_args, **_kwargs: _decision("verified"),
    )

    decision = pipeline._decide_candidate(
        pipeline.db.conn,
        "candidate-1",
        [],
        [],
        mode="production",
    )

    assert decision.state == "needs_review"
    assert decision.reason == (
        f"unresolved_exact_address_identity_conflict:{CATALOG_DECISION_VERSION}"
    )


def test_nonblocking_match_review_does_not_override_hard_gate_decision(monkeypatch):
    pipeline = CatalogPipeline(_Database())
    pipeline.repo = _ReviewRepository(blocking=False)
    monkeypatch.setattr(
        "paloma_data.catalog_pipeline.decide_candidate",
        lambda *_args, **_kwargs: _decision("verified"),
    )

    decision = pipeline._decide_candidate(
        pipeline.db.conn,
        "candidate-1",
        [],
        [],
        mode="production",
    )

    assert decision.state == "verified"


def test_resolving_review_refreshes_evidence_then_rechecks_candidate():
    database = _ResolutionDatabase()
    pipeline = CatalogPipeline(database)
    repository = _ResolutionRepository()
    pipeline.repo = repository
    evaluations = []

    def evaluate(_, candidate_id):
        evaluations.append(candidate_id)
        return _decision("verified")

    pipeline._evaluate_candidate = evaluate

    result = pipeline.resolve_match_review(
        123,
        resolution="not_same_or_stale",
        reviewer="github:test-reviewer",
        expected_city="Oakland",
        note="Current source is a different business.",
    )

    assert evaluations == ["candidate-1", "candidate-1"]
    assert repository.resolutions == [
        (
            123,
            "not_same_or_stale",
            "github:test-reviewer",
            "Current source is a different business.",
        )
    ]
    assert database.conn.commits == 1
    assert result["candidate_state"] == "verified"
    assert result["publication_mutated"] is False


def test_accepting_review_creates_a_durable_manual_identity_link():
    database = _ResolutionDatabase()
    pipeline = CatalogPipeline(database)
    repository = _ResolutionRepository()
    pipeline.repo = repository
    pipeline._evaluate_candidate = lambda *_: _decision("verified")

    result = pipeline.resolve_match_review(
        123,
        resolution="same_place",
        reviewer="github:test-reviewer",
    )

    assert repository.resolutions == [(123, "same_place", "github:test-reviewer", None)]
    assert len(repository.links) == 1
    candidate_id, source_record_id, kwargs = repository.links[0]
    assert candidate_id == "candidate-1"
    assert source_record_id == "abc-1"
    assert kwargs["confidence"] == 0.99
    assert kwargs["method"].startswith("manual_review:123:")
    assert kwargs["metadata"]["review_id"] == 123
    assert result["candidate_state"] == "verified"


def test_review_resolution_rejects_candidate_outside_city_guardrail():
    database = _ResolutionDatabase()
    pipeline = CatalogPipeline(database)
    repository = _ResolutionRepository()
    pipeline.repo = repository
    pipeline._evaluate_candidate = lambda *_: _decision("verified")

    try:
        pipeline.resolve_match_review(
            123,
            resolution="not_same_or_stale",
            reviewer="github:test-reviewer",
            expected_city="Berkeley",
        )
    except ValueError as error:
        assert str(error) == "Candidate city 'Oakland' does not match guardrail 'Berkeley'"
    else:
        raise AssertionError("a cross-city review resolution must fail closed")

    assert repository.resolutions == []
    assert database.conn.commits == 0


def test_discovery_skips_anchor_claimed_after_batch_was_selected():
    anchor = SourceRecord(
        source="fsq",
        source_record_id="anchor-1",
        name="Example Bar",
        address="123 Main St",
        city="San Francisco",
        latitude=37.78,
        longitude=-122.42,
    )
    database = _Database()
    pipeline = CatalogPipeline(database)
    pipeline.repo = _AlreadyLinkedDiscoveryRepository(anchor)
    pipeline._candidate_for_anchor = lambda *_: (_ for _ in ()).throw(
        AssertionError("an already-linked anchor must not create a candidate")
    )

    result = pipeline.discover(city="San Francisco", limit=100)

    assert result == {
        "anchors_considered": 1,
        "anchors_already_linked": 1,
        "candidates_created": 0,
        "anchors_linked": 0,
        "sources_linked": 0,
        "match_reviews": 0,
        "decisions": {},
        "candidate_ids": [],
    }
    assert database.conn.commits == 1


def test_verification_records_unusable_place_and_continues(monkeypatch):
    anchor = SourceRecord(
        source="fsq",
        source_record_id="fsq-broken",
        name="Example Bar",
        address="123 Main St",
        city="San Francisco",
        latitude=37.78,
        longitude=-122.42,
        primary_type_slug="bar",
    )
    pipeline = CatalogPipeline(_VerificationDatabase())
    repository = _UnusableVerificationRepository(anchor)
    pipeline.repo = repository
    monkeypatch.setattr(pipeline, "_correlate", lambda *_args: (0, 0))
    monkeypatch.setattr(pipeline, "_validated_links", lambda *_args: [])
    monkeypatch.setattr(
        pipeline,
        "_decide_candidate",
        lambda *_args, **_kwargs: _decision("needs_verification"),
    )

    result = pipeline.verify_with_foursquare(
        _UnusableAPI(),
        city="San Francisco",
        limit=1,
        mode="trial",
        lease_days=45,
        candidate_ids=["candidate-1"],
    )

    assert result["considered"] == 1
    assert result["api_calls"] == 1
    assert result["api_unusable"] == 1
    assert result["api_not_found"] == 0
    assert result["failed"] == 0
    assert result["inconclusive"] == 1
    assert result["decisions"] == {"needs_verification": 1}
    assert result["results"][0]["verification"] == "inconclusive"
    assert repository.evaluations[0][0:2] == ("candidate-1", "needs_verification")


def test_verification_respects_an_explicit_empty_candidate_scope():
    pipeline = CatalogPipeline(_VerificationDatabase())
    pipeline.repo = _NoVerificationFallbackRepository()

    result = pipeline.verify_with_foursquare(
        _UnusableAPI(),
        city="San Francisco",
        limit=250,
        mode="trial",
        lease_days=45,
        candidate_ids=[],
    )

    assert result["considered"] == 0
    assert result["api_calls"] == 0
    assert result["decisions"] == {}
