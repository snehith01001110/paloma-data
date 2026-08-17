# paloma-data

Accuracy-first establishment ingestion for Paloma.

This repository owns **source adapters, normalization, matching, reconciliation, review routing, batch workers, and schedules**. It intentionally does **not** own the canonical Supabase schema; migrations remain in the `paloma` app repository.

## Data philosophy

Paloma's `public.establishments.id` is the permanent product identity. Provider IDs never replace it. Each provider record is stored in the private `ingest` schema and linked to a Paloma UUID only after identity resolution.

The pipeline is conservative by design:

- authoritative/open data before paid per-place APIs;
- fewer high-confidence establishments over broad noisy coverage;
- exact source ID -> deterministic signals -> weighted fuzzy/geospatial match -> review;
- automatic creation only when coordinates and a high-confidence Paloma primary type are available;
- never delete a canonical establishment because a source disappears;
- permanent closure requires corroboration from at least two linked sources in v1;
- raw/persisted source fields are limited to data whose source terms permit durable storage.

## Phase 1 sources

### California ABC

The statewide daily CSV export is the authoritative alcohol-license backbone. We retain stable license identity/status evidence and only treat strong manufacturer license types as automatic Paloma type evidence. Type 42/48 public-premises records are candidates, not automatic `cocktail_bar` / `dive_bar` / `lounge` classifications.

### DataSF Registered Business Locations

Daily San Francisco business registrations provide stable local IDs, address/location evidence, and closure signals. Self-reported NAICS is treated as evidence, not absolute truth.

## Backfill vs incremental

The two execution paths are intentionally separate:

```bash
# One-time initial Bay Area seed/reconciliation
paloma-data backfill ca_abc
paloma-data backfill datasf

# Routine change reconciliation
paloma-data sync ca_abc
paloma-data sync datasf
paloma-data sync-all
```

Both paths are idempotent. `ingest.source_records.payload_hash` means unchanged source rows do not rerun matching/canonical writes.

The current ABC/DataSF transports are snapshots because both are free government bulk sources; incremental behavior happens at the record layer through stable IDs and hashes. FSQ OS deltas and Overture release/version handling are the next adapters and fit the same `SourceRecord` contract.

## Matching v1

The weighted score follows the directory research:

```text
0.40 normalized name
0.30 normalized address
0.10 phone
0.10 website host
0.10 geospatial proximity
```

Initial bands:

- exact provider source ID: update linked UUID;
- deterministic exact-address/strong-name or phone signal: auto-match;
- >= 0.92 with a clear lead: auto-match;
- 0.80-0.92: review queue;
- < 0.80: distinct unless stronger evidence arrives.

## Database boundary

The worker requires a server-side Postgres URL:

```bash
export SUPABASE_DB_URL='postgresql://...'
```

Do not place this secret in the iOS app or commit it. The `ingest` schema is revoked from `public`, `anon`, and `authenticated` roles.

## Automation

`.github/workflows/sync.yml` runs the incremental worker on weekdays and supports manual backfills. The workflow expects a repository secret named `SUPABASE_DB_URL`.

For larger national/international bulk jobs, the same Docker image can move to Cloud Run Jobs/ECS/Fargate without changing matching or database semantics. GitHub Actions is adequate for the initial SF/Bay Area government-source worker and keeps infrastructure cost near zero while volume is small.

## Next source adapters

1. FSQ OS Places: monthly releases/deltas, Apache 2.0, category-filtered before canonicalization.
2. Overture Places: monthly GeoParquet release, spatially clipped before matching.
3. Commercial enrichment only for unresolved high-value gaps such as current hours/phone/closure confidence.

Apple Maps and standard Google Places are not canonical bulk seed sources.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest -q
```
