# Cloud Run worker deployment

This directory is an optional concrete deployment of the portable worker contract. Replace every
uppercase placeholder in `worker-job.yaml.example`; do not deploy the example unchanged.

Requirements:

- an existing Google Cloud project and billing account selected by the owner;
- an Artifact Registry image pinned by digest;
- a dedicated runtime service account;
- a Secret Manager secret containing only the scoped `paloma_ingest` connection URL;
- network access to the selected Supabase connection endpoint.

After review, copy the example outside the repository or to a non-example environment file and
apply it with:

```bash
gcloud run jobs replace worker-job.yaml --region REGION
gcloud run jobs execute paloma-pipeline-worker --region REGION --wait
```

Inspect `paloma-data pipeline-status` and the Cloud Run execution logs before adding a schedule.
Then add a Cloud Scheduler trigger from the job's **Triggers** tab, initially every five minutes.
The scheduler service account needs only permission to invoke this job.

Cloud Run task retries are set to zero because the application queue owns bounded per-job retries;
stacking platform retries on top would make incident timing and provider usage harder to reason
about. Multiple task executions are still safe because pgmq visibility leases and active-job
deduplication coordinate them.

Keep the GitHub Actions weekly worker enabled for two clean cycles. Once Cloud Run has drained the
same queue reliably, remove only the scheduled `pipeline-worker` invocation from GitHub Actions;
retain its manual `queue-work` action for recovery.
