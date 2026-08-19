-- Harden the licensed provider runtime around these four invariants:
--   1. only explicitly reviewed providers may retain payloads;
--   2. identity validation happens before a bounded response is stored;
--   3. concurrent misses collapse to one external request;
--   4. the Edge Function operates through a purpose-built no-login role.

alter table ingest.provider_response_cache
  drop constraint provider_response_cache_check;

alter table ingest.provider_response_cache
  add constraint provider_response_cache_retention_check check (
    expires_at > fetched_at
    and expires_at <= fetched_at + interval '22 hours'
  ),
  add constraint provider_response_cache_payload_size_check check (
    pg_catalog.pg_column_size(payload) <= 262144
  );

create table ingest.provider_match_state (
  establishment_id uuid not null
    references public.establishments(id) on delete cascade,
  provider text not null,
  identity_fingerprint text not null,
  outcome text not null,
  attempted_at timestamptz not null,
  retry_after timestamptz not null,
  lease_token uuid,
  lease_expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (establishment_id, provider),
  check (provider = 'yelp'),
  check (identity_fingerprint ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  check (outcome in ('pending', 'matched', 'not_found', 'rejected', 'error')),
  check (retry_after >= attempted_at),
  check (
    (lease_token is null and lease_expires_at is null)
    or (
      lease_token is not null
      and lease_expires_at is not null
      and lease_expires_at > attempted_at
      and lease_expires_at <= attempted_at + interval '30 seconds'
    )
  )
);

alter table ingest.provider_match_state enable row level security;

revoke all on ingest.provider_match_state
  from public, anon, authenticated, service_role, paloma_ingest;

-- Changing or retiring a durable provider identity invalidates every cached
-- payload under that link. This database trigger closes races with concurrent
-- requests and prevents an old provider ID from inheriting a new identity.
create or replace function ingest.clear_cache_for_changed_provider_link()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if old.provider_place_id is distinct from new.provider_place_id
     or (old.retired_at is null and new.retired_at is not null) then
    delete from ingest.provider_response_cache
    where provider_link_id = new.id;

    delete from ingest.provider_refresh_leases
    where provider_link_id = new.id;
  end if;
  return new;
end;
$$;

revoke all on function ingest.clear_cache_for_changed_provider_link()
  from public, anon, authenticated, service_role, paloma_ingest;

create trigger clear_cache_for_changed_provider_link
after update of provider_place_id, retired_at
on ingest.runtime_provider_links
for each row
execute function ingest.clear_cache_for_changed_provider_link();

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
  delete from ingest.provider_response_cache cache
  where cache.expires_at <= pg_catalog.now()
     or exists (
       select 1
       from ingest.runtime_provider_links runtime_link
       join public.establishments establishment
         on establishment.id = runtime_link.establishment_id
       where runtime_link.id = cache.provider_link_id
         and (
           runtime_link.retired_at is not null
           or establishment.publication_state <> 'published'
           or establishment.status <> 'open'
           or establishment.access_mode <> 'walk_in'
           or establishment.verification_expires_at is null
           or establishment.verification_expires_at <= pg_catalog.now()
         )
     );
  get diagnostics deleted_responses = row_count;

  delete from ingest.provider_refresh_leases lease
  where lease.lease_expires_at <= pg_catalog.now()
     or exists (
       select 1
       from ingest.runtime_provider_links runtime_link
       join public.establishments establishment
         on establishment.id = runtime_link.establishment_id
       where runtime_link.id = lease.provider_link_id
         and (
           runtime_link.retired_at is not null
           or establishment.publication_state <> 'published'
           or establishment.status <> 'open'
           or establishment.access_mode <> 'walk_in'
           or establishment.verification_expires_at is null
           or establishment.verification_expires_at <= pg_catalog.now()
         )
     );
  get diagnostics deleted_leases = row_count;

  update ingest.provider_match_state
  set outcome = 'error',
      retry_after = greatest(retry_after, pg_catalog.now() + interval '5 minutes'),
      lease_token = null,
      lease_expires_at = null,
      updated_at = pg_catalog.now()
  where lease_expires_at <= pg_catalog.now();

  return query select deleted_responses, deleted_leases;
end;
$$;

revoke all on function ingest.purge_expired_provider_runtime()
  from public, anon, authenticated, service_role, paloma_ingest;

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
    '*/15 * * * *',
    'select * from ingest.purge_expired_provider_runtime()'
  );
