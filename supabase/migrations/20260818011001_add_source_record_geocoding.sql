-- Sources that publish a street address but no coordinates cannot create an establishment,
-- because ingest will not place a venue it cannot put on a map. California ABC is the case that
-- matters: the strongest licence signal Paloma has, with a full address and no latitude.
--
-- Store geocoded coordinates beside the source data rather than in place of it, and remember
-- attempts so an address the geocoder cannot resolve is not retried on every run.

alter table ingest.source_records
  add column if not exists geocode_source text,
  add column if not exists geocoded_at timestamptz,
  add column if not exists geocode_attempted_at timestamptz;

comment on column ingest.source_records.geocode_source is
  'Geocoder that supplied latitude/longitude when the source did not, for example census.';
comment on column ingest.source_records.geocoded_at is
  'When geocoded coordinates were written. Null when coordinates came from the source itself.';
comment on column ingest.source_records.geocode_attempted_at is
  'Last geocode attempt, including failures, so permanent non-matches are not retried each run.';

create index if not exists source_records_missing_coordinates_idx
  on ingest.source_records (source, geocode_attempted_at)
  where latitude is null;
