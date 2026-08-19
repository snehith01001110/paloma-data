# paloma-data

Accuracy-first establishment catalog for Paloma.

The core invariant is simple: **a discovery record is not an establishment**. Provider and
government records stay in the private `ingest` schema. `public.establishments` receives a row
only after the place passes a versioned, time-bounded verification decision.

## Production model

```text
complete source snapshots
  -> ingest.source_records                    raw normalized evidence
  -> ingest.catalog_candidates                private discovery/conflation entities
  -> ingest.candidate_source_links             conservative identity links
  -> ingest.candidate_verifications            licensed-provider/manual checks + expiry
  -> ingest.catalog_evaluations                immutable decision history
  -> public.establishments                     only verified product entities
```

Candidate and establishment IDs are deliberately separate concepts. In v2 the candidate UUID is
reused when it is first materialized, giving Paloma a stable product ID without exposing rejected
or ambiguous candidates.

The legacy pipeline wrote hidden candidates into `public.establishments`. Do not use it. The v2
CLI no longer calls that path, and code pushes no longer trigger data rebuilds.

## Publication hard gates

`catalog.py` uses hard gates, not a composite quality score. A candidate is publishable only when:

1. A current FSQ OS record identifies a Paloma consumer POI. Rows that also describe a
   restaurant, cafe, retail store, lodging property, gym, office, social club, or other parent
   business cannot anchor a candidate merely because FSQ attached a secondary bar category.
   Generic manufacturer categories remain private until the richer access checks below pass.
2. Its FSQ `date_refreshed` is no more than 365 days old and no closure/private/duplicate flag is
   present.
3. A conservatively linked California ABC record has raw status exactly `ACTIVE` and is a license,
   not an application.
4. The ABC license type is compatible with the consumer venue type.
5. The FSQ and ABC identity links each meet the 0.96 publication threshold.
6. Public access is proven by one of three narrow paths: (a) current FSQ OS consumer evidence plus
   an exact direct-public-premises ABC type (40/42/48/61), (b) an explicit brewpub plus Type 75,
   or (c) a licensed provider/manual attestation passing identity, operation, access, name, and
   type checks. Restaurant and manufacturer licenses cannot use path (a).
7. The resulting verification lease is unexpired and every persisted field has storage rights.
8. Every required materialization field is present.

Overture, DataSF, and OpenStreetMap may discover, corroborate, or enrich. None can publish a place
on its own. Overture provenance is decomposed into upstream origin keys, so an Overture row copied
from Foursquare is never counted as independent Foursquare corroboration. Legacy or unknown
Overture lineage cannot corroborate a contact field at all.

Manufacturer premises remain fail-closed. A Type 02, 23, or 74 license is not tasting-room proof.
A generic brewery, winery, or distillery requires a manual public-access attestation; even
provider business hours can describe a production office or appointment-only facility. An
explicit tasting-room or taproom POI may use current high-veracity provider hours instead.
An FSQ winery+wine-bar combination becomes a `tasting_room` candidate; brewery+bar and
brewery+restaurant combinations become `taproom` and `brewpub` candidates. These mappings improve
license compatibility but never bypass the access gate.

## ABC semantics

ABC is the legal backbone, not a venue directory.

- `DBA Name` is used when the export provides it; `Primary Name` is retained as legal evidence.
- Raw status is mapped by an exact allowlist. Only `ACTIVE` becomes open. `SUREND`, `SUSPEN`,
  `REVPEN`, `NREN`, `R65`, and unknown future codes all fail closed.
- Types 40, 42, 48, and 61 validate bars/taverns but do not choose a consumer subtype. Types 41,
  47, and 87 may validate an independently identified bar at a bona fide eating place; they do
  not turn restaurants into Paloma venues.
- Type 75 may validate an explicit brewpub.
- Types 01/23, 02, and 74 validate compatible manufacturer facets only after consumer-access
  verification.

ABC publishes a complete snapshot. A source row is marked missing only after the replacement
snapshot was fully consumed successfully; a download failure can never cause mass withdrawals.

