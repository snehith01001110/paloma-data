-- Provenance-aware directory attributes. Missing or weak claims remain null.
alter table ingest.source_records
  add column if not exists neighborhood text,
  add column if not exists hours jsonb,
  add column if not exists price_level smallint,
  add column if not exists setting_slugs text[] not null default '{}';

alter table ingest.source_records
  drop constraint if exists source_records_price_level_check,
  add constraint source_records_price_level_check check (
    price_level is null or price_level between 1 and 4
  );

alter table ingest.establishment_field_evidence
  add column if not exists value_json jsonb;

alter table ingest.establishment_field_evidence
  drop constraint if exists establishment_field_evidence_field_name_check,
  add constraint establishment_field_evidence_field_name_check check (
    field_name = any (array[
      'display_name', 'legal_name', 'address', 'phone_e164', 'website_url',
      'primary_type_slug', 'status', 'latitude', 'longitude', 'neighborhood',
      'hours', 'price_level', 'setting_slug'
    ]::text[])
  );

alter table public.establishments
  add column if not exists phone_source text,
  add column if not exists phone_confidence numeric,
  add column if not exists neighborhood_source text,
  add column if not exists neighborhood_confidence numeric,
  add column if not exists hours_source text,
  add column if not exists hours_confidence numeric,
  add column if not exists price_source text,
  add column if not exists price_confidence numeric;

alter table public.establishments
  drop constraint if exists establishments_phone_confidence_check,
  add constraint establishments_phone_confidence_check check (
    phone_confidence is null or phone_confidence between 0 and 1
  ),
  drop constraint if exists establishments_neighborhood_confidence_check,
  add constraint establishments_neighborhood_confidence_check check (
    neighborhood_confidence is null or neighborhood_confidence between 0 and 1
  ),
  drop constraint if exists establishments_hours_confidence_check,
  add constraint establishments_hours_confidence_check check (
    hours_confidence is null or hours_confidence between 0 and 1
  ),
  drop constraint if exists establishments_price_confidence_check,
  add constraint establishments_price_confidence_check check (
    price_confidence is null or price_confidence between 0 and 1
  );

alter table public.establishment_settings
  add column if not exists source text not null default 'manual',
  add column if not exists confidence numeric,
  add column if not exists last_verified_at timestamptz;

alter table public.establishment_settings
  drop constraint if exists establishment_settings_confidence_check,
  add constraint establishment_settings_confidence_check check (
    confidence is null or confidence between 0 and 1
  );

create index if not exists establishment_sources_establishment_idx
  on ingest.establishment_sources (establishment_id);

create index if not exists establishment_field_evidence_lookup_idx
  on ingest.establishment_field_evidence (establishment_id, field_name);

create index if not exists establishment_field_evidence_source_idx
  on ingest.establishment_field_evidence (source, field_name);

create index if not exists establishments_primary_type_idx
  on public.establishments (primary_type_id);

create index if not exists establishment_settings_setting_idx
  on public.establishment_settings (setting_id);

comment on column public.establishments.price_level is
  'Resolved 1-4 price claim. Null means Paloma has no trustworthy current claim.';
comment on column public.establishments.hours is
  'Resolved provider-native hours JSON. Clients must treat it as directory data, not a live-open guarantee.';
comment on column public.establishment_settings.source is
  'manual for curated rows; otherwise the selected machine-readable evidence source.';
