from pathlib import Path


MIGRATION = Path("supabase/migrations/20260819232256_harden_expansion_governance.sql").read_text()


def test_contribution_terms_are_not_left_with_a_policy_but_rls_disabled():
    assert (
        "alter table governance.contribution_terms_versions enable row level security" in MIGRATION
    )


def test_governance_foreign_keys_have_covering_indexes():
    for index in (
        "catalog_expansion_release_events_terms_idx",
        "contribution_reviews_observation_idx",
        "field_observations_source_run_idx",
        "hours_schedules_establishment_idx",
        "field_conflicts_decision_idx",
        "establishment_contributions_terms_idx",
        "merchant_claim_requests_terms_idx",
    ):
        assert f"create index {index}" in MIGRATION


def test_completed_destructive_cutover_capability_is_removed():
    assert "drop function if exists ingest.reset_legacy_public_catalog" in MIGRATION
    assert "drop table if exists ingest.catalog_cutover_control" in MIGRATION
