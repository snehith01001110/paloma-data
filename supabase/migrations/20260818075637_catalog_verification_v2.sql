-- Accuracy-first catalog v2.
--
-- Discovery entities live in ingest.catalog_candidates.  A row is materialized into
-- public.establishments only after a time-bounded verification passes every hard gate.
-- This migration is additive so a small trial can run without changing the current catalog.

alter table ingest.source_records
  add column if not exists origin_keys text[] not null default '{}',
  add column if not exists data_license text not null default 'unknown',
  add column if not exists storage_scope text not null default 'durable',
  add column if not exists provider_veracity smallint,
  add column if not exists last_seen_run_id uuid,
  add column if not exists retired_at timestamptz,
  add column if not exists geocode_matched_address text;

alter table ingest.source_records
  drop constraint if exists source_records_storage_scope_check,
  add constraint source_records_storage_scope_check check (
    storage_scope in ('durable', 'contract', 'ephemeral', 'manual')
  ),
  drop constraint if exists source_records_provider_veracity_check,
  add constraint source_records_provider_veracity_check check (
    provider_veracity is null or provider_veracity between 1 and 5
  );

update ingest.source_records
set origin_keys = case source
      when 'ca_abc' then array['ca_abc']::text[]
      when 'datasf' then array['datasf']::text[]
      when 'fsq' then array['foursquare']::text[]
      when 'overture' then array['overture']::text[]
      when 'osm' then array['openstreetmap']::text[]
      when 'official_web' then array['first_party']::text[]
      else array[source]::text[]
    end,
    data_license = case source
      when 'fsq' then 'Apache-2.0'
      when 'overture' then 'Overture-source-licenses'
      when 'osm' then 'ODbL-1.0'
      when 'ca_abc' then 'California-public-record'
      when 'datasf' then 'DataSF-open-data'
      else data_license
    end
where cardinality(origin_keys) = 0;

