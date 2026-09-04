-- Durable hours are useful only while the exact observation that supports them is current.
-- Keep that lease and a safe first-party reference on the public projection so clients can
-- fail over to licensed runtime providers without reading the private evidence ledger.

alter table public.establishments
  add column if not exists hours_verified_at timestamptz,
  add column if not exists hours_expires_at timestamptz,
  add column if not exists hours_source_url text,
  add column if not exists hours_source_kind text;

-- Reconstruct provenance for existing durable schedules from the strongest matching active
-- observation. Rows without current evidence fail closed instead of receiving invented dates.
with matching_observation as (
  select distinct on (establishment.id)
    establishment.id as establishment_id,
    observation.observed_at,
    coalesce(
      observation.expires_at,
      observation.observed_at + interval '90 days'
    ) as expires_at,
    case
      when observation.metadata->>'evidence_kind' = 'first_party'
        or exists (
          select 1
          from jsonb_array_elements(observation.source_items) item
          where item->>'kind' = 'first_party'
        ) then 'first_party'
      when observation.source = 'merchant' then 'merchant'
      when observation.source = 'firsthand' then 'firsthand'
      when observation.source = 'manual' then 'manual_review'
      when observation.source in (
        'ca_abc', 'datasf', 'datasf_neighborhoods', 'fsq', 'osm', 'overture', 'wikidata'
      ) then 'open_data'
      else 'other'
    end as source_kind,
    source_link.url as source_url
  from public.establishments establishment
  join catalog.current_field_decisions decision
    on decision.establishment_id = establishment.id
   and decision.field_name = 'hours'
   and decision.decision_status = 'selected'
  join catalog.field_observations observation
    on observation.id = any(decision.evidence_ids)
   and observation.field_name = 'hours'
   and observation.observation_status = 'asserted'
   and (observation.expires_at is null or observation.expires_at > now())
   and observation.value_json = establishment.hours
  left join lateral (
    select item->>'url' as url
    from jsonb_array_elements(observation.source_items) item
    where item->>'url' ~ '^https://[^[:space:]]+$'
    order by (item->>'kind' = 'first_party') desc, item->>'url'
    limit 1
  ) source_link on true
  where establishment.hours is not null
  order by establishment.id,
    (observation.metadata->>'evidence_kind' = 'first_party') desc,
    (observation.source = 'merchant') desc,
    observation.authority desc,
    observation.evidence_confidence desc,
    observation.observed_at desc,
    observation.id desc
)
update public.establishments establishment
set hours_verified_at = observation.observed_at,
    hours_expires_at = observation.expires_at,
    hours_source_url = observation.source_url,
    hours_source_kind = observation.source_kind
from matching_observation observation
where observation.establishment_id = establishment.id;

update public.establishments
set hours = null,
    hours_source = null,
    hours_confidence = null,
    hours_verified_at = null,
    hours_expires_at = null,
    hours_source_url = null,
    hours_source_kind = null
where hours is not null
  and (
    hours_verified_at is null
    or hours_expires_at is null
    or hours_expires_at <= now()
  );

alter table public.establishments
  drop constraint if exists establishments_hours_freshness_check,
  add constraint establishments_hours_freshness_check check (
    (
      hours is null
      and hours_verified_at is null
      and hours_expires_at is null
      and hours_source_url is null
      and hours_source_kind is null
    )
    or (
      hours is not null
      and hours_verified_at is not null
      and hours_expires_at is not null
      and hours_expires_at > hours_verified_at
      and hours_source_kind is not null
    )
  ),
  drop constraint if exists establishments_hours_source_kind_check,
  add constraint establishments_hours_source_kind_check check (
    hours_source_kind is null or hours_source_kind in (
      'first_party', 'merchant', 'firsthand', 'open_data', 'manual_review', 'other'
    )
  ),
  drop constraint if exists establishments_hours_source_url_check,
  add constraint establishments_hours_source_url_check check (
    hours_source_url is null or hours_source_url ~ '^https://[^[:space:]]+$'
  );

create index if not exists establishments_hours_reverification_idx
  on public.establishments (hours_expires_at, id)
  where publication_state = 'published' and status = 'open' and hours is not null;

create or replace view review.hours_verification_queue
with (security_invoker = true) as
select
  establishment.id as establishment_id,
  establishment.name,
  establishment.address,
  establishment.city,
  establishment.region,
  establishment.hours_verified_at,
  establishment.hours_expires_at,
  establishment.hours_source_url,
  establishment.hours_source_kind,
  case
    when establishment.hours is null then 'missing'
    when establishment.hours_expires_at <= now() then 'expired'
    else 'due_soon'
  end as reason,
  case
    when establishment.hours_expires_at <= now() then 100
    when establishment.hours is null then 70
    else 85
  end::smallint as priority
from public.establishments establishment
where establishment.publication_state = 'published'
  and establishment.status = 'open'
  and (
    establishment.hours is null
    or establishment.hours_expires_at is null
    or establishment.hours_expires_at <= now() + interval '14 days'
  );

revoke all on review.hours_verification_queue
  from public, anon, authenticated, service_role, paloma_ingest;
grant select on review.hours_verification_queue to paloma_ingest;

comment on column public.establishments.hours_verified_at is
  'When a reviewer or admissible source last verified the projected weekly and special schedule.';
comment on column public.establishments.hours_expires_at is
  'Hard freshness boundary. Clients must not treat durable hours as current at or after this time.';
comment on column public.establishments.hours_source_url is
  'Optional HTTPS evidence page for the selected normalized schedule; never a provider payload URL.';
comment on column public.establishments.hours_source_kind is
  'Rights-safe provenance class for the selected normalized schedule.';
comment on view review.hours_verification_queue is
  'Private prioritized queue for missing and soon-to-expire durable schedules.';
