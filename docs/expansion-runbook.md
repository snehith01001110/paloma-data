# Bay Area expansion runbook

Expansion is intentionally separate from recurring catalog maintenance. The target definition and
bounded pilot batches live in `src/paloma_data/data/bay_area_expansion_v1.json`. The manifest covers
the nine counties and 101 cities and towns in the ABAG region; each checked-in release is capped at
25 new public establishments.

The incorporated-jurisdiction list is not the final definition of Bay Area coverage. County FIPS
codes are retained so later releases can use reviewed county boundaries for unincorporated areas;
do not approximate those places with mailing-city strings.

## Readiness

Run this before spending provider calls or approving a release:

```bash
paloma-data expansion-status --release-id east-bay-pilot-v1
paloma-data catalog-status
paloma-data pipeline-status
```

`ready` remains false until all of these are true:

- the latest event for the release is `approved`, matches the checked-in manifest hash, is effective,
  and has not expired;
- the event records explicit coverage acceptance and references active contribution terms;
- ABC and DataSF snapshots are no more than seven days old, and FSQ OS, Overture, and Wikidata are
  no more than 45 days old;
- the published cohort has no expired verification, stale decision version, pending field conflict,
  or pending exact-address identity conflict;
- there are no recent dead jobs or failed/partial logical runs;
- full catalog refreshes have succeeded in at least two UTC calendar weeks during the prior 21 days,
  with the latest no more than 96 hours old; and
- the release still has capacity.

## Owner authorization

Authorization is a governance decision, not a worker capability. `paloma_ingest` has read-only
access to the event table. An owner uses the Supabase SQL editor or an equivalent `postgres`
connection to insert one immutable event after reviewing the terms and the exact coverage snapshot.
Never copy the example placeholders unchanged.

```sql
insert into governance.catalog_expansion_release_events (
  release_id, event_type, manifest_sha256, scope_cities,
  maximum_new_publications, baseline_publications,
  required_source_freshness_days, decision_version,
  minimum_healthy_refresh_weeks, refresh_history_days,
  maximum_latest_refresh_age_hours, failed_run_lookback_days,
  terms_version, coverage_snapshot, coverage_accepted_by,
  coverage_accepted_at, actor, reason, effective_at, expires_at
)
select
  'east-bay-pilot-v1', 'approved', '<current expansion-status manifest_sha256>',
  array['Berkeley','Oakland'], 25,
  count(*) filter (where publication_state = 'published' and status = 'open'),
  '{"ca_abc":7,"datasf":7,"fsq":45,"overture":45,"wikidata":45}'::jsonb,
  'v7', 2, 21, 96, 14,
  '<active terms version>', '<reviewed coverage snapshot>'::jsonb,
  '<coverage reviewer>', now(), '<approver>', '<approval rationale>',
  now(), now() + interval '14 days'
from public.establishments;
```

The insert fails if its baseline differs from the live catalog count. Changing a manifest, scope,
health policy, cap, or terms version requires a new approval event. Revoke without editing history:

```sql
insert into governance.catalog_expansion_release_events (
  release_id, event_type, manifest_sha256, actor, reason
) values (
  'east-bay-pilot-v1', 'revoked', '<current manifest sha256>',
  '<revoker>', '<revocation reason>'
);
```

## Execution

Use GitHub Actions → `Controlled catalog expansion`. The `catalog-expansion` environment requires
owner approval. Run one city and action at a time:

1. `status` and inspect every blocker.
2. `discover` to create private candidates only.
3. `trial` and `audit`; resolve identity conflicts manually.
4. `verify` only when the configured provider contract permits durable server storage.
5. `reevaluate`, then run `status` again with readiness required.
6. `publish` with `PUBLISH_VERIFIED`, at or below the release cap.
7. Run a full catalog refresh and recheck dashboard, queue, catalog, and release status.

Publication is transactional. The database trigger rejects a missing/revoked/stale authorization,
an out-of-scope city, exhausted capacity, or newly unhealthy control plane even when the CLI or
GitHub workflow is bypassed. Existing establishment refreshes do not consume release capacity.
