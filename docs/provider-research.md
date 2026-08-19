# Provider decision record

Reviewed 2026-08-18. Primary sources:

- [California ABC license status glossary](https://www.abc.ca.gov/licensing/license-lookup/glossary/)
- [California ABC license types](https://www.abc.ca.gov/licensing/license-types/)
- [California ABC reports/export](https://www.abc.ca.gov/licensing/licensing-reports/)
- [FSQ OS access and current-place filter](https://docs.foursquare.com/data-products/docs/access-fsq-os-places)
- [FSQ OS schema and delta actions](https://docs.foursquare.com/data-products/docs/places-os-data-schema)
- [Foursquare Pro/Premium schema](https://docs.foursquare.com/data-products/docs/places-pro-and-premium)
- [Foursquare Place Details API](https://docs.foursquare.com/fsq-developers-places/reference/place-details)
- [Foursquare Place Search API](https://docs.foursquare.com/fsq-developers-places/reference/place-search)
- [Foursquare authentication/service keys](https://docs.foursquare.com/fsq-developers-places/reference/authentication)
- [Foursquare API response fields](https://docs.foursquare.com/fsq-developers-places/reference/response-fields)
- [Foursquare API agreement](https://foursquare.com/legal/terms/apilicenseagreement/)
- [Foursquare API usage and retention guidelines](https://docs.foursquare.com/fsq-developers-places/reference/usage-guidelines)
- [Foursquare acceptable-use policy](https://foursquare.com/legal/terms/aup/)
- [Yelp Places API terms](https://terms.yelp.com/developers/api_terms/20250113_en_us/)
- [Yelp display requirements](https://terms.yelp.com/developers/display_requirements/)
- [Yelp Places API FAQ](https://docs.developer.yelp.com/docs/places-faq)
- [Overture Places guide](https://docs.overturemaps.org/guides/places/)
- [Overture Place schema](https://docs.overturemaps.org/schema/reference/places/place/)
- [Google Places policies](https://developers.google.com/maps/documentation/places/web-service/policies)
- [DataSF SF Find Neighborhoods](https://data.sfgov.org/Geographic-Locations-and-Boundaries/SF-Find-Neighborhoods/gfpk-269f)

## Decision

Automated publication semantics are versioned as `v7`. This revision retains the v6 identity and
producer protections and separates provider uncertainty from conclusive negative evidence. Missing
hours, incomplete/unusable payloads, low provider veracity, reclassification, and stale provider
IDs are inconclusive: they may require stronger evidence but cannot withdraw a listing as if the
establishment had closed. Withdrawal remains fail-closed for identity-matched explicit closure,
nonexistence, or private-access evidence. Generic `brewery`, `winery`, and `distillery` names
remain access-unknown.

Use California ABC for exact license state/privileges and Apache-2.0 FSQ OS for durable bulk
discovery/name/address plus phone/website observations. Their complementary facts may verify only direct
public-premises bars and explicit Type 75 brewpubs. Restaurant-license bars and every manufacturer
premise require stronger access evidence. A generic manufacturer always requires manual public
access attestation; a specifically classified tasting room or taproom may use contracted provider
hours. A data agreement must expressly permit server retention for any persisted provider fields.
Use municipal polygons, DataSF, Overture, and Wikidata only as field-specific enrichment or
corroboration. OSM is excluded from the proprietary canonical database pending a deliberate ODbL
decision; no OSM-derived value is persisted or used by the resolver.

FSQ OS `date_refreshed` does not assert field-level freshness. Open-evidence rows therefore omit a
phone or website unless a second durable source with independent upstream lineage agrees. Civic
neighborhood matches within 10 meters of a polygon edge are also omitted rather than risk a label
caused by coordinate noise.

Do not use a state license as sufficient publication evidence. It proves a licensed premise, not a
current consumer-facing brand, ordinary walk-in access, or even the existence of a tasting room.

Do not use Overture operating status as the publication clock. Overture introduced the field with
all values initially set to open and later changed default/null behavior; its confidence is an
existence score, not field-level truth. Preserve and inspect its upstream datasets because Overture
and FSQ may share lineage.

Do not use Google Places to persist Paloma's catalog. Its policy is incompatible with durable
prefetch/storage and a non-Google map. Do not persist self-service Foursquare API attributes either:
the current Usage Guidelines permit PAYG/sandbox retention of place IDs only, with no caching of
other attributes. The code defaults to ephemeral trial use and requires a specific written
server-retention grant before the production storage switch can be enabled.

Self-service Place Details can still be used as a transient display overlay. Paloma's endpoint may
look up only an already-published, conservatively linked FSQ ID, revalidate identity and provider
quality on every call, return `no-store`, and discard the response after the current detail view.
This improves optional-field coverage without allowing licensed data to determine catalog
membership or become database ground truth. It also requires Foursquare's venue link and visual
credit whenever rich fields are shown.

Yelp is an optional transient enrichment provider. Paloma may retain a matched Yelp business ID and
may server-cache API content for no more than 24 hours. The implementation uses a 22-hour serving
window plus a fifteen-minute purge schedule, stores raw responses outside the consumer schema,
requires Yelp attribution at display time, and never feeds cached Yelp values into publication or
the durable establishment row. A provider outage or expired response therefore falls back to
Paloma's durable fields or the uncached Foursquare overlay rather than stale Yelp content.

Runtime resolution is field-level rather than provider-level. Validated Yelp values have
precedence for the fields Yelp exposes; Foursquare is requested only for remaining fields. The
response carries per-field provenance and all attributions actually used. A mixed response cannot
inherit Yelp's cache expiry and remains current-view-only because it contains an uncached
Foursquare observation. Aggregate-safe logs record provider mode and returned field names, never
venue IDs, provider IDs, URLs, payload values, or user identities.

Yelp identity discovery is proactive but bounded. A weekly incremental job considers only currently
published, verified walk-in venues and searches only new, changed, or due identities, with an
explicit API-call cap. It retains the validated durable business ID and Paloma-owned match metadata,
then discards the search response. Paloma rejects ambiguous matches and enforces strong consumer
name, alcohol category, and 100-meter coordinate guards. Negative outcomes have bounded cooldowns;
the Edge Function's user-triggered matcher remains a fallback, not the normal first-view path.
Business Details stays on demand. Successful payloads are identity-validated before storage, capped
at 256 KiB, and protected by a single-flight lease.

The cache is policy-enforced rather than convention-based. SQL rejects every provider except Yelp,
rejects expiry beyond 22 hours and oversized payloads, and gives no client or ingest role access to
cached payloads. Edge code derives canonical request fingerprints, validates identities before
storage, uses a short single-flight lease for cold keys, and refuses Foursquare server caching before
any database operation can occur. Durable provider IDs are stored separately from licensed response
bodies, and the Edge Function assumes a no-login least-privilege database role.

The current Places API response fields include phone, website, hours, price, attributes, and
veracity rating, but not neighborhood. Neighborhood is therefore a separate civic-boundary fact,
not a value inferred from the provider or postal address.

## Refresh policy

Expansion is currently paused while the existing materialized cohort is refined. Scheduled
verification and queue refreshes include only already-materialized published or suppressed
identities, and scheduled publication is disabled. Source snapshots may continue so evidence stays
fresh; adding a new establishment still requires a later explicit scope decision and manual
publication action.

- ABC: monthly complete snapshot plus on-demand correction; only exact `ACTIVE`; absence processed
  only after success.
- FSQ OS: monthly complete Bay Area snapshot, with stable IDs and safe retirement; delta support is
  the next optimization, not a correctness dependency.
- Open FSQ OS + direct-public ABC: reevaluate monthly; leases are at most 45 days and never extend
  beyond the FSQ OS 365-day freshness deadline.
- Specifically licensed Premium/API: target only new or due candidates; 45-day lease by default.
- Yelp durable-ID sync: monthly and after publication; unchanged matched identities are rechecked at
  most every 90 days, while negative/error cooldowns are shorter and bounded.
- SF Find boundaries: monthly complete snapshot and point-in-polygon resolution; other cities stay
  null until their boundary feed has been reviewed.
- Manual attestation: 90-day lease by default.
- Expired verification: fail closed and suppress from consumer reads.

## Known non-goals

No provider can truthfully populate every optional field for every establishment. Price, hours,
settings, and imagery are independently nullable. Coverage is monitored, but it never weakens the
publication gate or causes a value to be guessed.
