from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/20260820035934_restore_live_details_source_eligibility.sql"
).read_text()


def test_runtime_source_record_access_is_column_scoped():
    assert "revoke select on ingest.source_records from paloma_runtime" in MIGRATION
    assert "grant select (" in MIGRATION
    for column in (
        "source",
        "source_record_id",
        "retired_at",
        "source_status",
        "consumer_facing",
        "public_access",
        "quality_flags",
    ):
        assert column in MIGRATION
    for forbidden_column in ("provider_payload", "hours", "price_level", "website_url"):
        assert forbidden_column not in MIGRATION


def test_runtime_policy_is_fail_closed_to_live_details_eligible_foursquare_rows():
    assert "paloma_runtime_read_eligible_fsq_source_records" in MIGRATION
    assert "source = 'fsq'" in MIGRATION
    assert "retired_at is null" in MIGRATION
    assert "source_status = 'open'" in MIGRATION
    assert "consumer_facing" in MIGRATION
    assert "public_access = 'walk_in'" in MIGRATION
    assert "not (quality_flags && array[" in MIGRATION
