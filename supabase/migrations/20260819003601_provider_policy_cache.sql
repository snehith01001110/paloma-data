-- Provider identities are durable; licensed API payloads are not catalog data.
--
-- Foursquare PAYG/Sandbox responses must remain uncached. Yelp permits a maximum
-- 24-hour cache, so the database permits Yelp only and enforces a 23-hour ceiling.
-- These constraints are intentionally duplicated in the Edge Function policy code:
-- either layer must fail closed if a future integration is wired incorrectly.

create table ingest.runtime_provider_links (
  id bigint generated always as identity primary key,
  establishment_id uuid not null
    references public.establishments(id) on delete cascade,
  provider text not null,
  provider_place_id text not null,
  match_method text not null,
  match_confidence numeric not null,
  matched_at timestamptz not null default now(),
  last_validated_at timestamptz,
  retired_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (establishment_id, provider),
  unique (provider, provider_place_id),
  unique (id, provider),
  check (provider in ('foursquare', 'yelp')),
  check (length(btrim(provider_place_id)) between 1 and 255),
  check (length(btrim(match_method)) between 1 and 100),
  check (match_confidence between 0 and 1),
  check (last_validated_at is null or last_validated_at >= matched_at),
  check (retired_at is null or retired_at >= matched_at)
);

create table ingest.provider_response_cache (
  provider_link_id bigint not null,
  provider text not null,
  endpoint text not null,
  request_fingerprint text not null,
  payload jsonb not null,
  fetched_at timestamptz not null,
  expires_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (provider_link_id, endpoint, request_fingerprint),
  foreign key (provider_link_id, provider)
    references ingest.runtime_provider_links(id, provider) on delete cascade,
  -- Yelp is the only currently approved server-cache provider. Adding another
  -- provider requires a reviewed migration, not merely an application-code edit.
  check (provider = 'yelp'),
  check (endpoint ~ '^[a-z][a-z0-9_]{0,63}$'),
  check (request_fingerprint ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  check (jsonb_typeof(payload) = 'object'),
  check (
    expires_at > fetched_at
    and expires_at <= fetched_at + interval '23 hours'
  )
);

create table ingest.provider_refresh_leases (
  provider_link_id bigint not null,
  provider text not null,
  endpoint text not null,
  request_fingerprint text not null,
  lease_token uuid not null,
  lease_expires_at timestamptz not null,
  updated_at timestamptz not null default now(),
  primary key (provider_link_id, endpoint, request_fingerprint),
  foreign key (provider_link_id, provider)
    references ingest.runtime_provider_links(id, provider) on delete cascade,
  check (provider = 'yelp'),
  check (endpoint ~ '^[a-z][a-z0-9_]{0,63}$'),
  check (request_fingerprint ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  check (
    lease_expires_at > updated_at
    and lease_expires_at <= updated_at + interval '30 seconds'
  )
);

create index provider_response_cache_expiry_idx
  on ingest.provider_response_cache (expires_at);

create index provider_refresh_leases_expiry_idx
  on ingest.provider_refresh_leases (lease_expires_at);

alter table ingest.runtime_provider_links enable row level security;
alter table ingest.provider_response_cache enable row level security;
alter table ingest.provider_refresh_leases enable row level security;

revoke all on ingest.runtime_provider_links
  from public, anon, authenticated, service_role, paloma_ingest;
revoke all on ingest.provider_response_cache
  from public, anon, authenticated, service_role, paloma_ingest;
revoke all on ingest.provider_refresh_leases
  from public, anon, authenticated, service_role, paloma_ingest;
revoke all on sequence ingest.runtime_provider_links_id_seq
  from public, anon, authenticated, service_role, paloma_ingest;

-- The recurring catalog job may maintain provider identifiers, but it cannot
-- read or write proprietary cached payloads or refresh leases.
create policy paloma_ingest_manage_runtime_provider_links
  on ingest.runtime_provider_links for all to paloma_ingest
  using (true) with check (true);

grant select, insert, update, delete on ingest.runtime_provider_links
  to paloma_ingest;
grant usage, select on sequence ingest.runtime_provider_links_id_seq
  to paloma_ingest;

-- Seed the durable Foursquare place IDs already proven by the publication gate.
-- Duplicate provider IDs are skipped rather than silently assigning one provider
-- identity to multiple Paloma establishments.
with ranked_links as (
  select
    e.id as establishment_id,
    csl.source_record_id as provider_place_id,
    csl.match_method,
    csl.identity_confidence,
    csl.linked_at,
    csl.last_checked_at,
    row_number() over (
      partition by e.id
      order by
        (candidate.anchor_source = 'fsq'
          and candidate.anchor_source_record_id = csl.source_record_id) desc,
        csl.identity_confidence desc,
        csl.last_checked_at desc
    ) as establishment_rank,
    row_number() over (
      partition by csl.source_record_id
      order by csl.identity_confidence desc, csl.last_checked_at desc, e.id
    ) as provider_rank
  from public.establishments e
  join ingest.catalog_candidates candidate
    on candidate.id = e.catalog_candidate_id
  join ingest.candidate_source_links csl
    on csl.candidate_id = candidate.id
   and csl.source = 'fsq'
   and csl.identity_confidence >= 0.96
  join ingest.source_records source_record
    on source_record.source = csl.source
   and source_record.source_record_id = csl.source_record_id
  where e.publication_state = 'published'
    and e.status = 'open'
    and e.verification_expires_at > now()
    and source_record.retired_at is null
    and source_record.source_status = 'open'
    and source_record.consumer_facing
    and source_record.public_access = 'walk_in'
    and not (source_record.quality_flags && array[
      'closed', 'delete', 'doesnt_exist', 'does_not_exist',
      'duplicate', 'inappropriate', 'privatevenue', 'private_venue'
    ]::text[])
)
insert into ingest.runtime_provider_links (
  establishment_id, provider, provider_place_id, match_method,
  match_confidence, matched_at, last_validated_at
)
select
  establishment_id, 'foursquare', provider_place_id, match_method,
  identity_confidence, linked_at, last_checked_at
from ranked_links
where establishment_rank = 1 and provider_rank = 1
on conflict do nothing;

create or replace function ingest.purge_expired_provider_runtime()
returns table(response_rows_deleted bigint, lease_rows_deleted bigint)
language plpgsql
security invoker
set search_path = ''
as $$
declare
  deleted_responses bigint;
  deleted_leases bigint;
begin
  delete from ingest.provider_response_cache
  where expires_at <= pg_catalog.now();
  get diagnostics deleted_responses = row_count;

  delete from ingest.provider_refresh_leases
  where lease_expires_at <= pg_catalog.now();
  get diagnostics deleted_leases = row_count;

  return query select deleted_responses, deleted_leases;
end;
$$;

revoke all on function ingest.purge_expired_provider_runtime()
  from public, anon, authenticated, service_role, paloma_ingest;

create extension if not exists pg_cron;

do $schedule$
declare
  existing_job_id bigint;
begin
  select jobid into existing_job_id
  from cron.job
  where jobname = 'paloma-provider-cache-purge';

  if existing_job_id is not null then
    perform cron.unschedule(existing_job_id);
  end if;

  perform cron.schedule(
    'paloma-provider-cache-purge',
    '7 * * * *',
    'select * from ingest.purge_expired_provider_runtime()'
  );
end;
$schedule$;

comment on table ingest.runtime_provider_links is
  'Durable provider identifiers and Paloma-owned match metadata only; never provider attributes.';
comment on table ingest.provider_response_cache is
  'Server-only raw Yelp response cache with a database-enforced 23-hour maximum lifetime.';
comment on table ingest.provider_refresh_leases is
  'Short-lived single-flight leases preventing duplicate provider calls on a cold cache key.';
comment on function ingest.purge_expired_provider_runtime() is
  'Hard-deletes expired provider payloads and abandoned refresh leases; also scheduled hourly.';
