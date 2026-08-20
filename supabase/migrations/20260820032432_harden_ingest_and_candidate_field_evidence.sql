-- Defense-in-depth for the five legacy ingestion tables that predate the v2 RLS
-- baseline.  The worker connects as paloma_ingest, so its explicit policy preserves
-- scheduled operation while Data API roles continue to have no privileges or policies.

alter table ingest.source_records enable row level security;
alter table ingest.establishment_sources enable row level security;
alter table ingest.ingestion_runs enable row level security;
alter table ingest.establishment_review_queue enable row level security;
alter table ingest.establishment_field_evidence enable row level security;

drop policy if exists paloma_ingest_manage_source_records
  on ingest.source_records;
create policy paloma_ingest_manage_source_records
  on ingest.source_records for all to paloma_ingest
  using (true) with check (true);

drop policy if exists paloma_ingest_manage_establishment_sources
  on ingest.establishment_sources;
create policy paloma_ingest_manage_establishment_sources
  on ingest.establishment_sources for all to paloma_ingest
  using (true) with check (true);

drop policy if exists paloma_ingest_manage_ingestion_runs
  on ingest.ingestion_runs;
create policy paloma_ingest_manage_ingestion_runs
  on ingest.ingestion_runs for all to paloma_ingest
  using (true) with check (true);

drop policy if exists paloma_ingest_manage_establishment_review_queue
  on ingest.establishment_review_queue;
create policy paloma_ingest_manage_establishment_review_queue
  on ingest.establishment_review_queue for all to paloma_ingest
  using (true) with check (true);

drop policy if exists paloma_ingest_manage_establishment_field_evidence
  on ingest.establishment_field_evidence;
create policy paloma_ingest_manage_establishment_field_evidence
  on ingest.establishment_field_evidence for all to paloma_ingest
  using (true) with check (true);

revoke all on
  ingest.source_records,
  ingest.establishment_sources,
  ingest.ingestion_runs,
  ingest.establishment_review_queue,
  ingest.establishment_field_evidence
from public, anon, authenticated, service_role;

-- A private candidate must be enrichable before publication.  Reuse the durable,
-- rights-enforced observation ledger rather than creating a second evidence system.
-- Exactly one entity scope is populated.  Candidate UUIDs are deliberately reused as
-- establishment UUIDs on first materialization, so the same immutable evidence remains
-- valid after publication without an UPDATE or copy.

alter table catalog.field_observations
  add column candidate_id uuid
    references ingest.catalog_candidates(id) on delete restrict;

alter table catalog.field_observations
  alter column establishment_id drop not null;

alter table catalog.field_observations
  add constraint field_observations_exactly_one_entity_check
  check (num_nonnulls(establishment_id, candidate_id) = 1);

create index field_observations_candidate_field_time_idx
  on catalog.field_observations (candidate_id, field_name, observed_at desc, id)
  where candidate_id is not null;

create index field_observations_candidate_current_idx
  on catalog.field_observations (
    candidate_id, field_name, source, source_record_id, observed_at desc
  )
  where candidate_id is not null and observation_status = 'asserted';

alter table catalog.hours_schedules
  add column candidate_id uuid
    references ingest.catalog_candidates(id) on delete restrict;

alter table catalog.hours_schedules
  alter column establishment_id drop not null;

alter table catalog.hours_schedules
  add constraint hours_schedules_exactly_one_entity_check
  check (num_nonnulls(establishment_id, candidate_id) = 1);

create index hours_schedules_candidate_idx
  on catalog.hours_schedules (candidate_id)
  where candidate_id is not null;

create or replace function catalog.project_normalized_hours()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  interval_item jsonb;
begin
  if new.field_name <> 'hours' or new.observation_status <> 'asserted' then
    return new;
  end if;
  if jsonb_typeof(new.value_json) <> 'object'
     or new.value_json->>'schema_version' <> 'paloma-hours-v1' then
    raise exception 'hours observation must use paloma-hours-v1';
  end if;

  insert into catalog.hours_schedules (
    observation_id, establishment_id, candidate_id, timezone_name,
    schema_version, valid_from, valid_to
  ) values (
    new.id, new.establishment_id, new.candidate_id,
    new.value_json->>'timezone', new.value_json->>'schema_version',
    new.valid_from, new.valid_to
  );

  for interval_item in
    select value
    from jsonb_array_elements(coalesce(new.value_json->'weekly', '[]'::jsonb))
  loop
    insert into catalog.hours_weekly_intervals (
      observation_id, iso_weekday, opens_at, closes_at, closes_day_offset
    ) values (
      new.id,
      (interval_item->>'day')::smallint,
      (interval_item->>'opens')::time,
      (interval_item->>'closes')::time,
      (interval_item->>'closes_day_offset')::smallint
    );
  end loop;

  for interval_item in
    select value
    from jsonb_array_elements(coalesce(new.value_json->'special', '[]'::jsonb))
  loop
    insert into catalog.hours_special_intervals (
      observation_id, service_date, closed, opens_at, closes_at, closes_day_offset
    ) values (
      new.id,
      (interval_item->>'date')::date,
      coalesce((interval_item->>'closed')::boolean, false),
      case when coalesce((interval_item->>'closed')::boolean, false)
           then null else (interval_item->>'opens')::time end,
      case when coalesce((interval_item->>'closed')::boolean, false)
           then null else (interval_item->>'closes')::time end,
      case when coalesce((interval_item->>'closed')::boolean, false)
           then null else (interval_item->>'closes_day_offset')::smallint end
    );
  end loop;
  return new;
end;
$$;

comment on column catalog.field_observations.candidate_id is
  'Private pre-publication entity scope. Exactly one of candidate_id or establishment_id is set.';
comment on column catalog.hours_schedules.candidate_id is
  'Candidate scope inherited from the immutable hours observation.';
