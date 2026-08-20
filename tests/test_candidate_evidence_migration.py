from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/20260820032432_harden_ingest_and_candidate_field_evidence.sql"
).read_text()


def test_legacy_ingest_tables_receive_worker_scoped_rls():
    for table in (
        "source_records",
        "establishment_sources",
        "ingestion_runs",
        "establishment_review_queue",
        "establishment_field_evidence",
    ):
        assert f"alter table ingest.{table} enable row level security" in MIGRATION
        assert f"paloma_ingest_manage_{table}" in MIGRATION
    assert "from public, anon, authenticated, service_role" in MIGRATION


def test_observation_ledger_supports_exactly_one_private_or_public_scope():
    assert "add column candidate_id uuid" in MIGRATION
    assert "num_nonnulls(establishment_id, candidate_id) = 1" in MIGRATION
    assert "field_observations_candidate_current_idx" in MIGRATION
    assert "hours_schedules_exactly_one_entity_check" in MIGRATION
    assert "new.id, new.establishment_id, new.candidate_id" in MIGRATION
