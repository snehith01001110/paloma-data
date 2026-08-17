# paloma-data

Accuracy-first establishment ingestion for Paloma.

This repository owns **source adapters, normalization, entity matching, field-level provenance, reconciliation, review routing, batch workers, schedules, and the internal ingestion dashboard**. The canonical Supabase schema migrations remain in the `paloma` app repository.

## Data philosophy

Paloma's `public.establishments.id` is the permanent product identity. Provider IDs never replace it. Each provider record is stored in the private `ingest` schema and linked to a Paloma UUID only after identity resolution.

The pipeline is conservative by design:

- authoritative/open data before paid per-place APIs;
- fewer high-confidence establishments over broad noisy coverage;
- exact source ID -> deterministic signals -> weighted fuzzy/geospatial match -> review;
- automatic creation only when coordinates and a high-confidence Paloma primary type are available;
- never delete a canonical establishment because a source disappears;
- permanent closure requires corroboration from at least two linked sources and no linked source reporting open;
- raw/persisted source fields are limited to data whose source terms permit durable storage;
- **entity identity confidence is separate from confidence in each canonical field**.

A venue can be confidently identified while its current public-facing name is uncertain. That is not a 0.99-quality venue. `data_quality_score` is therefore the weakest critical resolved field across identity, display name, and primary type.

## Sources

### California ABC

The statewide alcohol-license export is the authoritative California license backbone. It is strongest for license identity/status, address, and manufacturer license type. ABC business/licensee names are stored as **legal-name evidence** and do not automatically become the consumer-facing display name.

GitHub-hosted runners may be rejected by ABC. The worker therefore uses the Supabase OIDC relay only to retrieve the exact official ABC-hosted export; ABC remains the source of truth.

### DataSF Registered Business Locations

San Francisco business registrations provide stable local IDs, address/location evidence, and closure signals. Registered names are treated as **legal/registry-name evidence**, not assumed to be the current venue brand.

### Overture Maps Places

Overture provides open consumer-place evidence for names, coordinates, phone, website, and category. It is useful for corroboration and discovery, but a single Overture display name is intentionally below Paloma's strong-name threshold.

### Verified first-party web

For linked canonical establishments with candidate websites, Paloma verifies the page against address/phone/location signals before trusting it. Structured LocalBusiness/Brewery/etc. data from a verified first-party page is the highest-authority display-name evidence.

This source is called `official_web` in field provenance. It is an enrichment source, not an establishment identity provider.

## Field-level provenance and confidence

`ingest.establishment_field_evidence` stores competing claims instead of overwriting canonical fields blindly. Important fields include:

- `display_name`
- `legal_name`
- `primary_type_slug`
- address/location
- phone/website
- status

Each evidence row carries source, source record, evidence confidence, entity identity confidence, field-specific source authority, source freshness, and whether it was selected.

The canonical establishment exposes:

- `identity_confidence`
- `display_name_confidence`
- `display_name_source`
- `type_confidence`
- `field_resolution_version`
- `data_quality_score`

Current resolver version: `v2`.

A changed display name may replace the canonical name automatically only when first-party verification is strong, or multiple independent public-facing sources strongly agree. A lone aggregator can surface a conflict but cannot silently rename a venue.

## Rebrands and operator changes

Matching is deliberately tolerant of brand changes:

- same phone + strong physical location can auto-match even if the name is very different;
- same website + strong address can auto-match;
- same physical location + divergent name is routed to `same_location_name_conflict` rather than automatically creating a duplicate establishment.

The permanent Paloma UUID survives a validated rename.

## Backfill vs incremental

```bash
# Explicit full rebuild of all primary sources + first-party name verification
paloma-data rebuild-catalog

# One source full backfill
paloma-data backfill ca_abc
paloma-data backfill datasf
paloma-data backfill overture

# Routine reconciliation
paloma-data sync ca_abc
paloma-data sync datasf
paloma-data sync overture
paloma-data sync-all

# Re-check current public-facing names only
paloma-data enrich-web

# Recompute field evidence/resolution without source network fetches
paloma-data resolve-fields
```

`bootstrap` is migration-aware. Normal deployments skip already-complete initial backfills, but if ingestion-backed rows do not have the current resolver version it forces one complete three-source rebuild before resolving fields. Once all rows are current, normal skip behavior resumes.

## Matching

The baseline weighted identity score remains:

```text
0.40 normalized name
0.30 normalized address
0.10 phone
0.10 website host
0.10 geospatial proximity
```

Important deterministic rules take precedence when stronger identity evidence exists, including exact source identity, exact address/strong name, exact phone/strong location, and exact website/strong location.

Initial fuzzy bands remain conservative:

- >= 0.92 with a clear lead: auto-match;
- 0.80-0.92: review queue;
- < 0.80: distinct unless stronger physical/identity evidence indicates a possible rebrand.

## Database boundary

The worker requires a server-side Postgres URL. Production GitHub Actions does **not** store a long-lived database password in GitHub. It exchanges a GitHub OIDC token with the `paloma-data-credentials` Supabase Edge Function for a scoped `paloma_ingest` database credential.

Never place database credentials in the iOS app or commit them. The `ingest` schema is not exposed to normal product roles.

## Automation

`.github/workflows/sync.yml`:

- runs government reconciliation on weekdays;
- re-verifies first-party public names weekly;
- runs Overture reconciliation monthly;
- supports manual source/full rebuilds;
- forces one full rebuild automatically when the field resolver version advances.

The internal dashboard in `site/` is deployed by `.github/workflows/pages.yml`. Its backing `paloma-data-progress` Edge Function exposes only operational/public-business metadata, including field-confidence health and limited name evidence.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest -q
```
