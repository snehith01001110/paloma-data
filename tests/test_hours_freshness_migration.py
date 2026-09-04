from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/20260904063044_add_hours_freshness_contract.sql"
).read_text()


def test_hours_freshness_contract_has_metadata_constraint_and_private_queue() -> None:
    for column in (
        "hours_verified_at",
        "hours_expires_at",
        "hours_source_url",
        "hours_source_kind",
    ):
        assert column in MIGRATION
    assert "establishments_hours_freshness_check" in MIGRATION
    assert "review.hours_verification_queue" in MIGRATION
    assert "with (security_invoker = true)" in MIGRATION
    assert "grant select on review.hours_verification_queue to paloma_ingest" in MIGRATION