create table if not exists ingest.source_sync_state (
  source text primary key,
  last_complete_run_id uuid,
  release_id text,
  cursor_after text,
  completed_at timestamptz,
  record_count bigint not null default 0 check (record_count >= 0),
  metadata jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create table if not exists ingest.neighborhood_boundaries (
  source text not null,
  source_record_id text not null,
  jurisdiction text not null,
  name text not null,
  normalized_name text not null,
  boundary extensions.geometry(multipolygon, 4326) not null,
  authority numeric not null check (authority between 0 and 1),
  data_license text not null,
  source_updated_at timestamptz,
  payload_hash text not null,
  last_seen_run_id uuid,
  retired_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (source, source_record_id),
  check (not ST_IsEmpty(boundary))
);

create table if not exists ingest.catalog_candidates (
  id uuid primary key default gen_random_uuid(),
  anchor_source text not null,
  anchor_source_record_id text not null,
  candidate_state text not null default 'discovered',
  decision_reason text not null default 'not_evaluated:v2',
  decision_reasons text[] not null default '{}',
  decision_version text,
  identity_confidence numeric,
  verification_tier text not null default 'unverified',
  verified_at timestamptz,
  verification_expires_at timestamptz,
  name text not null,
  normalized_name text not null,
  primary_type_slug text,
  address text not null,
  normalized_address text not null,
  city text not null,
  region text,
  postal_code text,
  country_code character(2) not null default 'US',
  location extensions.geography(point, 4326) not null,
  resolved_snapshot jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  last_evaluated_at timestamptz,
  unique (anchor_source, anchor_source_record_id),
  foreign key (anchor_source, anchor_source_record_id)
    references ingest.source_records(source, source_record_id) on delete restrict,
  check (candidate_state in (
    'discovered', 'needs_verification', 'needs_review', 'verified',
    'rejected', 'withdrawn', 'published'
  )),
  check (verification_tier in ('unverified', 'open_evidence', 'provider', 'manual')),
  check (identity_confidence is null or identity_confidence between 0 and 1),
  check (
    (candidate_state not in ('verified', 'published'))
    or (
      verified_at is not null
      and verification_expires_at is not null
      and verification_expires_at > verified_at
    )
  )
);

create table if not exists ingest.candidate_source_links (
  candidate_id uuid not null references ingest.catalog_candidates(id) on delete cascade,
  source text not null,
  source_record_id text not null,
  identity_confidence numeric not null,
  match_method text not null,
  origin_keys text[] not null default '{}',
  linked_at timestamptz not null default now(),
  last_checked_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb,
  primary key (candidate_id, source, source_record_id),
  unique (source, source_record_id),
  foreign key (source, source_record_id)
    references ingest.source_records(source, source_record_id) on delete restrict,
  check (identity_confidence between 0 and 1)
);

create table if not exists ingest.candidate_match_reviews (
  id bigint generated always as identity primary key,
  candidate_id uuid not null references ingest.catalog_candidates(id) on delete cascade,
  source text not null,
  source_record_id text not null,
  reason text not null,
  score numeric,
  evidence jsonb not null default '{}'::jsonb,
  state text not null default 'pending',
  created_at timestamptz not null default now(),
  resolved_at timestamptz,
  foreign key (source, source_record_id)
    references ingest.source_records(source, source_record_id) on delete restrict,
  check (score is null or score between 0 and 1),
  check (state in ('pending', 'accepted', 'rejected', 'superseded'))
);

create unique index if not exists candidate_match_reviews_pending_unique
  on ingest.candidate_match_reviews (candidate_id, source, source_record_id, reason)
  where state = 'pending';

create index if not exists candidate_match_reviews_source_record_idx
  on ingest.candidate_match_reviews (source, source_record_id);

create index if not exists candidate_match_reviews_pending_reason_idx
  on ingest.candidate_match_reviews (reason, created_at desc)
  where state = 'pending';

create table if not exists ingest.candidate_verifications (
  id bigint generated always as identity primary key,
  candidate_id uuid not null references ingest.catalog_candidates(id) on delete cascade,
  verifier text not null,
  verifier_record_id text not null,
  outcome text not null,
  verification_tier text not null,
  checks jsonb not null,
  permitted_snapshot jsonb not null default '{}'::jsonb,
  storage_policy text not null,
  verified_at timestamptz not null,
  expires_at timestamptz not null,
  decision_version text not null,
  created_at timestamptz not null default now(),
  check (outcome in ('pass', 'fail', 'inconclusive')),
  check (verification_tier in ('provider', 'manual')),
  check (storage_policy in ('contract', 'manual')),
  check (expires_at > verified_at)
);

create table if not exists ingest.catalog_evaluations (
  id bigint generated always as identity primary key,
  candidate_id uuid not null references ingest.catalog_candidates(id) on delete cascade,
  evaluation_mode text not null,
  decision text not null,
  reasons text[] not null,
  decision_version text not null,
  snapshot jsonb not null default '{}'::jsonb,
  evaluated_at timestamptz not null default now(),
  check (evaluation_mode in ('trial', 'production')),
  check (decision in (
    'needs_verification', 'needs_review', 'verified', 'rejected', 'withdrawn'
  ))
);

create index if not exists catalog_candidates_state_updated_idx
  on ingest.catalog_candidates (candidate_state, updated_at);

create index if not exists catalog_candidates_location_idx
  on ingest.catalog_candidates using gist (location);

create index if not exists catalog_candidates_name_trgm_idx
  on ingest.catalog_candidates using gin (normalized_name extensions.gin_trgm_ops);

create index if not exists catalog_candidates_address_trgm_idx
  on ingest.catalog_candidates using gin (normalized_address extensions.gin_trgm_ops);

create index if not exists catalog_candidates_verification_due_idx
  on ingest.catalog_candidates (verification_expires_at)
  where candidate_state in ('verified', 'published');

create index if not exists candidate_source_links_candidate_idx
  on ingest.candidate_source_links (candidate_id);

create index if not exists candidate_verifications_candidate_time_idx
  on ingest.candidate_verifications (candidate_id, verified_at desc);

create index if not exists candidate_verifications_expiry_idx
  on ingest.candidate_verifications (expires_at)
  where outcome = 'pass';

create index if not exists catalog_evaluations_candidate_time_idx
  on ingest.catalog_evaluations (candidate_id, evaluated_at desc);

create index if not exists source_records_active_discovery_idx
  on ingest.source_records (source, city, last_seen_at desc)
  where retired_at is null and source_status = 'open' and consumer_facing;

create index if not exists neighborhood_boundaries_boundary_idx
  on ingest.neighborhood_boundaries using gist (boundary);

create index if not exists neighborhood_boundaries_jurisdiction_idx
  on ingest.neighborhood_boundaries (lower(jurisdiction))
  where retired_at is null;

alter table public.establishments
  add column if not exists catalog_candidate_id uuid,
  add column if not exists verification_tier text,
  add column if not exists verification_expires_at timestamptz,
  add column if not exists verification_version text;

alter table public.establishments
  drop constraint if exists establishments_verification_tier_check,
  add constraint establishments_verification_tier_check check (
    verification_tier is null
    or verification_tier in ('open_evidence', 'provider', 'manual')
  );

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'establishments_catalog_candidate_id_fkey'
      and conrelid = 'public.establishments'::regclass
  ) then
    alter table public.establishments
      add constraint establishments_catalog_candidate_id_fkey
      foreign key (catalog_candidate_id)
      references ingest.catalog_candidates(id) on delete restrict;
  end if;
