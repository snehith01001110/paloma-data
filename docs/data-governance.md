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
| FSQ OS Places | Yes, field-scoped | Apache-2.0 release only; never self-service API attributes. |
| Overture | Yes, property-scoped | Each field retains SourceItems, upstream origins, and licenses. |
| Wikidata | Yes, field-scoped | CC0 corroboration; sparse and lower authority. |
| Merchant/firsthand | After review | Requires active terms, attestation, and merchant verification where applicable. |
| Official websites | No | Ephemeral identity verification only; public access is not a storage license. |
| Yelp/FSQ API cache | No canonical use | TTL runtime layer only under provider-specific policy. |
| OpenStreetMap | Excluded | Reconsider only after an explicit ODbL product and database-boundary review. |

## Refresh and expansion gates

Durable source snapshots run monthly; user/merchant corrections and incident refreshes are
on-demand. The managed queue worker runs every 12 hours and scales to zero. Expansion remains disabled until
all pending conflict and high-risk review items for the published cohort are resolved, coverage is
accepted explicitly, contribution terms are approved, and scheduled runs are healthy.
