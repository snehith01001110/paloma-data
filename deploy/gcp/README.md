# Managed Google Cloud worker

This directory contains the reproducible deployment path for the frequent Paloma queue worker.
GitHub Actions builds the repository image, pushes an immutable commit tag to Artifact Registry,
resolves the image digest, rotates scoped runtime secrets, deploys the digest to Cloud Run Jobs,
and performs an empty-queue health execution. Cloud Scheduler then invokes the job every five
minutes.

The deployment has three identities:

- `paloma-github-deployer` receives short-lived credentials only from the repository's
  `sync.yml` workflow on `main` during a manual dispatch. It can push images, add secret versions,
  and deploy the worker, but cannot read secret payloads.
- `paloma-pipeline-worker` can access only the two runtime secrets required by reviewed handlers.
- `paloma-pipeline-scheduler` can invoke only the deployed Cloud Run job.

No Google service-account key or database credential is stored in GitHub. The existing
main-only GitHub OIDC broker supplies the scoped `paloma_ingest` URL during deployment, and the
workflow streams it directly into Secret Manager.

## Bootstrap

Run once as a billing-enabled project owner:

```bash
GCP_PROJECT_ID=paloma-506006 GCP_REGION=us-west1 deploy/gcp/bootstrap.sh
```

The script is idempotent. It enables APIs, creates the registry, service accounts, secret
containers, and Workload Identity provider, applies least-privilege IAM, and configures non-secret
GitHub repository variables.

## Deploy

Merge the deployment wiring to `main`, then dispatch **Verified establishment catalog** with
`action=deploy-worker`. Only that main-branch manual workflow can exchange both Supabase and Google
OIDC credentials. A successful run builds and deploys the job and executes it once.

After the first deployment:

```bash
GCP_PROJECT_ID=paloma-506006 GCP_REGION=us-west1 deploy/gcp/configure-scheduler.sh
```

Configure the verified operator destination and the three production alert policies:

```bash
GCP_PROJECT_ID=paloma-506006 \
PALOMA_ALERT_EMAIL=operator@example.com \
deploy/gcp/configure-monitoring.sh
```

The first invocation sends a Google Cloud verification code to the address. Until the recipient
verifies that code, the channel exists but cannot deliver incidents. The policies alert on failed
Cloud Run executions, a missing completion for 15 minutes, and structured worker telemetry showing
dead work or a queue age above 15 minutes.

Keep GitHub's scheduled worker enabled during the initial overlap. The shared pgmq leases and
deduplication keys make concurrent claims safe. After two clean weekly cycles, remove the scheduled
GitHub drain while retaining its manual `queue-work` recovery action.

The job claims one message at a time and processes at most 40 messages per invocation. This bounds
the normal execution near the five-minute schedule at measured catalog-refresh latency. Delayed
retries remain in pgmq for the next invocation, while unresolved retries or dead jobs make the
Cloud Run execution fail visibly.