end;
$schedule$;

do $role$
begin
  create role paloma_runtime nologin noinherit;
exception
  when duplicate_object then null;
end;
$role$;

alter role paloma_runtime nologin noinherit;
grant paloma_runtime to postgres;

grant usage on schema public, ingest, extensions to paloma_runtime;
grant select on public.establishments, public.establishment_settings
  to paloma_runtime;
grant select on ingest.catalog_candidates, ingest.candidate_source_links,
  ingest.source_records
  to paloma_runtime;
grant select, insert, update, delete on ingest.runtime_provider_links,
  ingest.provider_response_cache, ingest.provider_refresh_leases,
  ingest.provider_match_state
  to paloma_runtime;
grant select, insert, update on ingest.live_detail_user_limits,
  ingest.live_detail_global_limit
  to paloma_runtime;
grant usage, select on sequence ingest.runtime_provider_links_id_seq
  to paloma_runtime;

create policy paloma_runtime_read_establishments
  on public.establishments for select to paloma_runtime
  using (
    publication_state = 'published'
    and status = 'open'
    and access_mode = 'walk_in'
    and verification_tier in ('open_evidence', 'provider', 'manual')
    and verification_expires_at > now()
  );

create policy paloma_runtime_read_establishment_settings
  on public.establishment_settings for select to paloma_runtime
  using (
    exists (
      select 1 from public.establishments establishment
      where establishment.id = establishment_id
    )
  );

create policy paloma_runtime_read_catalog_candidates
  on ingest.catalog_candidates for select to paloma_runtime
  using (
    candidate_state in ('verified', 'published')
    and identity_confidence >= 0.96
  );

create policy paloma_runtime_read_candidate_source_links
  on ingest.candidate_source_links for select to paloma_runtime
  using (source = 'fsq' and identity_confidence >= 0.96);

create policy paloma_runtime_manage_provider_links
  on ingest.runtime_provider_links for all to paloma_runtime
  using (
    exists (
      select 1 from public.establishments establishment
      where establishment.id = establishment_id
    )
  )
  with check (
    exists (
      select 1 from public.establishments establishment
      where establishment.id = establishment_id
    )
  );

create policy paloma_runtime_manage_provider_cache
  on ingest.provider_response_cache for all to paloma_runtime
  using (
    exists (
      select 1 from ingest.runtime_provider_links runtime_link
      where runtime_link.id = provider_link_id
        and runtime_link.provider = provider
    )
  )
  with check (
    exists (
      select 1 from ingest.runtime_provider_links runtime_link
      where runtime_link.id = provider_link_id
        and runtime_link.provider = provider
    )
  );

create policy paloma_runtime_manage_provider_refresh_leases
  on ingest.provider_refresh_leases for all to paloma_runtime
  using (
    exists (
      select 1 from ingest.runtime_provider_links runtime_link
      where runtime_link.id = provider_link_id
        and runtime_link.provider = provider
    )
  )
  with check (
    exists (
      select 1 from ingest.runtime_provider_links runtime_link
      where runtime_link.id = provider_link_id
        and runtime_link.provider = provider
    )
  );

create policy paloma_runtime_manage_provider_match_state
  on ingest.provider_match_state for all to paloma_runtime
  using (
    exists (
      select 1 from public.establishments establishment
      where establishment.id = establishment_id
    )
  )
  with check (
    exists (
      select 1 from public.establishments establishment
      where establishment.id = establishment_id
    )
  );

create policy paloma_runtime_manage_live_detail_user_limits
  on ingest.live_detail_user_limits for all to paloma_runtime
  using (true) with check (true);

create policy paloma_runtime_manage_live_detail_global_limit
  on ingest.live_detail_global_limit for all to paloma_runtime
  using (true) with check (true);

comment on table ingest.provider_response_cache is
  'Server-only, identity-validated Yelp response cache with a database-enforced 22-hour maximum lifetime and 256 KiB payload cap.';
comment on table ingest.provider_match_state is
  'Paloma-owned match cooldown and single-flight state; contains no provider response attributes.';
comment on function ingest.purge_expired_provider_runtime() is
  'Deletes expired or ineligible provider payloads and leases every fifteen minutes.';
comment on role paloma_runtime is
  'No-login least-privilege role assumed by the venue-live-details Edge Function.';
