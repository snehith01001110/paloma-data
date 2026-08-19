from pathlib import Path


BOOTSTRAP = Path("deploy/gcp/bootstrap.sh").read_text()
DEPLOY = Path("deploy/gcp/deploy-worker.sh").read_text()
SCHEDULER = Path("deploy/gcp/configure-scheduler.sh").read_text()
MONITORING = Path("deploy/gcp/configure-monitoring.sh").read_text()
WORKFLOW = Path(".github/workflows/sync.yml").read_text()
WORKER_SETUP = Path(".github/actions/setup-data-worker/action.yml").read_text()


def test_github_deployment_uses_keyless_main_workflow_identity():
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in WORKFLOW
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in WORKER_SETUP
    assert "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093" in WORKFLOW
    assert "workload_identity_provider: ${{ vars.GCP_WORKLOAD_IDENTITY_PROVIDER }}" in WORKFLOW
    assert "credentials_json" not in WORKFLOW
    assert "assertion.ref == 'refs/heads/main'" in BOOTSTRAP
    assert "assertion.workflow_ref == '${PALOMA_GITHUB_WORKFLOW_REF}'" in BOOTSTRAP
    assert "assertion.event_name == 'workflow_dispatch'" in BOOTSTRAP


def test_worker_deploys_an_immutable_digest_with_scoped_secrets():
    assert "--immutable-tags" in BOOTSTRAP
    assert '--image="${image_repository}@${image_digest}"' in DEPLOY
    assert "SUPABASE_DB_URL=${GCP_DB_SECRET}:${db_secret_version}" in DEPLOY
    assert "YELP_API_KEY=${GCP_YELP_SECRET}:${yelp_secret_version}" in DEPLOY
    assert "--max-retries=0" in DEPLOY
    assert "--parallelism=1" in DEPLOY
    assert "--batch-size,1" in DEPLOY
    assert "--max-jobs,5000" in DEPLOY


def test_scheduler_uses_a_dedicated_authenticated_invoker():
    assert "roles/run.invoker" in SCHEDULER
    assert "--oauth-service-account-email" in SCHEDULER
    assert "https://run.googleapis.com/v2/projects/" in SCHEDULER
    assert '--schedule="0 3,15 * * *"' in SCHEDULER


def test_monitoring_covers_execution_failure_absence_and_queue_health():
    monitoring_directory = Path("deploy/gcp/monitoring")
    policy_text = "\n".join(
        path.read_text() for path in sorted(monitoring_directory.glob("*.json"))
    )

    assert "sendVerificationCode" in MONITORING
    assert "notificationChannels" in MONITORING
    assert "job/completed_execution_count" in policy_text
    assert '"conditionAbsent"' in policy_text
    assert '"conditionMatchedLog"' in policy_text
    assert "jsonPayload.queue_metrics.jobs_dead_24h>0" in policy_text
    assert "jsonPayload.queue_metrics.oldest_queued_seconds>129600" in policy_text
