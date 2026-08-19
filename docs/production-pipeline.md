# Production pipeline operations

The catalog uses two execution planes with one database control plane:

```text
GitHub Actions                         Managed container job
CI, deploy, bulk source snapshots     Frequent queue draining and retries
               \                       /
                ingest.pipeline_* + pgmq
                         |
                reviewed Python handlers
                         |
          private evidence -> verified catalog
```

GitHub Actions remains the initial worker runtime and the manual recovery path. Before adding
more Bay Area jurisdictions, run the same container as a managed job on a short schedule. No
Kubernetes cluster or second implementation is required.

Both the GitHub runner and container install from the committed `uv.lock`; dependency resolution
does not drift between scheduled runs.

## Safety contract

- `pgmq.q_paloma_pipeline` and its archive are private and have RLS enabled with no client
  policies. They are not exposed through `pgmq_public` or the Data API.
- Workers connect as `paloma_ingest`, never `postgres` or `service_role`.
- Only fixed job handlers are dispatchable. Queue payloads cannot execute shell commands or name
  arbitrary Python functions.
- `candidate_refresh` can refresh, republish, or suppress an establishment that was already
  materialized. It cannot publish a new candidate. New publication still requires the explicit
  `catalog-publish --confirm PUBLISH_VERIFIED` gate.
- Each job has an active deduplication key, a visibility lease, bounded attempts, immutable
  attempt records, exponential retry, and a terminal dead-letter state.
- Provider network requests do not hold an open database transaction. Provider response-retention
  rules remain unchanged.

## Apply and verify

Apply `supabase/migrations/20260819054949_production_pipeline_jobs.sql` before starting a worker.
Then run:

```bash
paloma-data pipeline-status
paloma-data pipeline-enqueue-catalog --city "San Francisco" --limit 5000
paloma-data pipeline-worker --drain --max-jobs 5000 --fail-on-error
paloma-data pipeline-status
paloma-data catalog-status
```

The default refresh scope is published establishments only. Add `--include-suppressed` for an
explicit recovery/republication pass. The first command should report an empty queue. The completed
logical run should have no dead jobs, and the public catalog should still satisfy all verification
invariants.

To reevaluate private candidates without authorizing publication:

```bash
paloma-data pipeline-enqueue-catalog --include-unpublished --limit 50000
paloma-data pipeline-worker --drain --max-jobs 50000 --fail-on-error
```

## Managed worker contract

Build the repository `Dockerfile` once and run that immutable image with:

```text
pipeline-worker --drain --max-jobs 5000 --batch-size 1 --idle-timeout-seconds 120
```

`deploy/cloud-run/worker-job.yaml.example` is the reviewed Cloud Run starting point. It deliberately
contains project, image-digest, service-account, and secret placeholders because selecting a
billable cloud project is an owner decision.

Required secret:

- `SUPABASE_DB_URL`: a scoped `paloma_ingest` connection, preferably through the appropriate
  Supavisor endpoint for the runtime network. Store it in the compute provider's secret manager.

Optional secrets are needed only by enabled handlers:

- `YELP_API_KEY` for `provider_links_sync`;
- FSQ bulk/API settings for separate snapshot or licensed-verification jobs.

Start with one scheduled task and claim one job at a time. Increase to two to four tasks only after
measuring queue age, database connection use, provider quotas, and duplicate-review rates. The
database lease and deduplication rules make horizontal workers safe. Keep the claim batch at one
unless the worker implementation also processes or renews every claimed job concurrently.

The managed scheduler invokes the worker at 03:00 and 15:00 UTC. A drain invocation exits when the
queue is empty, so the runtime scales to zero. The queue remains durable between invocations;
on-demand runs are available for urgent corrections. Increase frequency only if measured queue
latency or product requirements justify the added executions.

Bulk ABC, DataSF, FSQ OS, and neighborhood snapshots remain separate container jobs because they
have snapshot-level completeness semantics. Do not split a complete source snapshot into
independent record jobs unless the finalizer can prove every partition completed before absence
reconciliation.

## Scheduling during cutover

While catalog expansion is paused, the checked-in GitHub workflow refreshes regulatory evidence
twice weekly, refreshes the larger durable open-source snapshots monthly, and performs this sequence:

1. Refresh specifically licensed verification evidence only for the existing materialized cohort
   when configured.
2. Stage ABC and DataSF twice weekly; stage FSQ OS when configured, Overture, Wikidata, and civic
   boundaries monthly without discovering or publishing new establishments.
3. Append rights-approved observations, resolve the existing cohort, and report field coverage.
4. Queue published/suppressed cohort refreshes and a bounded provider-link sync.
5. Leave scheduled publication disabled. A separate daily action queues the expiry sweep, and the
   managed worker drains all queued work.

Keep that path enabled until the managed worker has completed at least two clean weekly cycles.
Afterward, remove only the scheduled queue-drain step from GitHub Actions. Retain its manual
`queue-work` action as disaster recovery.

## Alerts and operating thresholds

Alert when any of these conditions is true:

- `jobs_dead_24h > 0`;
- the oldest queued job is older than 36 hours, or three full worker schedules
  interval for a bulk job;
- a logical run finishes `partial` or `failed`;
- a running lease remains expired after two worker invocations;
- the verified publication count falls unexpectedly or an invariant-risk counter becomes nonzero.

Use `paloma-data pipeline-status` for queue/run state and `paloma-data catalog-status` for catalog
truth and completeness. Retry a dead job by enqueueing a new reviewed request; do not edit queue
tables or job states directly.

## Capacity model

Capacity planning targets—not current catalog estimates—are:

- 25,000 published establishments;
- 250,000 source identities;
- 10 million field claims.

Scale workers from measured queue age. Scale Postgres indexes, claim batch size, and retention from
measured row counts. Archived queue messages are retained for 30 days; completed logical runs,
jobs, and attempts are retained for 180 days by the database maintenance job.
