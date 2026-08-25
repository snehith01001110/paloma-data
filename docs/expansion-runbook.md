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
- full catalog refreshes have succeeded in the number of UTC calendar weeks required by the
  checked-in manifest, with the latest no more than 96 hours old; and
- the release still has capacity.

The checked-in manifest explicitly identifies its deployment phase. Development may use one
healthy refresh week so an empty pre-launch catalog is not forced to idle. Production validation
requires at least two healthy refresh weeks; before launch, change `deployment_phase` to
`production` and restore `minimum_healthy_refresh_weeks` to at least `2`. Either change updates the
manifest hash and invalidates earlier release authorizations.

## Owner authorization

Authorization is a governance decision, not a worker capability. `paloma_ingest` has read-only
access to the event table. An owner uses the Supabase SQL editor or an equivalent `postgres`
connection to insert one immutable event after reviewing the terms and the exact coverage snapshot.
Never copy the example placeholders unchanged.

Contribution-terms activation is also an owner decision. The database hash must match the reviewed
file exactly. Development approval does not authorize accepting public contributions in production;
complete legal review and activate a production-approved version before enabling that workflow.

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
  'v7', 1, 21, 96, 14,
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

Use GitHub Actions → `Controlled catalog expansion`. The checked-in manifest is currently in
development phase, so the GitHub environment approval gate is intentionally disabled; the database
authorization, release-health gate, scoped workflow, and explicit CLI confirmations remain required.
Restore the `catalog-expansion` environment gate before switching the manifest to production. Run one
city and action at a time:

1. `status` and inspect every blocker.
2. `discover` to create private candidates only.
3. `trial` and `audit`; resolve identity conflicts with `resolve-review` in the same protected
   workflow. Supply `RESOLVE_MATCH_REVIEW`, the exact review ID, city, decision, and a short rationale.
   The GitHub actor and evidence snapshot are recorded in immutable history. `same_place` creates a
   durable link bound to the reviewed evidence fingerprint, while `not_same_or_stale` prevents that
   exact prompt from reopening until its source evidence changes.
4. Use `attest` only after a named reviewer checks current first-party or authoritative pages for
   identity, operation, ordinary public access, display name, and venue type. Supply 1–10 HTTPS
   evidence URLs, `MANUAL_ATTESTATION`, and a type override only when the evidence corrects a coarse
   FSQ OS classification. Use `pass` only when every hard fact is confirmed; use `fail` only for an
   explicit closure, move, or loss of ordinary public access. The command appends a 90-day
   verification lease and retains the evidence trail; it deliberately does not copy hours, phone,
   website, price, or neighborhood fields.
5. Use `observe-field` for a reviewed atomic phone, website, neighborhood, normalized hours, price,
   or setting fact. Supply `RECORD_FIELD_OBSERVATION`, the exact candidate and city, JSON value,
   1–10 current HTTPS evidence URLs, and a short review note. The command stores the normalized fact,
   reviewer, timestamp, and evidence links, but never the source page payload. Do not infer price or
   setting from branding. Unreviewed provider phone and website values need two independent origins.
   For the checked-in East Bay pilot batch, use `observe-manifest` with
   `RECORD_FIELD_MANIFEST`. It validates candidate UUID, normalized name, city, evidence URL, and
   hours structure before writing anything; the whole batch commits or rolls back together and a
   rerun is idempotent.
6. Run `enrich-open-attributes` from catalog maintenance to append policy-approved Overture civic
   divisions for both verified candidates and published establishments. OSM remains excluded.
   A missing neighborhood is acceptable when the open layer has no containing division. Do not
   substitute Oakland's unlicensed portal layer or an informal Berkeley boundary map; add a
   versioned municipal source policy only after its durable-reuse terms are explicitly reviewed.
7. `verify` only when the configured provider contract permits durable server storage.
8. `reevaluate`, then run `status` and `audit` again. Resolve every conflict or leave the field null.
   Use the `review-field-conflict` workflow action with `REVIEW_FIELD_CONFLICT` to record each
   human selection or audited unknown in the append-only review history. Supply the exact city of
   the published establishment; the reviewer rejects a conflict ID from any other city.
9. `publish` with `PUBLISH_VERIFIED`, at or below the release cap.
10. Run a full catalog refresh and recheck dashboard, queue, catalog, and release status.

Publication is transactional. The database trigger rejects a missing/revoked/stale authorization,
an out-of-scope city, exhausted capacity, or newly unhealthy control plane even when the CLI or
GitHub workflow is bypassed. Existing establishment refreshes do not consume release capacity.