## Consumer data and licensing

FSQ Open Source Places is the preferred discovery layer. It is Apache-2.0, has stable place IDs,
phone and website observations, quality flags, refresh/closure dates, and monthly
add/update/remove/merge deltas. Its `date_refreshed` is place-level rather than field-level, so an
open-evidence publication retains phone/website only when an independent durable source agrees.
The current implementation performs a geography-bounded complete snapshot; stable hashes and
`last_seen_run_id` provide idempotency and safe absence detection.

Hours, price, provider veracity, and richer attributes require Foursquare Premium/API access. The
current Foursquare Usage Guidelines allow self-service pay-as-you-go/sandbox customers to retain
place IDs but prohibit caching all other API attributes; even the documented default enterprise
rule allows only 24-hour local-device caching, not server caching. A normal API key therefore does
**not** authorize Paloma to put API name/phone/hours/price into Postgres. `catalog-trial` keeps those
responses in memory and persists only an attribute-coverage/decision audit with no returned field
values. Production persistence is disabled unless `FSQ_SERVER_STORAGE_LICENSED=true`, which is
reserved for a written agreement that expressly grants server retention and display rights.

For consumer detail screens, `venue-live-details` implements the no-contract production path. It
accepts only an authenticated user's public establishment UUID, resolves a high-confidence FSQ ID
server-side, and calls Place Details only when a durable optional field is missing. Before returning
anything, it rechecks the immutable ID, provider veracity, closure flags, name, category, and a
100-meter coordinate guard. Responses use `Cache-Control: no-store`; neither Postgres nor logs
receive provider values, and the iOS client keeps them only in the open view's memory. Per-user and
global aggregate counters bound spend without recording which places were requested. Rich values
must be accompanied by the required Foursquare venue link and visual credit in the client.

When `YELP_API_KEY` is configured, the same endpoint adds a policy-bounded Yelp path. The scheduled
`provider-links-sync` job resolves strictly validated Yelp business IDs for new or changed
published venues before user traffic; a user-triggered matcher remains only as a fallback. The
first detail request can therefore read Business Details immediately. Valid raw JSON is cached once
for all users, while concurrent misses share a single refresh lease.
Cache hits do not consume paid-provider quota. Yelp data can fill phone, hours, and price in the
transient overlay; Foursquare is then called only for fields Yelp did not supply. The combined
response records field-level sources and every attribution actually used. Mixed responses are
never reusable because the Foursquare portion is uncached. Yelp's profile URL is attribution,
never misrepresented as the venue website.

Licensed runtime enrichment uses two deliberately separate storage paths:

- `ingest.runtime_provider_links` retains only provider identifiers and Paloma-owned match
  metadata. Current terms permit indefinite retention of FSQ place IDs and Yelp business IDs.
- `ingest.provider_response_cache` is a private, raw-payload cache that accepts only Yelp rows and
  enforces a 22-hour maximum lifetime and a 256 KiB payload limit in both SQL and Edge Function
  code. A short refresh lease collapses concurrent cold requests, and a fifteen-minute database
  job removes expired rows. Expired content is never served as a stale fallback.
- `ingest.provider_match_state` stores only Paloma-owned fingerprints, outcomes, cooldowns, and
  short leases. It prevents repeated Business Match calls when Yelp has no safe match.

Foursquare PAYG/Sandbox responses never enter that cache. Every client response remains
`no-store`; Foursquare rich fields live only for the open detail-view session. The Edge Function
assumes the no-login `paloma_runtime` role, which can read only eligible catalog evidence and manage
the private runtime tables. Supporting a new cacheable provider requires an adapter, an explicit
retention/payload policy, identity validation, attribution UI, tests, and a reviewed database
migration. An unreviewed adapter therefore cannot silently retain licensed data.

The durable no-contract path is still useful: direct-public bars can pass using complementary
Apache-2.0 FSQ OS and California ABC evidence, and FSQ OS phone/website fields may be stored. Rich
optional fields stay null unless an open, manual, or specifically licensed source supports them.

