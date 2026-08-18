-- The recurring ingest role must not have general TRUNCATE/DELETE access to user data.
-- This one-shot, fail-closed function gives it exactly the initial pre-launch cutover needed.

create table if not exists ingest.catalog_cutover_control (
  singleton boolean primary key default true check (singleton),
  completed_at timestamptz not null default now(),
  completed_by text not null,
  legacy_rows_removed bigint not null check (legacy_rows_removed >= 0)
);

alter table ingest.catalog_cutover_control enable row level security;
revoke all on ingest.catalog_cutover_control from public, anon, authenticated, paloma_ingest;

create or replace function ingest.reset_legacy_public_catalog(
  p_confirmation text,
  p_minimum_verified integer
)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  verified_count bigint;
  legacy_count bigint;
begin
  if p_confirmation is distinct from 'REPLACE_PUBLIC_CATALOG' then
    raise exception 'catalog cutover confirmation did not match';
  end if;
  if p_minimum_verified is null or p_minimum_verified < 1 then
    raise exception 'catalog cutover minimum must be positive';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtext('paloma_catalog_cutover')
  );

  if exists (select 1 from ingest.catalog_cutover_control) then
    raise exception 'initial catalog cutover has already completed';
  end if;
  if exists (
    select 1 from public.establishments
    where catalog_candidate_id is not null
  ) then
    raise exception 'catalog is not legacy-only; one-time cutover refused';
  end if;

  select count(*)
  into verified_count
  from ingest.catalog_candidates
  where candidate_state in ('verified', 'published')
    and decision_version = 'v6'
    and verification_expires_at > pg_catalog.now();

  if verified_count < p_minimum_verified then
    raise exception 'catalog cutover has % verified candidates; minimum is %',
      verified_count, p_minimum_verified;
  end if;

  select count(*) into legacy_count from public.establishments;

  truncate table
    public.visit_experiences,
    public.visits,
    public.saved_establishments,
    public.comparisons,
    public.plan_members,
    public.plans,
    public.establishment_settings,
    ingest.establishment_field_evidence,
    ingest.establishment_review_queue,
    ingest.establishment_sources,
    public.establishments
  restart identity;

  insert into ingest.catalog_cutover_control (
    singleton, completed_at, completed_by, legacy_rows_removed
  ) values (
    true, pg_catalog.now(), session_user, legacy_count
  );

  return legacy_count;
end;
$$;

revoke all on function ingest.reset_legacy_public_catalog(text, integer)
  from public, anon, authenticated;
grant execute on function ingest.reset_legacy_public_catalog(text, integer)
  to paloma_ingest;

comment on table ingest.catalog_cutover_control is
  'One-row latch proving the destructive pre-launch legacy catalog replacement already ran.';
comment on function ingest.reset_legacy_public_catalog(text, integer) is
  'One-time, transactional pre-launch cutover; refuses repeat use and leaves recurring ingest least-privileged.';
