# Cloud Run worker deployment

This directory preserves the provider-neutral reviewed job shape. The active, reproducible Google
Cloud deployment now lives in `deploy/gcp/`; do not deploy this placeholder example unchanged.

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
Then add a Cloud Scheduler trigger from the job's **Triggers** tab, every 12 hours.
The scheduler service account needs only permission to invoke this job.

Cloud Run task retries are set to zero because the application queue owns bounded per-job retries;
stacking platform retries on top would make incident timing and provider usage harder to reason
about. Multiple task executions are still safe because pgmq visibility leases and active-job
deduplication coordinate them.

GitHub queues twice-weekly regulatory work, monthly open-source work, and a daily safety sweep.
Cloud Run owns scheduled draining;
retain GitHub's manual `queue-work` action for recovery.
