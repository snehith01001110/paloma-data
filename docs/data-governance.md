# Establishment data governance

Paloma stores durable facts only after a versioned source-and-field policy admits them. Unknown
licenses, unknown Overture property lineage, web-crawled facts, restricted provider responses, and
OSM observations fail closed. The durable ledger and canonical decisions are append-only; current
state is a projection, not overwritten history.

## Storage boundaries

- `governance`: versioned source policy, field rights, and contribution terms.
- `catalog`: durable observations, decisions, normalized weekly/special hours, and review history.
- `runtime`: expiring provider responses, leases, rate limits, and provider matching state.
- `review`: conflicts and high-risk single-origin fields requiring a human decision.
- `ingest`: source snapshots, identity links, ingestion runs, and the legacy read-only evidence
  table retained for migration audit.

## Current source decisions

| Source | Durable canonical use | Notes |
|---|---:|---|
| California ABC | Yes, field-scoped | License/legal evidence; not consumer operating hours. |
| DataSF reviewed datasets | Yes, field-scoped | Preserve dataset identity and terms review. |
| DataSF SF Find boundaries | Yes, neighborhood only | Public-domain point-in-polygon display labels; general locations, not legal boundaries. |
| FSQ OS Places | Yes, field-scoped | Apache-2.0 release only; never self-service API attributes. |
| Overture | Yes, property-scoped | Each field retains SourceItems, upstream origins, and licenses. |
| Wikidata | Yes, field-scoped | CC0 corroboration; sparse and lower authority. |
| Merchant/firsthand | After review | Requires active terms, attestation, and merchant verification where applicable. |
| Official websites | No | Ephemeral identity verification only; public access is not a storage license. |
| Yelp/FSQ API cache | No canonical use | TTL runtime layer only under provider-specific policy. |
| OpenStreetMap | Excluded | Reconsider only after an explicit ODbL product and database-boundary review. |

## Refresh and expansion gates

Regulatory evidence refreshes twice weekly; larger durable source snapshots run monthly;
user/merchant corrections and incident refreshes are on-demand. The managed queue worker runs
every 12 hours and scales to zero. Expansion remains disabled until an owner inserts a bounded,
expiring, append-only release authorization. The database status function verifies that the
manifest and scope match, all pending published-cohort conflict and high-risk items are resolved,
coverage was accepted explicitly, contribution terms are active, source snapshots are fresh, and
the worker has clean history across at least two calendar weeks. A trigger enforces the same gate
for every new `public.establishments` row and records its release attribution.

FSQ OS `date_refreshed` is the default current-operation lease for the consumer identity. When an
unchanged place ages beyond that window, only a still-current verification bound to the exact FSQ
place ID may supersede it: a response retained under a written provider contract or an independently
reviewed Paloma manual attestation. Ephemeral API responses never renew production publication.
Manual attestations record their evidence trail, expire after a bounded lease, and do not copy
third-party hours, price, or other restricted response attributes into the durable catalog.