Google Places is intentionally not a catalog source: its policies restrict prefetching, caching,
and use with a non-Google map. A data source that cannot legally back Paloma's persistent database
is not part of this pipeline.

The current Places API does not expose neighborhoods. San Francisco labels therefore come from
the public-domain SF Find polygon feed and a deterministic point-in-polygon join. A point within
10 meters of a polygon edge remains `NULL` because ordinary coordinate noise can put a storefront
on the wrong side of a neighborhood boundary. Other cities remain `NULL` until a reviewed civic
boundary feed is configured; the pipeline never guesses a neighborhood from an address.

## Field contract

Required for publication:

- consumer-facing name;
- Paloma primary type;
- street address, city, country;
- latitude and longitude;
- current operating/public-access verification;
- exact active compatible ABC license;
- verification timestamps and provenance.

Optional fields remain `NULL` when trustworthy evidence is unavailable:

| Field | Preferred source | Fallback | Never do |
|---|---|---|---|
| phone | transient Place Details display | two independent durable sources/manual | persist a self-service API response |
| website | transient Place Details display | two independent durable sources/manual | persist a self-service API response |
| neighborhood | reviewed civic polygon | reviewed division polygon | free-text guess from address |
| hours | transient Place Details display | contracted FSQ/manual owner attestation | cache or fabricate a schedule |
| price | transient Place Details display | contracted FSQ/manual | infer from type or neighborhood |
| setting | transient objective provider attributes | contracted FSQ/manual | infer subjective vibe |
| cover image | licensed/owner-supplied asset | none | reuse a URL without display rights |

Completeness and truth are separate. A correct row with null price is publishable; a guessed price
is not.

## Initial backfill rollout

Apply the additive v2 migration first. It does not change the current public catalog.

```bash
# 1. Load authoritative and preferred consumer evidence privately.
paloma-data backfill ca_abc
paloma-data backfill datasf
paloma-data backfill fsq
paloma-data sync-neighborhoods

# 2. Build only a small private trial set.
paloma-data catalog-discover --city "San Francisco" --limit 20

# 3. Make at most 20 targeted detail calls. This never mutates the public catalog.
paloma-data catalog-trial --city "San Francisco" --limit 20
# Pre-cutover paid audit of only the current publishable set (up to 100).
paloma-data catalog-trial --city "San Francisco" --limit 100 --verified-only

# 4. Review the JSON results and match-review queue. Direct-public bars may already have an
#    open-evidence lease. Only with specific server-retention rights, persist a rich provider pass.
# paloma-data catalog-verify --city "San Francisco" --limit 20
paloma-data catalog-status

# 5. After the trial is manually accepted, replace the pre-launch junk table once.
paloma-data catalog-cutover \
  --confirm REPLACE_PUBLIC_CATALOG \
  --minimum-verified 20
```

The cutover truncates rebuildable product interactions and the old public catalog, but preserves
Auth identities, profiles, source snapshots, private candidates, verifications, and evaluation
history. It refuses to run unless the requested number of unexpired verified candidates exists.

After launch, use `catalog-publish --confirm PUBLISH_VERIFIED`; do not truncate user-linked data.

## Incremental operation

The workflow has three independent paths:

- Weekdays: replace ABC/DataSF snapshots, geocode private legal evidence, reevaluate candidates,
  immediately suppress hard negatives, and sweep expired verification leases.
- Monthly: replace the bounded FSQ OS snapshot, refresh civic neighborhood polygons, and create
  private candidates for newly discovered places. Overture is optional corroboration and cannot
  block the core job.
- Weekly: reevaluate open evidence; optionally refresh a bounded set through a specifically
  licensed provider; materialize passing candidates only when `PALOMA_CATALOG_AUTO_PUBLISH=true`;
  resolve due Yelp business IDs without storing Yelp attributes; and suppress expired rows.
  Auto-publish remains off until the initial cutover is approved.

