from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
import socket
import threading
import time
from typing import Any, Callable, Iterable
from uuid import UUID, uuid4

import httpx
import psycopg

from paloma_data.adapters.yelp import YelpPlacesAPI
from paloma_data.catalog_pipeline import CatalogPipeline
from paloma_data.catalog_repository import CatalogRepository
from paloma_data.config import Settings
from paloma_data.db import Database
from paloma_data.provider_links import ProviderLinkSync


@dataclass(frozen=True, slots=True)
class PipelineJob:
    id: str
    message_id: int
    job_type: str
    payload: dict[str, Any]
    attempt_no: int
    max_attempts: int
    run_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JobRequest:
    job_type: str
    dedupe_key: str
    payload: dict[str, Any]
    max_attempts: int = 5
    delay_seconds: int = 0


class PermanentJobError(RuntimeError):
    """A job cannot succeed without changing its payload or implementation."""


class PipelineQueue:
    """Small direct-Postgres client for the private pgmq control functions."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create_run(
        self,
        run_type: str,
        *,
        requested_by: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        with self.db.connection() as conn:
            row = conn.execute(
                """
                select ingest.create_pipeline_run(%s, %s, %s::jsonb)::text as run_id
                """,
                (run_type, requested_by, _json(metadata or {})),
            ).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("Pipeline run creation returned no identifier")
        return str(row["run_id"])

    def enqueue_many(
        self,
        run_id: str,
        jobs: Iterable[JobRequest],
        *,
        checkpoint_every: int = 500,
    ) -> dict[str, int]:
        counts = {"requested": 0, "created": 0, "deduplicated": 0}
        with self.db.connection() as conn:
            for job in jobs:
                row = conn.execute(
                    """
                    select job_id::text, created, job_state
                    from ingest.enqueue_pipeline_job(
                      %s::uuid, %s, %s, %s::jsonb, %s, %s
                    )
                    """,
                    (
                        run_id,
                        job.job_type,
                        job.dedupe_key,
                        _json(job.payload),
                        job.max_attempts,
                        job.delay_seconds,
                    ),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Pipeline enqueue returned no job")
                counts["requested"] += 1
                counts["created" if row["created"] else "deduplicated"] += 1
                if counts["requested"] % checkpoint_every == 0:
                    conn.commit()
            conn.commit()
        return counts

    def claim(
        self,
        worker_id: str,
        *,
        visibility_seconds: int,
        quantity: int,
    ) -> list[PipelineJob]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                select job_id::text, message_id, job_type, payload, attempt_no,
                       max_attempts, run_ids
                from ingest.claim_pipeline_jobs(%s, %s, %s)
                """,
                (worker_id, visibility_seconds, quantity),
            ).fetchall()
            conn.commit()
        return [
            PipelineJob(
                id=str(row["job_id"]),
                message_id=int(row["message_id"]),
                job_type=str(row["job_type"]),
                payload=dict(row["payload"] or {}),
                attempt_no=int(row["attempt_no"]),
                max_attempts=int(row["max_attempts"]),
                run_ids=tuple(str(value) for value in (row["run_ids"] or ())),
            )
            for row in rows
        ]

    def complete(self, job: PipelineJob, worker_id: str, result: dict[str, Any]) -> None:
        with self.db.connection() as conn:
            row = conn.execute(
                """
                select ingest.complete_pipeline_job(
                  %s::uuid, %s, %s, %s::jsonb
                ) as completed
                """,
                (job.id, job.message_id, worker_id, _json(result)),
            ).fetchone()
            if row is None or not row["completed"]:
                raise RuntimeError(f"Pipeline job {job.id} was not acknowledged")
            conn.commit()

    def fail(
        self,
        job: PipelineJob,
        worker_id: str,
        *,
        error_code: str,
        error_summary: str,
        retryable: bool,
        retry_delay_seconds: int,
    ) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
                """
                select job_state, next_attempt_at
                from ingest.fail_pipeline_job(
                  %s::uuid, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    job.id,
                    job.message_id,
                    worker_id,
                    error_code,
                    error_summary,
                    retryable,
                    retry_delay_seconds,
                ),
            ).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError(f"Pipeline job {job.id} failure was not recorded")
        return dict(row)

    def renew(
        self,
        job: PipelineJob,
        worker_id: str,
        *,
        visibility_seconds: int,
    ) -> None:
        with self.db.connection() as conn:
            row = conn.execute(
                """
                select ingest.renew_pipeline_job_lease(
                  %s::uuid, %s, %s, %s
                ) as renewed
                """,
                (job.id, job.message_id, worker_id, visibility_seconds),
            ).fetchone()
            if row is None or not row["renewed"]:
                raise RuntimeError(f"Pipeline job {job.id} lease was not renewed")
            conn.commit()

    def status(self, *, recent_runs: int = 20) -> dict[str, Any]:
        with self.db.connection() as conn:
            metrics = conn.execute(
                "select * from ingest.pipeline_queue_metrics()"
            ).fetchone()
            runs = conn.execute(
                """
                select id::text, run_type, requested_by, state, metadata,
                       job_count, succeeded_count, dead_count,
                       created_at, started_at, finished_at, updated_at
                from ingest.pipeline_runs
                order by created_at desc
                limit %s
                """,
                (recent_runs,),
            ).fetchall()
            dead_jobs = conn.execute(
                """
                select id::text, job_type, dedupe_key, attempt_count, max_attempts,
                       error_code, error_summary, finished_at
                from ingest.pipeline_jobs
                where state = 'dead'
                order by finished_at desc
                limit 20
                """
            ).fetchall()
        return {
            "metrics": dict(metrics) if metrics else {},
            "recent_runs": [dict(row) for row in runs],
            "recent_dead_jobs": [dict(row) for row in dead_jobs],
        }

    def metrics(self) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("select * from ingest.pipeline_queue_metrics()").fetchone()
        return dict(row) if row else {}


class PipelineJobHandler:
    """Dispatch reviewed job types; arbitrary CLI execution is deliberately impossible."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    def handle(self, job: PipelineJob) -> dict[str, Any]:
        if job.job_type == "candidate_refresh":
            candidate_id = _required_uuid(job.payload, "candidate_id")
            return CatalogPipeline(self.db).refresh_candidate(candidate_id)

        if job.job_type == "catalog_sweep":
            with self.db.connection() as conn:
                withdrawn = CatalogRepository(self.db).withdraw_expired(conn)
                conn.commit()
            return {"expired_withdrawn": withdrawn}

        if job.job_type == "provider_links_sync":
            if not self.settings.yelp_api_key:
                raise PermanentJobError("YELP_API_KEY is required for provider_links_sync")
            city = _optional_text(job.payload, "city")
            limit = _bounded_integer(job.payload, "limit", minimum=1, maximum=500)
            with YelpPlacesAPI(self.settings.yelp_api_key) as api:
                return ProviderLinkSync(self.db).run(api, city=city, limit=limit)

        raise PermanentJobError(f"Unsupported pipeline job type: {job.job_type}")


