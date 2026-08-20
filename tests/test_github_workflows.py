from pathlib import Path


EXPANSION = Path(".github/workflows/expansion.yml").read_text()
SYNC = Path(".github/workflows/sync.yml").read_text()
CREDENTIALS = Path("supabase/functions/paloma-data-credentials/index.ts").read_text()


def test_expansion_is_manual_environment_protected_and_database_gated():
    assert "workflow_dispatch:" in EXPANSION
    assert "schedule:" not in EXPANSION
    assert "environment: catalog-expansion" in EXPANSION
    assert "paloma-data expansion-status --release-id" in EXPANSION
    assert "--require-ready" in EXPANSION
    assert "paloma-data catalog-publish" in EXPANSION
    assert '--release-id "$RELEASE_ID"' in EXPANSION
    assert "observe-field" in EXPANSION
    assert "RECORD_FIELD_OBSERVATION" in EXPANSION
    assert "paloma-data catalog-observe-field" in EXPANSION
    assert "observe-manifest" in EXPANSION
    assert "RECORD_FIELD_MANIFEST" in EXPANSION
    assert "paloma-data catalog-observe-manifest" in EXPANSION


def test_protected_review_resolution_supports_existing_catalog_cities():
    assert 'case "$ACTION" in' in EXPANSION
    assert "resolve-review)" in EXPANSION
    assert 'os.environ["PALOMA_CITIES"].split(",")' in EXPANSION
    assert "outside the configured maintenance region" in EXPANSION


def test_maintenance_workflow_has_no_new_publication_or_legacy_cutover_action():
    assert "catalog-publish" not in SYNC
    assert "catalog-cutover" not in SYNC
    assert "PALOMA_CATALOG_AUTO_PUBLISH" not in SYNC


def test_maintenance_workflow_checks_live_details_runtime_daily():
    assert "paloma-data live-details-health --require-healthy" in SYNC
    assert "if: github.event_name == 'schedule'" in SYNC


def test_scoped_oidc_credential_allows_only_the_two_reviewed_workflows():
    assert ".github/workflows/sync.yml@refs/heads/main" in CREDENTIALS
    assert ".github/workflows/expansion.yml@refs/heads/main" in CREDENTIALS
    assert 'new Set(["workflow_dispatch"])' in CREDENTIALS
