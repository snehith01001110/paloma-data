from __future__ import annotations

from collections import deque
import threading

import pytest

from paloma_data.jobs import (
    PermanentJobError,
    PipelineJob,
    PipelineWorker,
    _LeaseHeartbeat,
    catalog_refresh_requests,
)


def _job(*, attempt_no: int = 1, job_type: str = "candidate_refresh") -> PipelineJob:
    return PipelineJob(
        id="11111111-1111-1111-1111-111111111111",
        message_id=42,
        job_type=job_type,
        payload={"candidate_id": "22222222-2222-2222-2222-222222222222"},
        attempt_no=attempt_no,
        max_attempts=5,
        run_ids=("33333333-3333-3333-3333-333333333333",),
    )


class _Queue:
    def __init__(self, jobs, *, failed_state="queued", queue_totals=()):
        self.jobs = deque(jobs)
        self.queue_totals = deque(queue_totals)
        self.failed_state = failed_state
        self.claims = []
        self.completions = []
        self.failures = []
        self.renewals = []
        self.renewed = threading.Event()

    def claim(self, worker_id, *, visibility_seconds, quantity):
        self.claims.append((worker_id, visibility_seconds, quantity))
        claimed = []
        while self.jobs and len(claimed) < quantity:
            claimed.append(self.jobs.popleft())
        return claimed

    def complete(self, job, worker_id, result):
        self.completions.append((job, worker_id, result))

    def fail(self, job, worker_id, **failure):
        self.failures.append((job, worker_id, failure))
        return {"job_state": self.failed_state, "next_attempt_at": None}

    def renew(self, job, worker_id, *, visibility_seconds):
        self.renewals.append((job, worker_id, visibility_seconds))
        self.renewed.set()

    def metrics(self):
        return {"queue_total": self.queue_totals.popleft() if self.queue_totals else 0}


def test_worker_drains_and_acknowledges_successful_jobs():
    queue = _Queue([_job()])
    events = []
    worker = PipelineWorker(
        queue,
        lambda job: {"candidate_id": job.payload["candidate_id"], "refreshed": True},
        worker_id="test-worker",
        poll_seconds=0,
        event_sink=events.append,
    )

    result = worker.run(drain=True)

    assert result["claimed"] == 1
    assert result["succeeded"] == 1
    assert result["drained"] is True
    assert queue.completions[0][2]["refreshed"] is True
    assert queue.failures == []
    assert [event["event"] for event in events] == ["job_started", "job_succeeded"]


def test_worker_retries_unexpected_errors_with_exponential_backoff():
    queue = _Queue([_job(attempt_no=3)], failed_state="queued")

    def fail(_):
        raise RuntimeError("temporary provider failure")

    result = PipelineWorker(
        queue,
        fail,
        worker_id="test-worker",
        poll_seconds=0,
        retry_base_seconds=30,
    ).run(drain=True)

    assert result["retried"] == 1
    assert result["unresolved_retries"] == 1
    assert queue.failures[0][2]["retryable"] is True
    assert queue.failures[0][2]["retry_delay_seconds"] == 120


def test_worker_clears_a_retry_failure_when_the_same_job_recovers():
    queue = _Queue([_job(attempt_no=1), _job(attempt_no=2)], failed_state="queued")
    attempts = iter((RuntimeError("temporary provider failure"), {"ok": True}))

    def handle(_):
        outcome = next(attempts)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    result = PipelineWorker(
        queue,
        handle,
        worker_id="test-worker",
        poll_seconds=0,
    ).run(drain=True)

    assert result["retried"] == 1
    assert result["recovered"] == 1
    assert result["unresolved_retries"] == 0
    assert result["dead"] == 0


def test_worker_dead_letters_permanent_errors_without_retrying():
    queue = _Queue([_job(job_type="unknown")], failed_state="dead")

    def fail(_):
        raise PermanentJobError("unsupported")

    result = PipelineWorker(
        queue,
        fail,
        worker_id="test-worker",
        poll_seconds=0,
    ).run(drain=True)

    assert result["dead"] == 1
    assert queue.failures[0][2]["retryable"] is False
    assert queue.failures[0][2]["error_code"] == "permanent_job_error"


def test_worker_does_not_claim_more_than_the_maximum():
    queue = _Queue([_job(), _job(), _job()])
    worker = PipelineWorker(
        queue,
        lambda _: {"ok": True},
        worker_id="test-worker",
        batch_size=10,
        poll_seconds=0,
    )

    result = worker.run(drain=True, max_jobs=2)

    assert result["claimed"] == 2
    assert queue.claims[0][2] == 2
    assert len(queue.jobs) == 1
    assert result["limit_reached"] is True
    assert result["drained"] is False


def test_drain_waits_for_delayed_messages_before_reporting_empty():
    queue = _Queue([], queue_totals=(1, 0))
    worker = PipelineWorker(
        queue,
        lambda _: {"ok": True},
        worker_id="test-worker",
        poll_seconds=0,
    )

    result = worker.run(drain=True, idle_timeout_seconds=1)

    assert len(queue.claims) == 2
    assert result["drained"] is True
    assert result["timed_out"] is False


def test_catalog_refresh_requests_are_scoped_and_deduplicable():
    requests = list(
        catalog_refresh_requests(
            ["candidate-1", "candidate-2"],
            decision_version="v6",
            max_attempts=4,
        )
    )

    assert [request.dedupe_key for request in requests] == [
        "candidate-1:v6",
        "candidate-2:v6",
    ]
    assert requests[0].payload == {"candidate_id": "candidate-1"}
    assert requests[0].max_attempts == 4


def test_worker_configuration_rejects_an_unsafe_visibility_timeout():
    with pytest.raises(ValueError, match="visibility_seconds"):
        PipelineWorker(_Queue([]), lambda _: {}, worker_id="worker", visibility_seconds=5)


def test_long_jobs_renew_the_database_and_message_lease():
    queue = _Queue([])
    heartbeat = _LeaseHeartbeat(
        queue,
        _job(),
        "test-worker",
        visibility_seconds=60,
    )
    heartbeat.interval_seconds = 0.001

    heartbeat.start()
    assert queue.renewed.wait(timeout=1)
    heartbeat.stop()
    heartbeat.raise_if_failed()

    assert queue.renewals[0][1:] == ("test-worker", 60)
