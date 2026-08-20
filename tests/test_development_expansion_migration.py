from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/20260820055305_allow_single_development_refresh_week.sql"
).read_text()


def test_development_release_events_allow_one_healthy_refresh_week():
    assert "minimum_healthy_refresh_weeks between 1 and 8" in MIGRATION
    assert "catalog_expansion_release_events_approval_policy_check" in MIGRATION


def test_other_release_authorization_controls_remain_required():
    for control in (
        "maximum_new_publications between 1 and 500",
        "baseline_publications >= 0",
        "terms_version is not null",
        "coverage_snapshot <> '{}'::jsonb",
        "coverage_accepted_by is not null",
        "expires_at is not null",
    ):
        assert control in MIGRATION
