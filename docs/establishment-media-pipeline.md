# Establishment media pipeline

This pipeline is establishment-agnostic. `catalog.establishment_media_work_queue`
is computed from every row whose `public.establishments.publication_state` is
`published`; it contains no Crown Billiards ID, city allowlist, or fixed catalog
snapshot. A newly published establishment appears in the queue without a schema
or app release.

## Cover priority

The default app experience never waits for a provider request:

1. Use a published, venue-specific, rights-cleared photo or illustration when
   one exists.
2. Otherwise use the reviewed Paloma-owned template assigned by establishment
   category or family. The current iOS client bundles these templates for an
   immediate list preview; their responsive copies in Supabase Storage support
   other clients and future app releases.
3. Use the deterministic gradient only if an assignment or asset is malformed.
4. Runtime provider photos may supplement future photo galleries under their
   provider's display terms, but they are not copied into the durable catalog,
   used as generation inputs, or allowed to block the base experience.

The iOS establishment-detail header deliberately uses a pinned map instead of
category artwork. Category symbols are reserved for compact discovery surfaces,
where their purpose is immediately readable and they cannot be mistaken for a
picture of a particular venue.

A venue-specific storefront illustration may be created only from a source
manually reviewed as `exact_storefront`. A source reviewed as `exact_building`
or `site_context` may produce only an accurately disclosed building/location
illustration.

Google Maps, Google Street View, Yelp, and establishment-site photos are excluded
from durable ingestion unless Paloma obtains a separate license that explicitly
permits storage, commercial display, and derivatives. Public visibility alone is
not permission.

## State machine

```text
published establishment
  -> source discovery
  -> append-only identity review
  -> private immutable source copy
  -> illustration/photo render
  -> independent quality review
  -> atomic publication
```

The database enforces the gates. Direct updates cannot mark an asset approved or
published. Source identity, rights, prompt hash, input hash, disclosure, and
output license become immutable when the asset is registered. Replacing a cover
publishes a new asset and retires the old record.

## Responsive delivery and caching

Every published cover has three center-cropped 8:5 JPEG variants:

| Variant | Dimensions | Use |
| --- | ---: | --- |
| hero | 1600 × 1000 | Reserved responsive asset; not used by the iOS detail header |
| card | 960 × 600 | Homepage and discovery cards |
| thumbnail | 320 × 200 | Compact rows and previews |

Object paths include content hashes and are never overwritten. Uploads use a
one-year immutable cache directive. The iOS app adds a bounded 256 MB disk cache
for durable Paloma assets; transient provider images remain memory-only.

Image bytes live in Supabase Storage, while Postgres stores the template/asset
assignment, provenance, hashes, dimensions, and public URLs. Storing large image
blobs directly in Postgres would make delivery and caching worse.

## Current baseline rollout

As of 2026-09-03, all 218 published establishments have a durable base-cover
assignment and hero, card, and thumbnail URLs. The v3 baseline has one reviewed,
Paloma-owned, single-symbol illustration for each of the 16 active establishment
types:

| Template | Establishments |
| --- | ---: |
| `category-bar` | 24 |
| `category-beer-bar` | 4 |
| `category-billiards-bar` | 2 |
| `category-brewery` | 8 |
| `category-brewpub` | 5 |
| `category-cocktail-bar` | 43 |
| `category-distillery` | 2 |
| `category-dive-bar` | 48 |
| `category-lounge` | 11 |
| `category-nightclub` | 10 |
| `category-pub` | 11 |
| `category-sports-bar` | 13 |
| `category-taproom` | 8 |
| `category-tasting-room` | 5 |
| `category-wine-bar` | 16 |
| `category-winery` | 8 |

These images are intentionally generic, use a bold silhouette rather than a
detailed scene, and carry the disclosure “Paloma category symbol; not a
photograph or depiction of this establishment.” They solve empty and slow list
previews without pretending that generated architecture is real. Their 48
responsive files use immutable `templates/v3/` object paths; the iOS bundle
contains the same reviewed art so compact rows render without a network wait.
Future published establishments receive their exact category template
automatically. A new type without reviewed category art safely falls back to a
broad v1 family template. A reviewed venue-specific asset atomically replaces
either template assignment.

## Operations

```shell
paloma-data media-discover-batch
paloma-data media-queue
paloma-data media-review-source --source-id ... --verdict exact_storefront --notes ...
paloma-data media-ingest-artwork --establishment-id ... --artwork ... --prompt-file ... --disclosure ...
paloma-data media-approve-asset --asset-id ... --notes ...
paloma-data media-publish-asset --asset-id ...
```

`media-discover-batch` defaults to every published establishment missing a cover.
`--city` is only an optional release-priority filter. The same command can be run
on a schedule; future establishments are picked up from the live queue.

## Supported durable source policies

- Mapillary imagery is admitted under CC BY-SA 4.0, with creator attribution and
  share-alike output licensing retained.
- Wikimedia Commons files are admitted per-file only when their upstream license
  permits commercial use, storage, and derivatives. Noncommercial,
  no-derivatives, and unknown licenses are rejected.
- Openverse may be useful for discovery later, but its license metadata must be
  verified against the original source before the file can enter this pipeline.

Every candidate still requires visual identity review. Distance, name matching,
or camera direction can prioritize review; none of them proves that an image is
the establishment.
