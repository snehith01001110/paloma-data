from pathlib import Path


MIGRATION = Path("supabase/migrations/20260819054949_production_pipeline_jobs.sql")


def test_pipeline_queue_stays_private_and_uses_bounded_worker_functions():
    sql = MIGRATION.read_text()

    assert "alter table pgmq.q_paloma_pipeline enable row level security" in sql
    assert "revoke usage on schema pgmq" in sql
    assert "pgmq_public" not in sql
    assert "grant execute on function ingest.claim_pipeline_jobs" in sql
    assert "grant select on ingest.pipeline_runs" in sql
    assert "grant insert" not in sql
    assert "set search_path = pg_catalog" in sql


def test_pipeline_queue_has_deduplication_leases_retries_and_retention():
    sql = MIGRATION.read_text()

    assert "pipeline_jobs_active_dedupe_idx" in sql
    assert "lease_expires_at" in sql
    assert "pgmq.set_vt" in sql
    assert "state = 'dead'" in sql
    assert "pipeline_job_attempts" in sql
    assert "paloma-pipeline-history-purge" in sql
    # Explicit timestamps avoid the pgmq 1.4 -> 1.5 integer-delay behavior change.
    assert "v_next_attempt_at::timestamptz" in sql
