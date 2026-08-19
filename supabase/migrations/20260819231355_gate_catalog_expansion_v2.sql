-- New identities may enter the public catalog only through an immutable, bounded release.
-- Existing identities keep refreshing normally; the trigger distinguishes them by primary key.

create table governance.catalog_expansion_release_events (
  id bigint generated always as identity primary key,
  release_id text not null,
  event_type text not null,
  manifest_sha256 character(64) not null,
  scope_cities text[] not null default '{}',
  maximum_new_publications integer,
  baseline_publications integer,
  required_source_freshness_days jsonb not null default '{}'::jsonb,
  decision_version text,
  minimum_healthy_refresh_weeks integer,
  refresh_history_days integer,
  maximum_latest_refresh_age_hours integer,
  failed_run_lookback_days integer,
  terms_version text
    references governance.contribution_terms_versions(version) on delete restrict,
  coverage_snapshot jsonb not null default '{}'::jsonb,
  coverage_accepted_by text,
  coverage_accepted_at timestamptz,
  actor text not null,
  reason text not null,
  effective_at timestamptz not null default now(),
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  check (release_id ~ '^[a-z][a-z0-9-]{2,99}$'),
  check (event_type in ('approved','revoked')),
  check (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  check (jsonb_typeof(required_source_freshness_days) = 'object'),
  check (jsonb_typeof(coverage_snapshot) = 'object'),
  check (length(actor) between 1 and 200),
  check (length(reason) between 1 and 2000),
  check (expires_at is null or expires_at > effective_at),
  check (
    event_type = 'revoked'
    or (
      cardinality(scope_cities) > 0
      and maximum_new_publications between 1 and 500
      and baseline_publications >= 0
      and decision_version ~ '^v[0-9]+$'
      and minimum_healthy_refresh_weeks between 2 and 8
      and refresh_history_days between 14 and 90
      and maximum_latest_refresh_age_hours between 12 and 168
      and failed_run_lookback_days between 7 and 90
      and terms_version is not null
      and coverage_snapshot <> '{}'::jsonb
      and coverage_accepted_by is not null
      and coverage_accepted_at is not null
      and expires_at is not null
    )
  )
);

create index catalog_expansion_release_events_latest_idx
  on governance.catalog_expansion_release_events (release_id, id desc);

alter table public.establishments
  add column expansion_release_id text,
  add column expansion_manifest_sha256 character(64),
  add constraint establishments_expansion_manifest_hash_check
    check (
      expansion_manifest_sha256 is null
      or expansion_manifest_sha256 ~ '^[0-9a-f]{64}$'
    ),
  add constraint establishments_expansion_attribution_check
    check (
      (expansion_release_id is null and expansion_manifest_sha256 is null)
      or (expansion_release_id is not null and expansion_manifest_sha256 is not null)
    );

create index establishments_expansion_release_idx
  on public.establishments (expansion_release_id, published_at)
  where expansion_release_id is not null;

create or replace function governance.validate_catalog_expansion_release_event()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_live_publications integer;
begin
  if new.event_type = 'approved' then
    select count(*)::integer into v_live_publications
    from public.establishments
    where publication_state = 'published' and status = 'open';

    if new.baseline_publications is distinct from v_live_publications then
      raise exception 'baseline_publications must equal current live count %',
        v_live_publications;
    end if;
    if new.coverage_accepted_at > now() + interval '5 minutes' then
      raise exception 'coverage acceptance cannot be in the future';
    end if;
    if exists (
      select 1
      from unnest(new.scope_cities) as city
      group by lower(trim(city))
      having count(*) > 1 or lower(trim(city)) = ''
    ) then
      raise exception 'scope cities must be non-empty and case-insensitively unique';
    end if;
    if exists (
      select 1
      from jsonb_each_text(new.required_source_freshness_days) as source(name, days)
      where name !~ '^[a-z][a-z0-9_]{1,63}$'
         or days !~ '^[1-9][0-9]{0,2}$'
         or days::integer > 365
    ) then
      raise exception 'source freshness policy is invalid';
    end if;
  end if;
  return new;
end;
$$;

create trigger validate_catalog_expansion_release_event
before insert on governance.catalog_expansion_release_events
for each row execute function governance.validate_catalog_expansion_release_event();

create trigger catalog_expansion_release_events_append_only
before update or delete on governance.catalog_expansion_release_events
for each row execute function governance.reject_append_only_mutation();

create or replace function governance.catalog_expansion_status(
  p_release_id text,
  p_manifest_sha256 text,
  p_expected_cities text[],
  p_expected_maximum integer,
  p_required_source_freshness_days jsonb,
  p_decision_version text,
  p_minimum_healthy_refresh_weeks integer,
  p_refresh_history_days integer,
  p_maximum_latest_refresh_age_hours integer,
  p_failed_run_lookback_days integer
)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_event governance.catalog_expansion_release_events%rowtype;
  v_blockers jsonb := '[]'::jsonb;
  v_live_publications integer := 0;
  v_release_publications integer := 0;
  v_expired_publications integer := 0;
  v_stale_decisions integer := 0;
  v_pending_field_conflicts integer := 0;
  v_pending_identity_conflicts integer := 0;
  v_dead_jobs integer := 0;
  v_failed_runs integer := 0;
  v_healthy_refresh_weeks integer := 0;
  v_latest_refresh_at timestamptz;
  v_source text;
  v_days_text text;
  v_source_completed_at timestamptz;
  v_available_slots integer := 0;
begin
  if p_release_id is null or p_manifest_sha256 !~ '^[0-9a-f]{64}$'
     or cardinality(p_expected_cities) is null
     or cardinality(p_expected_cities) = 0
     or p_expected_maximum not between 1 and 500
     or jsonb_typeof(p_required_source_freshness_days) <> 'object' then
    raise exception 'invalid expansion status request';
  end if;

  select * into v_event
  from governance.catalog_expansion_release_events
  where release_id = p_release_id
  order by id desc
  limit 1;

  if not found then
    v_blockers := v_blockers || jsonb_build_array('authorization_missing');
  else
    if v_event.event_type <> 'approved' then
      v_blockers := v_blockers || jsonb_build_array('authorization_revoked');
    end if;
    if v_event.manifest_sha256 <> p_manifest_sha256 then
      v_blockers := v_blockers || jsonb_build_array('manifest_hash_mismatch');
    end if;
    if (
      select array_agg(lower(city) order by lower(city))
      from unnest(v_event.scope_cities) as city
    ) is distinct from (
      select array_agg(lower(city) order by lower(city))
      from unnest(p_expected_cities) as city
    ) or v_event.maximum_new_publications is distinct from p_expected_maximum then
      v_blockers := v_blockers || jsonb_build_array('release_scope_mismatch');
    end if;
    if v_event.required_source_freshness_days
         is distinct from p_required_source_freshness_days
       or v_event.decision_version is distinct from p_decision_version
       or v_event.minimum_healthy_refresh_weeks
         is distinct from p_minimum_healthy_refresh_weeks
       or v_event.refresh_history_days is distinct from p_refresh_history_days
       or v_event.maximum_latest_refresh_age_hours
         is distinct from p_maximum_latest_refresh_age_hours
       or v_event.failed_run_lookback_days
         is distinct from p_failed_run_lookback_days then
      v_blockers := v_blockers || jsonb_build_array('health_policy_mismatch');
    end if;
    if v_event.effective_at > now() then
      v_blockers := v_blockers || jsonb_build_array('authorization_not_effective');
    end if;
    if v_event.expires_at is null or v_event.expires_at <= now() then
      v_blockers := v_blockers || jsonb_build_array('authorization_expired');
    end if;
    if v_event.coverage_accepted_at is null or v_event.coverage_snapshot = '{}'::jsonb then
      v_blockers := v_blockers || jsonb_build_array('coverage_not_accepted');
    end if;
    if not exists (
      select 1
      from governance.contribution_terms_versions as terms
      where terms.version = v_event.terms_version
        and terms.state = 'active'
        and terms.effective_from <= now()
        and (terms.retired_at is null or terms.retired_at > now())
    ) then
      v_blockers := v_blockers || jsonb_build_array('terms_not_active');
    end if;
  end if;

  select
    count(*) filter (where publication_state = 'published' and status = 'open')::integer,
    count(*) filter (
      where publication_state = 'published'
        and (verification_expires_at is null or verification_expires_at <= now())
    )::integer,
    count(*) filter (
      where publication_state = 'published'
        and verification_version is distinct from p_decision_version
    )::integer,
    count(*) filter (
      where expansion_release_id = p_release_id and publication_state = 'published'
    )::integer
  into v_live_publications, v_expired_publications, v_stale_decisions,
       v_release_publications
  from public.establishments;

  select count(*)::integer into v_pending_field_conflicts
  from review.field_conflicts as conflict
  join public.establishments as establishment on establishment.id = conflict.establishment_id
  where conflict.state = 'pending'
    and establishment.publication_state = 'published';

  select count(*)::integer into v_pending_identity_conflicts
  from ingest.catalog_candidates as candidate
  join public.establishments as establishment
    on establishment.catalog_candidate_id = candidate.id
  join ingest.candidate_match_reviews as review on review.candidate_id = candidate.id
  join ingest.source_records as source_record
    on source_record.source = review.source
   and source_record.source_record_id = review.source_record_id
  where establishment.publication_state = 'published'
    and review.state = 'pending'
    and source_record.retired_at is null
    and source_record.source_status = 'open'
    and not (
      source_record.quality_flags && array[
        'closed','delete','doesnt_exist','does_not_exist','duplicate','inappropriate',
        'privatevenue','private_venue','stale','consumer_identity_conflict'
      ]::text[]
    )
    and source_record.normalized_address = candidate.normalized_address
    and (
      review.reason like '%same_location_name_conflict'
      or review.reason like '%probable_identity_needs_review'
    );

  select count(*)::integer into v_dead_jobs
  from ingest.pipeline_jobs
  where state = 'dead'
    and finished_at >= now() - make_interval(days => p_failed_run_lookback_days);

  select count(*)::integer into v_failed_runs
  from ingest.pipeline_runs
  where state in ('partial','failed')
    and created_at >= now() - make_interval(days => p_failed_run_lookback_days);

  select
    count(distinct date_trunc('week', finished_at at time zone 'UTC'))::integer,
    max(finished_at)
  into v_healthy_refresh_weeks, v_latest_refresh_at
  from ingest.pipeline_runs
  where run_type = 'catalog_refresh'
    and state = 'succeeded'
    and dead_count = 0
    and job_count >= coalesce(v_event.baseline_publications, v_live_publications)
    and metadata->>'decision_version' = p_decision_version
    and finished_at >= now() - make_interval(days => p_refresh_history_days);

  if v_expired_publications > 0 then
    v_blockers := v_blockers || jsonb_build_array('published_verification_expired');
  end if;
  if v_stale_decisions > 0 then
    v_blockers := v_blockers || jsonb_build_array('published_decision_version_stale');
  end if;
  if v_pending_field_conflicts > 0 then
    v_blockers := v_blockers || jsonb_build_array('published_field_conflicts_pending');
  end if;
  if v_pending_identity_conflicts > 0 then
    v_blockers := v_blockers || jsonb_build_array('published_identity_conflicts_pending');
  end if;
  if v_dead_jobs > 0 then
    v_blockers := v_blockers || jsonb_build_array('recent_dead_jobs');
  end if;
  if v_failed_runs > 0 then
    v_blockers := v_blockers || jsonb_build_array('recent_failed_runs');
  end if;
  if v_healthy_refresh_weeks < p_minimum_healthy_refresh_weeks then
    v_blockers := v_blockers || jsonb_build_array('insufficient_healthy_refresh_weeks');
  end if;
  if v_latest_refresh_at is null
     or v_latest_refresh_at < now() - make_interval(hours => p_maximum_latest_refresh_age_hours)
  then
    v_blockers := v_blockers || jsonb_build_array('latest_refresh_stale');
  end if;

  for v_source, v_days_text in
    select key, value
    from jsonb_each_text(p_required_source_freshness_days)
    order by key
  loop
    select completed_at into v_source_completed_at
    from ingest.source_sync_state
    where source = v_source;
    if v_source_completed_at is null
       or v_source_completed_at < now() - make_interval(days => v_days_text::integer)
    then
      v_blockers := v_blockers || jsonb_build_array('source_stale:' || v_source);
    end if;
  end loop;

  if v_event.id is not null and v_event.event_type = 'approved' then
    v_available_slots := greatest(
      coalesce(v_event.maximum_new_publications, 0) - v_release_publications,
      0
    );
  end if;
  if v_available_slots = 0 then
    v_blockers := v_blockers || jsonb_build_array('release_capacity_exhausted');
  end if;

  return jsonb_build_object(
    'release_id', p_release_id,
    'ready', jsonb_array_length(v_blockers) = 0,
    'blockers', v_blockers,
    'authorization_event_id', v_event.id,
    'authorization_event_type', v_event.event_type,
    'authorization_expires_at', v_event.expires_at,
    'terms_version', v_event.terms_version,
    'live_publications', v_live_publications,
    'release_publications', v_release_publications,
    'authorized_limit', coalesce(v_event.maximum_new_publications, p_expected_maximum),
    'available_slots', v_available_slots,
    'health', jsonb_build_object(
      'expired_publications', v_expired_publications,
      'stale_decisions', v_stale_decisions,
      'pending_field_conflicts', v_pending_field_conflicts,
      'pending_identity_conflicts', v_pending_identity_conflicts,
      'dead_jobs', v_dead_jobs,
      'failed_runs', v_failed_runs,
      'healthy_refresh_weeks', v_healthy_refresh_weeks,
      'latest_refresh_at', v_latest_refresh_at
    )
  );
end;
$$;

create or replace function governance.enforce_catalog_expansion_release()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_release_id text;
  v_manifest_sha256 text;
  v_event governance.catalog_expansion_release_events%rowtype;
  v_status jsonb;
begin
  -- INSERT ... ON CONFLICT also fires BEFORE INSERT. Existing IDs are maintenance, not expansion.
  if exists (select 1 from public.establishments where id = new.id) then
    return new;
  end if;

  v_release_id := nullif(current_setting('paloma.expansion_release_id', true), '');
  v_manifest_sha256 := nullif(
    current_setting('paloma.expansion_manifest_sha256', true),
    ''
  );
  if v_release_id is null or v_manifest_sha256 is null then
    raise exception 'new catalog publication requires an armed expansion release';
  end if;

  perform pg_advisory_xact_lock(hashtext('paloma_catalog_expansion:' || v_release_id));
  select * into v_event
  from governance.catalog_expansion_release_events
  where release_id = v_release_id
  order by id desc
  limit 1;
  if not found then
    raise exception 'expansion release % does not exist', v_release_id;
  end if;

  v_status := governance.catalog_expansion_status(
    v_event.release_id,
    v_manifest_sha256,
    v_event.scope_cities,
    v_event.maximum_new_publications,
    v_event.required_source_freshness_days,
    v_event.decision_version,
    v_event.minimum_healthy_refresh_weeks,
    v_event.refresh_history_days,
    v_event.maximum_latest_refresh_age_hours,
    v_event.failed_run_lookback_days
  );
  if not coalesce((v_status->>'ready')::boolean, false) then
    raise exception 'expansion release % is blocked', v_release_id
      using detail = coalesce(v_status->'blockers', '[]'::jsonb)::text;
  end if;
  if not lower(new.city) = any(
    array(select lower(city) from unnest(v_event.scope_cities) as city)
  ) then
    raise exception 'city % is outside expansion release %', new.city, v_release_id;
  end if;

  new.expansion_release_id := v_release_id;
  new.expansion_manifest_sha256 := v_manifest_sha256;
  return new;
end;
$$;

create trigger enforce_catalog_expansion_release
before insert on public.establishments
for each row execute function governance.enforce_catalog_expansion_release();

create or replace function governance.preserve_catalog_expansion_attribution()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if new.expansion_release_id is distinct from old.expansion_release_id
     or new.expansion_manifest_sha256 is distinct from old.expansion_manifest_sha256 then
    raise exception 'catalog expansion attribution is immutable';
  end if;
  return new;
end;
$$;

create trigger preserve_catalog_expansion_attribution
before update of expansion_release_id, expansion_manifest_sha256
on public.establishments
for each row execute function governance.preserve_catalog_expansion_attribution();

alter table governance.catalog_expansion_release_events enable row level security;

revoke all on governance.catalog_expansion_release_events
  from public, anon, authenticated, service_role, paloma_ingest;
revoke all on sequence governance.catalog_expansion_release_events_id_seq
  from public, anon, authenticated, service_role, paloma_ingest;
revoke execute on function governance.validate_catalog_expansion_release_event()
  from public, anon, authenticated, service_role, paloma_ingest;
revoke execute on function governance.catalog_expansion_status(
  text, text, text[], integer, jsonb, text, integer, integer, integer, integer
) from public, anon, authenticated, service_role, paloma_ingest;
revoke execute on function governance.enforce_catalog_expansion_release()
  from public, anon, authenticated, service_role, paloma_ingest;
revoke execute on function governance.preserve_catalog_expansion_attribution()
  from public, anon, authenticated, service_role, paloma_ingest;

grant select on governance.catalog_expansion_release_events to paloma_ingest;
grant execute on function governance.catalog_expansion_status(
  text, text, text[], integer, jsonb, text, integer, integer, integer, integer
) to paloma_ingest;

create policy paloma_ingest_read_catalog_expansion_release_events
  on governance.catalog_expansion_release_events
  for select to paloma_ingest using (true);

comment on table governance.catalog_expansion_release_events is
  'Immutable owner approvals and revocations for bounded public-catalog expansion releases.';
comment on column public.establishments.expansion_release_id is
  'Immutable release attribution for establishments published after the initial cohort.';