end $$;

create unique index if not exists establishments_catalog_candidate_id_key
  on public.establishments (catalog_candidate_id)
  where catalog_candidate_id is not null;

create index if not exists establishments_verification_expiry_idx
  on public.establishments (verification_expires_at)
  where publication_state = 'published';

-- The custom ingest role uses direct Postgres credentials and does not bypass RLS.
-- New v2 tables are private by default and have an explicit single-role policy.
alter table ingest.source_sync_state enable row level security;
alter table ingest.neighborhood_boundaries enable row level security;
alter table ingest.catalog_candidates enable row level security;
alter table ingest.candidate_source_links enable row level security;
alter table ingest.candidate_match_reviews enable row level security;
alter table ingest.candidate_verifications enable row level security;
alter table ingest.catalog_evaluations enable row level security;

drop policy if exists paloma_ingest_manage_source_sync_state on ingest.source_sync_state;
create policy paloma_ingest_manage_source_sync_state
  on ingest.source_sync_state for all to paloma_ingest using (true) with check (true);

drop policy if exists paloma_ingest_manage_neighborhood_boundaries
  on ingest.neighborhood_boundaries;
create policy paloma_ingest_manage_neighborhood_boundaries
  on ingest.neighborhood_boundaries for all to paloma_ingest using (true) with check (true);

drop policy if exists paloma_ingest_manage_catalog_candidates on ingest.catalog_candidates;
create policy paloma_ingest_manage_catalog_candidates
  on ingest.catalog_candidates for all to paloma_ingest using (true) with check (true);

drop policy if exists paloma_ingest_manage_candidate_source_links
  on ingest.candidate_source_links;
create policy paloma_ingest_manage_candidate_source_links
  on ingest.candidate_source_links for all to paloma_ingest using (true) with check (true);

drop policy if exists paloma_ingest_manage_candidate_match_reviews
  on ingest.candidate_match_reviews;
create policy paloma_ingest_manage_candidate_match_reviews
  on ingest.candidate_match_reviews for all to paloma_ingest using (true) with check (true);

drop policy if exists paloma_ingest_manage_candidate_verifications
  on ingest.candidate_verifications;
create policy paloma_ingest_manage_candidate_verifications
  on ingest.candidate_verifications for all to paloma_ingest using (true) with check (true);

drop policy if exists paloma_ingest_manage_catalog_evaluations
  on ingest.catalog_evaluations;
create policy paloma_ingest_manage_catalog_evaluations
  on ingest.catalog_evaluations for all to paloma_ingest using (true) with check (true);

revoke all on ingest.source_sync_state from public, anon, authenticated;
revoke all on ingest.neighborhood_boundaries from public, anon, authenticated;
revoke all on ingest.catalog_candidates from public, anon, authenticated;
revoke all on ingest.candidate_source_links from public, anon, authenticated;
revoke all on ingest.candidate_match_reviews from public, anon, authenticated;
revoke all on ingest.candidate_verifications from public, anon, authenticated;
revoke all on ingest.catalog_evaluations from public, anon, authenticated;

grant select, insert, update on ingest.source_sync_state to paloma_ingest;
grant select, insert, update, delete on ingest.neighborhood_boundaries to paloma_ingest;
grant select, insert, update on ingest.catalog_candidates to paloma_ingest;
grant select, insert, update, delete on ingest.candidate_source_links to paloma_ingest;
grant select, insert, update on ingest.candidate_match_reviews to paloma_ingest;
grant select, insert on ingest.candidate_verifications to paloma_ingest;
grant select, insert on ingest.catalog_evaluations to paloma_ingest;
grant usage, select on sequence ingest.candidate_match_reviews_id_seq to paloma_ingest;
grant usage, select on sequence ingest.candidate_verifications_id_seq to paloma_ingest;
grant usage, select on sequence ingest.catalog_evaluations_id_seq to paloma_ingest;

comment on table ingest.catalog_candidates is
  'Private discovery/conflation entities. Rows are not consumer catalog establishments.';
comment on table ingest.neighborhood_boundaries is
  'Versioned civic/open boundary evidence used only for deterministic point-in-polygon labels.';
comment on table ingest.candidate_verifications is
  'Immutable, time-bounded verification evidence used by the v2 publication gate.';
comment on column public.establishments.catalog_candidate_id is
  'Private v2 candidate that passed every publication gate and materialized this row.';
comment on column public.establishments.verification_expires_at is
  'After this time the row must be refreshed or withdrawn from consumer reads.';