class PipelineWorker:
    def __init__(
        self,
        queue: PipelineQueue,
        handler: PipelineJobHandler | Callable[[PipelineJob], dict[str, Any]],
        *,
        worker_id: str,
        visibility_seconds: int = 900,
        batch_size: int = 1,
        poll_seconds: float = 2.0,
        retry_base_seconds: int = 30,
        retry_max_seconds: int = 1800,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not 30 <= visibility_seconds <= 7200:
            raise ValueError("visibility_seconds must be between 30 and 7200")
        if not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be between 1 and 100")
        if poll_seconds < 0:
            raise ValueError("poll_seconds cannot be negative")
        self.queue = queue
        self.handler = handler
        self.worker_id = worker_id
        self.visibility_seconds = visibility_seconds
        self.batch_size = batch_size
        self.poll_seconds = poll_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.event_sink = event_sink or _emit_event

    def run(
        self,
        *,
        drain: bool,
        max_jobs: int | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        counts = {
            "claimed": 0,
            "succeeded": 0,
            "retried": 0,
            "recovered": 0,
            "dead": 0,
        }
        pending_retries: set[str] = set()
        queue_drained = False
        timed_out = False
        idle_since = time.monotonic()
        while max_jobs is None or counts["claimed"] < max_jobs:
            quantity = self.batch_size
            if max_jobs is not None:
                quantity = min(quantity, max_jobs - counts["claimed"])
            jobs = self.queue.claim(
                self.worker_id,
                visibility_seconds=self.visibility_seconds,
                quantity=quantity,
            )
            if not jobs:
                if drain:
                    if idle_timeout_seconds is None:
                        queue_drained = True
                        break
                    if int(self.queue.metrics().get("queue_total") or 0) == 0:
                        queue_drained = True
                        break
                if (
                    idle_timeout_seconds is not None
                    and time.monotonic() - idle_since >= idle_timeout_seconds
                ):
                    timed_out = True
                    break
                time.sleep(self.poll_seconds)
                continue

            idle_since = time.monotonic()
            for job in jobs:
                counts["claimed"] += 1
                self._emit("job_started", job)
                try:
                    result = self._handle_with_heartbeat(job)
                    self.queue.complete(job, self.worker_id, result)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    retryable = _is_retryable(exc)
                    failure = self.queue.fail(
                        job,
                        self.worker_id,
                        error_code=_error_code(exc),
                        error_summary=_safe_error_summary(exc),
                        retryable=retryable,
                        retry_delay_seconds=self._retry_delay(job.attempt_no),
                    )
                    if failure["job_state"] == "queued":
                        counts["retried"] += 1
                        pending_retries.add(job.id)
                    else:
                        counts["dead"] += 1
                        pending_retries.discard(job.id)
                    self._emit(
                        "job_failed",
                        job,
                        state=failure["job_state"],
                        error_code=_error_code(exc),
                        error_summary=_safe_error_summary(exc),
                    )
                else:
                    counts["succeeded"] += 1
                    if job.id in pending_retries:
                        counts["recovered"] += 1
                        pending_retries.remove(job.id)
                    self._emit("job_succeeded", job)
                if max_jobs is not None and counts["claimed"] >= max_jobs:
                    break
        result = {
            **counts,
            "worker_id": self.worker_id,
            "drained": queue_drained,
            "timed_out": timed_out,
            "limit_reached": max_jobs is not None and counts["claimed"] >= max_jobs,
            "unresolved_retries": len(pending_retries),
            "queue_metrics": self.queue.metrics(),
        }
        self.event_sink({"event": "worker_finished", **result})
        return result

    def _handle(self, job: PipelineJob) -> dict[str, Any]:
        if hasattr(self.handler, "handle"):
            return self.handler.handle(job)  # type: ignore[union-attr]
        return self.handler(job)  # type: ignore[operator]

    def _handle_with_heartbeat(self, job: PipelineJob) -> dict[str, Any]:
        heartbeat = _LeaseHeartbeat(
            self.queue,
            job,
            self.worker_id,
            visibility_seconds=self.visibility_seconds,
        )
        heartbeat.start()
        try:
            result = self._handle(job)
        finally:
            heartbeat.stop()
        heartbeat.raise_if_failed()
        return result

    def _retry_delay(self, attempt_no: int) -> int:
        return min(self.retry_max_seconds, self.retry_base_seconds * (2 ** (attempt_no - 1)))

    def _emit(self, event: str, job: PipelineJob, **values: Any) -> None:
        self.event_sink(
            {
                "event": event,
                "job_id": job.id,
                "job_type": job.job_type,
                "attempt_no": job.attempt_no,
                "worker_id": self.worker_id,
                **values,
            }
        )


def catalog_refresh_requests(
    candidate_ids: Iterable[str],
    *,
    decision_version: str,
    max_attempts: int,
) -> Iterable[JobRequest]:
    for candidate_id in candidate_ids:
        yield JobRequest(
            job_type="candidate_refresh",
            dedupe_key=f"{candidate_id}:{decision_version}",
            payload={"candidate_id": candidate_id},
            max_attempts=max_attempts,
        )


def default_requester() -> str:
    configured = os.getenv("PALOMA_PIPELINE_REQUESTER")
    if configured:
        return configured[:200]
    github_run = os.getenv("GITHUB_RUN_ID")
    return f"github_actions:{github_run}" if github_run else "paloma_data_cli"


def default_worker_id() -> str:
    configured = os.getenv("PALOMA_WORKER_ID")
    if configured:
        return configured[:200]
    return f"{socket.gethostname()}:{os.getpid()}:{str(uuid4())[:8]}"[:200]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _LeaseHeartbeat:
    def __init__(
        self,
        queue: PipelineQueue,
        job: PipelineJob,
        worker_id: str,
        *,
        visibility_seconds: int,
    ) -> None:
        self.queue = queue
        self.job = job
        self.worker_id = worker_id
        self.visibility_seconds = visibility_seconds
        self.interval_seconds = max(10.0, min(60.0, visibility_seconds / 3))
        self._stop = threading.Event()
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"pipeline-heartbeat-{job.id[:8]}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("Pipeline job lease heartbeat failed") from self._error

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.queue.renew(
                    self.job,
                    self.worker_id,
                    visibility_seconds=self.visibility_seconds,
                )
            except Exception as exc:
                self._error = exc
                return


def _required_uuid(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise PermanentJobError(f"{key} must be a UUID string")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise PermanentJobError(f"{key} must be a UUID string") from exc


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise PermanentJobError(f"{key} must be a non-empty string no longer than 200 chars")
    return value.strip()


def _bounded_integer(
    payload: dict[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PermanentJobError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise PermanentJobError(f"{key} must be between {minimum} and {maximum}")
    return value


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (PermanentJobError, ValueError, TypeError, KeyError)):
        return False
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            psycopg.OperationalError,
            psycopg.errors.DeadlockDetected,
            psycopg.errors.SerializationFailure,
        ),
    ):
        return True
    return True


def _error_code(exc: Exception) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return name[:100] or "worker_error"


def _safe_error_summary(exc: Exception) -> str:
    summary = f"{type(exc).__name__}: {exc}"
    summary = re.sub(r"postgres(?:ql)?://[^\s@]+@", "postgresql://[redacted]@", summary)
    return summary[:2000] or type(exc).__name__


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _emit_event(event: dict[str, Any]) -> None:
    print(_json(event), flush=True)
