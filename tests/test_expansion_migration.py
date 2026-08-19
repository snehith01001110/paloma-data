from pathlib import Path


MIGRATION = Path("supabase/migrations/20260819231355_gate_catalog_expansion_v2.sql").read_text()


def test_new_publication_is_enforced_at_the_database_write_boundary():
    assert "before insert on public.establishments" in MIGRATION
    assert "new catalog publication requires an armed expansion release" in MIGRATION
    assert "catalog_expansion_status" in MIGRATION
    assert "release_capacity_exhausted" in MIGRATION
    assert "city % is outside expansion release %" in MIGRATION


def test_existing_publications_bypass_the_new_identity_gate_for_refreshes():
    assert "if exists (select 1 from public.establishments where id = new.id)" in MIGRATION
    assert "INSERT ... ON CONFLICT also fires BEFORE INSERT" in MIGRATION


def test_release_events_are_private_append_only_and_least_privilege():
    assert "catalog_expansion_release_events_append_only" in MIGRATION
    assert "catalog expansion attribution is immutable" in MIGRATION
    assert "enable row level security" in MIGRATION
    assert (
        "grant select on governance.catalog_expansion_release_events to paloma_ingest" in MIGRATION
    )
    assert "grant insert" not in MIGRATION
    assert "security definer\nset search_path = ''" in MIGRATION
