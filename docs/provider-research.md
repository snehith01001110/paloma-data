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
- [Overture Places guide](https://docs.overturemaps.org/guides/places/)
- [Overture Place schema](https://docs.overturemaps.org/schema/reference/places/place/)
- [Google Places policies](https://developers.google.com/maps/documentation/places/web-service/policies)
- [DataSF SF Find Neighborhoods](https://data.sfgov.org/Geographic-Locations-and-Boundaries/SF-Find-Neighborhoods/gfpk-269f)

## Decision

Automated publication semantics are versioned as `v5`. This revision retains the v4 protections
and narrowly permits an explicit `brewpub`, `taproom`, or `tasting room` name to refine a
compatible generic producer category. Generic `brewery`, `winery`, and `distillery` names remain
access-unknown.

Use California ABC for exact license state/privileges and Apache-2.0 FSQ OS for durable bulk
discovery/name/address plus phone/website observations. Their complementary facts may verify only direct
public-premises bars and explicit Type 75 brewpubs. Restaurant-license bars and every manufacturer
premise require stronger access evidence. A generic manufacturer always requires manual public
access attestation; a specifically classified tasting room or taproom may use contracted provider
hours. A data agreement must expressly permit server retention for any persisted provider fields.
Use municipal polygons, OSM, DataSF, and Overture only as field-specific enrichment or
corroboration.

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

The current Places API response fields include phone, website, hours, price, attributes, and
veracity rating, but not neighborhood. Neighborhood is therefore a separate civic-boundary fact,
not a value inferred from the provider or postal address.

## Refresh policy

- ABC: complete business-day snapshot; only exact `ACTIVE`; absence processed only after success.
- FSQ OS: monthly complete Bay Area snapshot, with stable IDs and safe retirement; delta support is
  the next optimization, not a correctness dependency.
- Open FSQ OS + direct-public ABC: reevaluate weekly; leases are at most 45 days and never extend
  beyond the FSQ OS 365-day freshness deadline.
- Specifically licensed Premium/API: target only new or due candidates; 45-day lease by default.
- SF Find boundaries: monthly complete snapshot and point-in-polygon resolution; other cities stay
  null until their boundary feed has been reviewed.
- Manual attestation: 90-day lease by default.
- Expired verification: fail closed and suppress from consumer reads.

## Known non-goals

No provider can truthfully populate every optional field for every establishment. Price, hours,
settings, and imagery are independently nullable. Coverage is monitored, but it never weakens the
publication gate or causes a value to be guessed.