No GitHub push runs ingestion. Deployment and data mutation are deliberately separate.

## Durable worker control plane

Per-candidate maintenance runs through the private `paloma_pipeline` Supabase Queue. Logical runs,
active-job deduplication, worker leases, bounded retries, attempt audits, and terminal dead letters
live in `ingest.pipeline_*`. Queue functions are available only to the scoped `paloma_ingest`
database role; clients cannot enqueue work through the Data API.

GitHub Actions initially drains the same queue used by a managed container worker. This preserves
the current recovery path while allowing compute to move without changing job semantics. A queued
candidate refresh may update or suppress an already materialized establishment but cannot publish
a new identity. See [production pipeline operations](docs/production-pipeline.md) for deployment,
cutover, alerting, and capacity guidance.

## Commands

```bash
paloma-data stage-source ca_abc --mode full
paloma-data backfill fsq
paloma-data sync ca_abc
paloma-data sync-government
paloma-data catalog-discover --city "San Francisco" --limit 20
paloma-data catalog-trial --city "San Francisco" --limit 20
paloma-data catalog-verify --limit 250
paloma-data catalog-reevaluate
paloma-data catalog-audit --city "San Francisco" --limit 500
# Human-reviewed exception; both resolutions preserve the evidence and trigger reevaluation.
paloma-data catalog-review-resolve --review-id 123 --resolution not_same_or_stale \
  --confirm RESOLVE_MATCH_REVIEW
paloma-data catalog-publish --confirm PUBLISH_VERIFIED
paloma-data provider-links-sync --provider yelp --city "San Francisco" --limit 25
paloma-data pipeline-enqueue-catalog --city "San Francisco" --limit 5000
paloma-data pipeline-worker --drain --max-jobs 5000 --fail-on-error
paloma-data pipeline-status
paloma-data catalog-sweep
paloma-data catalog-status
paloma-data sync-neighborhoods
```

## Configuration

See `.env.example`. Required server-side values are:

- `SUPABASE_DB_URL` or `DATABASE_URL`;
- optional `PALOMA_PIPELINE_REQUESTER` and `PALOMA_WORKER_ID` audit labels;
- FSQ Places Portal Iceberg connection values for FSQ OS discovery;
- optional `FSQ_PLACES_API_KEY` for a bounded, non-caching trial;
- `FSQ_PLACES_API_KEY` as a Supabase Edge Function secret for transient consumer detail lookups;
- `YELP_API_KEY` as both a Supabase Edge Function secret and GitHub Actions secret. The scheduled
  job retains only durable Yelp IDs; the Edge Function owns the 22-hour attribute cache;
- `FSQ_SERVER_STORAGE_LICENSED=true` only under written server-retention/display rights;
- `PALOMA_CATALOG_AUTO_PUBLISH=true` only after the initial cutover is approved;
- `SF_NEIGHBORHOODS_URL` defaults to DataSF's public-domain SF Find GeoJSON feed.

Generate an optional API service key in the Foursquare Developer Console and store it as a local or
GitHub Actions secret—never in this repository. The free OS bulk token is generated separately in
the Foursquare Places Portal. An API key alone does not permit server-side attribute caching.

Production GitHub Actions exchanges GitHub OIDC for the scoped `paloma_ingest` Postgres credential.
No database password belongs in GitHub or the iOS app.

## Database security

New v2 and pipeline-control tables enable RLS, revoke client privileges, and grant one explicit
`paloma_ingest` policy. The pgmq tables also have RLS enabled with no client policy; workers use
privilege-contained functions for one fixed private queue. Existing legacy ingest tables predate v2
and should receive the same RLS hardening in a separately approved migration after confirming every
operational role; enabling RLS without the ingest policy would stop scheduled jobs.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest -q
```

Decision version: `v7` (v6 protections plus conclusive-negative semantics: incomplete, stale, or
unusable provider results require more evidence but cannot masquerade as establishment closures).
The additive database architecture remains catalog v2.
