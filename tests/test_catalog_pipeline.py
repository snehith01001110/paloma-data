from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from paloma_data.catalog import CATALOG_DECISION_VERSION, CatalogDecision
from paloma_data.catalog_pipeline import CatalogPipeline


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

    def pending_match_review_candidate_id(self, _, review_id):
        assert review_id == 123
        return "candidate-1"

    def resolve_match_review(self, _, review_id, *, resolution):
        self.resolutions.append((review_id, resolution))
        return "candidate-1"


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
    pipeline._evaluate_candidate = lambda *_: _decision("needs_verification")

    result = pipeline.publish(limit=10)

    assert repository.requested_version == CATALOG_DECISION_VERSION
    assert repository.materialized == []
    assert result == {
        "considered": 1,
        "published": 0,
        "skipped": 1,
        "expired_withdrawn": 0,
    }


def test_publish_materializes_only_after_current_decision_passes():
    pipeline = CatalogPipeline(_Database())
    repository = _PublicationRepository()
    pipeline.repo = repository
    pipeline._evaluate_candidate = lambda *_: _decision("verified")

    result = pipeline.publish(limit=10)

    assert repository.requested_version == CATALOG_DECISION_VERSION
    assert repository.materialized == ["candidate-1"]
    assert result["published"] == 1


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

    result = pipeline.resolve_match_review(123, resolution="not_same_or_stale")

    assert evaluations == ["candidate-1", "candidate-1"]
    assert repository.resolutions == [(123, "not_same_or_stale")]
    assert database.conn.commits == 1
    assert result["candidate_state"] == "verified"
    assert result["publication_mutated"] is False
