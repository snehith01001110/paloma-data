-- Rights-aware, append-only establishment evidence.
--
-- Durable observations are admitted only when a versioned source/field policy permits
-- persistence and canonical derivation. Restricted provider payloads are moved to an isolated
-- runtime schema and can never satisfy this gate.

create schema if not exists governance;
create schema if not exists catalog;
create schema if not exists review;
create schema if not exists runtime;

revoke all on schema governance, catalog, review, runtime
  from public, anon, authenticated, service_role;
grant usage on schema governance, catalog, review to paloma_ingest;

alter table ingest.source_records
  add column if not exists field_provenance jsonb not null default '{}'::jsonb;

alter table public.establishments
  add column if not exists website_source text,
  add column if not exists website_confidence numeric;

alter table public.establishments
  drop constraint if exists establishments_website_confidence_check,
  add constraint establishments_website_confidence_check check (
    website_confidence is null or website_confidence between 0 and 1
  );

alter table ingest.source_records
  drop constraint if exists source_records_field_provenance_object_check,
  add constraint source_records_field_provenance_object_check
    check (jsonb_typeof(field_provenance) = 'object');

create table governance.source_policy_versions (
  id bigint generated always as identity primary key,
  source text not null,
  policy_version text not null,
  source_owner text not null,
  acquisition_method text not null,
  terms_url text,
  terms_hash text,
  license_ids text[] not null default '{}',
  storage_class text not null,
  raw_persistence_allowed boolean not null,
  normalized_persistence_allowed boolean not null,
  canonical_derivation_allowed boolean not null,
  display_allowed boolean not null,
  max_retention interval,
  attribution_text text,
  effective_from timestamptz not null,
  effective_to timestamptz,
  reviewed_at timestamptz not null,
  reviewed_by text not null,
  notes text,
  created_at timestamptz not null default now(),
  unique (source, policy_version),
  check (source ~ '^[a-z][a-z0-9_]{0,63}$'),
  check (length(policy_version) between 1 and 100),
  check (length(source_owner) between 1 and 200),
  check (length(acquisition_method) between 1 and 100),
  check (storage_class in ('durable_open', 'durable_contract', 'direct_contribution', 'ttl', 'excluded')),
  check (max_retention is null or max_retention > interval '0 seconds'),
  check (effective_to is null or effective_to > effective_from),
  check (
    storage_class not in ('ttl', 'excluded')
    or not canonical_derivation_allowed
  )
);

create table governance.source_field_policies (
  source_policy_id bigint not null
    references governance.source_policy_versions(id) on delete restrict,
  field_name text not null,
  durable_storage_allowed boolean not null,
  canonical_derivation_allowed boolean not null,
  display_allowed boolean not null,
  authority numeric not null,
  allowed_license_ids text[] not null default '{}',
  recommended_max_age interval,
  created_at timestamptz not null default now(),
  primary key (source_policy_id, field_name),
  check (field_name ~ '^[a-z][a-z0-9_]{0,63}$'),
  check (authority between 0 and 1),
  check (recommended_max_age is null or recommended_max_age > interval '0 seconds'),
  check (not canonical_derivation_allowed or durable_storage_allowed)
);

create index source_policy_versions_current_idx
  on governance.source_policy_versions (source, effective_from desc, id desc)
  where effective_to is null;

create or replace view governance.current_source_field_policies
with (security_invoker = true) as
select distinct on (policy.source, field_policy.field_name)
  policy.id as source_policy_id,
  policy.source,
  policy.policy_version,
  policy.storage_class,
  policy.license_ids as policy_license_ids,
  policy.normalized_persistence_allowed,
  policy.canonical_derivation_allowed as source_derivation_allowed,
  policy.display_allowed as source_display_allowed,
  policy.max_retention,
  field_policy.field_name,
  field_policy.durable_storage_allowed,
  field_policy.canonical_derivation_allowed,
  field_policy.display_allowed,
  field_policy.authority,
  field_policy.allowed_license_ids,
  field_policy.recommended_max_age
from governance.source_policy_versions policy
join governance.source_field_policies field_policy
  on field_policy.source_policy_id = policy.id
where policy.effective_from <= now()
  and (policy.effective_to is null or policy.effective_to > now())
order by policy.source, field_policy.field_name, policy.effective_from desc, policy.id desc;

insert into governance.source_policy_versions (
  source, policy_version, source_owner, acquisition_method, terms_url,
  license_ids, storage_class, raw_persistence_allowed,
  normalized_persistence_allowed, canonical_derivation_allowed, display_allowed,
  max_retention, attribution_text, effective_from, reviewed_at, reviewed_by, notes
) values
  ('ca_abc', '2026-08-v1', 'California Department of Alcoholic Beverage Control',
   'public_record_export', 'https://www.abc.ca.gov/licensing/licensing-reports/',
   array['California-public-record'], 'durable_open', true, true, true, true, null,
   'California Department of Alcoholic Beverage Control', '2026-08-01', now(),
   'paloma-data-policy-review', 'Legal license evidence only; not consumer operating status.'),
  ('datasf', '2026-08-v1', 'City and County of San Francisco',
   'open_data_export', 'https://data.sfgov.org/terms-of-use',
   array['PDDL-1.0','DataSF-open-data'], 'durable_open', true, true, true, true, null,
   'DataSF', '2026-08-01', now(), 'paloma-data-policy-review',
   'Dataset-specific metadata must still be reviewed before adding a new DataSF dataset.'),
  ('fsq', 'os-places-2026-v1', 'Foursquare', 'open_data_release',
   'https://docs.foursquare.com/data-products/docs/places-os-data-schema',
   array['Apache-2.0'], 'durable_open', true, true, true, true, null,
   'Foursquare OS Places', '2026-08-01', now(), 'paloma-data-policy-review',
   'Applies only to FSQ OS Places, never the self-service API.'),
  ('overture', 'places-2026-v1', 'Overture Maps Foundation', 'open_data_release',
   'https://docs.overturemaps.org/guides/places/',
   array['Apache-2.0','CC0-1.0','CDLA-Permissive-2.0'], 'durable_open', true, true,
   true, true, null, 'Overture Maps Foundation and property-level upstream sources',
   '2026-08-01', now(), 'paloma-data-policy-review',
   'Every observation must retain the property-level SourceItem license and origin.'),
  ('overture_divisions', 'divisions-2026-v1', 'Overture Maps Foundation',
   'open_data_release', 'https://docs.overturemaps.org/guides/divisions/',
   array['Overture-source-licenses','CC0-1.0','CDLA-Permissive-2.0'], 'durable_open',
   true, true, true, true, null, 'Overture Maps Foundation', '2026-08-01', now(),
   'paloma-data-policy-review', 'Used for deterministic point-in-polygon labels.'),
  ('wikidata', 'cc0-2026-v1', 'Wikimedia Foundation and Wikidata contributors',
   'sparql', 'https://www.wikidata.org/wiki/Wikidata:Copyright', array['CC0-1.0'],
   'durable_open', true, true, true, true, null, 'Wikidata contributors',
   '2026-08-01', now(), 'paloma-data-policy-review', 'Sparse corroboration only.'),
  ('manual', 'paloma-curation-v1', 'Paloma', 'independent_manual_verification', null,
   array['Paloma-manual-verification'], 'direct_contribution', true, true, true, true,
   null, null, '2026-08-01', now(), 'paloma-data-policy-review',
   'Paloma staff independently verified facts.'),
  ('merchant', 'contributor-terms-2026-08-v1', 'Authorized establishment representative',
   'merchant_claim', null, array['Paloma-contributor-terms-2026-08-v1'],
   'direct_contribution', true, true, true, true, null, null, '2026-08-01', now(),
   'paloma-data-policy-review', 'Requires a verified merchant claim and accepted terms.'),
  ('firsthand', 'contributor-terms-2026-08-v1', 'Paloma contributor',
   'firsthand_report', null, array['Paloma-contributor-terms-2026-08-v1'],
   'direct_contribution', true, true, true, true, null, null, '2026-08-01', now(),
   'paloma-data-policy-review', 'Must be firsthand and is reviewed before canonical use.'),
  ('osm', 'excluded-2026-v1', 'OpenStreetMap contributors', 'open_data_release',
   'https://osmfoundation.org/wiki/Licence/Licence_and_Legal_FAQ', array['ODbL-1.0'],
   'excluded', false, false, false, false, null, null, '2026-08-01', now(),
   'paloma-data-policy-review', 'Excluded from the proprietary canonical database pending an explicit ODbL decision.'),
  ('official_web', 'excluded-2026-v1', 'Individual establishment websites', 'web_fetch',
   null, array['unknown'], 'excluded', false, false, false, false, null, null,
   '2026-08-01', now(), 'paloma-data-policy-review',
   'A structured format and robots allowance do not grant durable storage rights.')
on conflict (source, policy_version) do nothing;

with configured(source, policy_version, field_name, durable, derive, display, authority, licenses, max_age) as (
  values
    ('ca_abc','2026-08-v1','legal_name',true,true,true,0.99,array['California-public-record']::text[],interval '180 days'),
    ('ca_abc','2026-08-v1','address',true,true,true,0.99,array['California-public-record']::text[],interval '180 days'),
    ('ca_abc','2026-08-v1','primary_type_slug',true,true,true,0.99,array['California-public-record']::text[],interval '180 days'),
    ('ca_abc','2026-08-v1','license_status',true,true,true,0.99,array['California-public-record']::text[],interval '45 days'),
    ('ca_abc','2026-08-v1','latitude',true,true,true,0.85,array['California-public-record']::text[],interval '365 days'),
    ('ca_abc','2026-08-v1','longitude',true,true,true,0.85,array['California-public-record']::text[],interval '365 days'),
    ('datasf','2026-08-v1','legal_name',true,true,true,0.88,array['PDDL-1.0','DataSF-open-data']::text[],interval '180 days'),
    ('datasf','2026-08-v1','address',true,true,true,0.95,array['PDDL-1.0','DataSF-open-data']::text[],interval '180 days'),
    ('datasf','2026-08-v1','primary_type_slug',true,true,true,0.92,array['PDDL-1.0','DataSF-open-data']::text[],interval '180 days'),
    ('datasf','2026-08-v1','registration_status',true,true,true,0.92,array['PDDL-1.0','DataSF-open-data']::text[],interval '45 days'),
    ('datasf','2026-08-v1','latitude',true,true,true,0.90,array['PDDL-1.0','DataSF-open-data']::text[],interval '365 days'),
    ('datasf','2026-08-v1','longitude',true,true,true,0.90,array['PDDL-1.0','DataSF-open-data']::text[],interval '365 days'),
    ('datasf','2026-08-v1','neighborhood',true,true,true,0.90,array['PDDL-1.0','DataSF-open-data']::text[],interval '365 days'),
    ('fsq','os-places-2026-v1','display_name',true,true,true,0.90,array['Apache-2.0']::text[],interval '365 days'),
    ('fsq','os-places-2026-v1','address',true,true,true,0.91,array['Apache-2.0']::text[],interval '365 days'),
    ('fsq','os-places-2026-v1','phone_e164',true,true,true,0.90,array['Apache-2.0']::text[],interval '120 days'),
    ('fsq','os-places-2026-v1','website_url',true,true,true,0.88,array['Apache-2.0']::text[],interval '120 days'),
    ('fsq','os-places-2026-v1','primary_type_slug',true,true,true,0.90,array['Apache-2.0']::text[],interval '365 days'),
    ('fsq','os-places-2026-v1','operating_status',true,true,true,0.84,array['Apache-2.0']::text[],interval '45 days'),
    ('fsq','os-places-2026-v1','latitude',true,true,true,0.96,array['Apache-2.0']::text[],interval '365 days'),
    ('fsq','os-places-2026-v1','longitude',true,true,true,0.96,array['Apache-2.0']::text[],interval '365 days'),
    ('overture','places-2026-v1','display_name',true,true,true,0.84,array['Apache-2.0','CC0-1.0','CDLA-Permissive-2.0']::text[],interval '365 days'),
    ('overture','places-2026-v1','address',true,true,true,0.91,array['Apache-2.0','CC0-1.0','CDLA-Permissive-2.0']::text[],interval '365 days'),
    ('overture','places-2026-v1','phone_e164',true,true,true,0.88,array['Apache-2.0','CC0-1.0','CDLA-Permissive-2.0']::text[],interval '120 days'),
    ('overture','places-2026-v1','website_url',true,true,true,0.84,array['Apache-2.0','CC0-1.0','CDLA-Permissive-2.0']::text[],interval '120 days'),
    ('overture','places-2026-v1','primary_type_slug',true,true,true,0.88,array['Apache-2.0','CC0-1.0','CDLA-Permissive-2.0']::text[],interval '365 days'),
    ('overture','places-2026-v1','operating_status',true,true,true,0.80,array['Apache-2.0','CC0-1.0','CDLA-Permissive-2.0']::text[],interval '45 days'),
    ('overture','places-2026-v1','latitude',true,true,true,0.95,array['Apache-2.0','CC0-1.0','CDLA-Permissive-2.0']::text[],interval '365 days'),
    ('overture','places-2026-v1','longitude',true,true,true,0.95,array['Apache-2.0','CC0-1.0','CDLA-Permissive-2.0']::text[],interval '365 days'),
    ('overture','places-2026-v1','setting_slug',true,true,true,0.76,array['Apache-2.0','CC0-1.0','CDLA-Permissive-2.0']::text[],interval '180 days'),
    ('overture_divisions','divisions-2026-v1','neighborhood',true,true,true,0.98,array['Overture-source-licenses','CC0-1.0','CDLA-Permissive-2.0']::text[],interval '365 days'),
    ('wikidata','cc0-2026-v1','display_name',true,true,true,0.78,array['CC0-1.0']::text[],interval '365 days'),
    ('wikidata','cc0-2026-v1','address',true,true,true,0.78,array['CC0-1.0']::text[],interval '365 days'),
    ('wikidata','cc0-2026-v1','phone_e164',true,true,true,0.76,array['CC0-1.0']::text[],interval '180 days'),
    ('wikidata','cc0-2026-v1','website_url',true,true,true,0.82,array['CC0-1.0']::text[],interval '180 days'),
    ('wikidata','cc0-2026-v1','primary_type_slug',true,true,true,0.72,array['CC0-1.0']::text[],interval '365 days'),
    ('wikidata','cc0-2026-v1','latitude',true,true,true,0.80,array['CC0-1.0']::text[],interval '365 days'),
    ('wikidata','cc0-2026-v1','longitude',true,true,true,0.80,array['CC0-1.0']::text[],interval '365 days')
)
insert into governance.source_field_policies (
  source_policy_id, field_name, durable_storage_allowed,
  canonical_derivation_allowed, display_allowed, authority,
  allowed_license_ids, recommended_max_age
)
select policy.id, configured.field_name, configured.durable, configured.derive,
       configured.display, configured.authority, configured.licenses, configured.max_age
from configured
join governance.source_policy_versions policy
  on policy.source = configured.source and policy.policy_version = configured.policy_version
on conflict (source_policy_id, field_name) do nothing;

with direct_sources(source, policy_version, authority, licenses, fields) as (
  values
    ('manual','paloma-curation-v1',1.00,array['Paloma-manual-verification']::text[],
      array['display_name','legal_name','address','phone_e164','website_url','primary_type_slug','operating_status','license_status','registration_status','latitude','longitude','neighborhood','hours','price_level','setting_slug']::text[]),
    ('merchant','contributor-terms-2026-08-v1',1.00,array['Paloma-contributor-terms-2026-08-v1']::text[],
      array['display_name','address','phone_e164','website_url','primary_type_slug','operating_status','latitude','longitude','hours','price_level','setting_slug']::text[]),
    ('firsthand','contributor-terms-2026-08-v1',0.76,array['Paloma-contributor-terms-2026-08-v1']::text[],
      array['phone_e164','website_url','operating_status','hours','price_level','setting_slug']::text[])
)
insert into governance.source_field_policies (
  source_policy_id, field_name, durable_storage_allowed,
  canonical_derivation_allowed, display_allowed, authority,
  allowed_license_ids, recommended_max_age
)
select policy.id, field_name, true, true, true, source.authority, source.licenses,
       case
         when field_name in ('hours','operating_status') then interval '90 days'
         when field_name in ('phone_e164','website_url','price_level','setting_slug') then interval '180 days'
         else interval '365 days'
       end
from direct_sources source
join governance.source_policy_versions policy
  on policy.source = source.source and policy.policy_version = source.policy_version
cross join lateral unnest(source.fields) as field_name
on conflict (source_policy_id, field_name) do nothing;

with excluded_sources(source, policy_version, licenses, fields) as (
  values
    ('osm','excluded-2026-v1',array['ODbL-1.0']::text[],
      array['display_name','address','phone_e164','website_url','primary_type_slug','operating_status','latitude','longitude','hours','setting_slug']::text[]),
    ('official_web','excluded-2026-v1',array['unknown']::text[],
      array['display_name','phone_e164','website_url','hours']::text[])
)
insert into governance.source_field_policies (
  source_policy_id, field_name, durable_storage_allowed,
  canonical_derivation_allowed, display_allowed, authority,
  allowed_license_ids, recommended_max_age
)
select policy.id, field_name, false, false, false, 0, source.licenses, null
from excluded_sources source
join governance.source_policy_versions policy
  on policy.source = source.source and policy.policy_version = source.policy_version
cross join lateral unnest(source.fields) as field_name
on conflict (source_policy_id, field_name) do nothing;

create table catalog.field_observations (
  id uuid primary key default gen_random_uuid(),
  establishment_id uuid not null references public.establishments(id) on delete restrict,
  field_name text not null,
  value_text text,
  normalized_value text,
  value_json jsonb,
  value_hash character(64) not null,
  source text not null,
  source_record_id text not null,
  source_property text,
  source_run_id uuid references ingest.ingestion_runs(id) on delete restrict,
  source_record_payload_hash text,
  claim_kind text not null default 'observed',
  observation_status text not null default 'asserted',
  evidence_confidence numeric not null,
  identity_confidence numeric not null,
  authority numeric not null,
  upstream_origin_keys text[] not null,
  license_ids text[] not null,
  source_items jsonb not null default '[]'::jsonb,
  source_policy_id bigint not null
    references governance.source_policy_versions(id) on delete restrict,
  source_updated_at timestamptz,
  observed_at timestamptz not null default now(),
  valid_from timestamptz,
  valid_to timestamptz,
  expires_at timestamptz,
  observation_fingerprint character(64) not null unique,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (field_name ~ '^[a-z][a-z0-9_]{0,63}$'),
  check (value_text is not null or value_json is not null or observation_status = 'retracted'),
  check (value_hash ~ '^[0-9a-f]{64}$'),
  check (source ~ '^[a-z][a-z0-9_]{0,63}$'),
  check (length(source_record_id) between 1 and 500),
  check (claim_kind in ('observed','owner_attested','firsthand','manual','derived')),
  check (observation_status in ('asserted','retracted')),
  check (evidence_confidence between 0 and 1),
  check (identity_confidence between 0 and 1),
  check (authority between 0 and 1),
  check (cardinality(upstream_origin_keys) > 0),
  check (cardinality(license_ids) > 0),
  check (jsonb_typeof(source_items) = 'array'),
  check (jsonb_typeof(metadata) = 'object'),
  check (valid_to is null or (valid_from is not null and valid_to > valid_from)),
  check (expires_at is null or expires_at > observed_at),
  check (observation_fingerprint ~ '^[0-9a-f]{64}$')
);

create index field_observations_establishment_field_time_idx
  on catalog.field_observations (establishment_id, field_name, observed_at desc, id);
create index field_observations_source_record_idx
  on catalog.field_observations (source, source_record_id, field_name, observed_at desc);
create index field_observations_policy_idx
  on catalog.field_observations (source_policy_id);
create index field_observations_current_idx
  on catalog.field_observations (establishment_id, field_name, source, source_record_id, observed_at desc)
  where observation_status = 'asserted';
create index field_observations_expiry_idx
  on catalog.field_observations (expires_at)
  where expires_at is not null;

create table catalog.field_decisions (
  id bigint generated always as identity primary key,
  establishment_id uuid not null references public.establishments(id) on delete restrict,
  field_name text not null,
  decision_status text not null,
  value_text text,
  normalized_value text,
  value_json jsonb,
  confidence numeric,
  resolver_version text not null,
  evidence_ids uuid[] not null default '{}',
  independent_origin_keys text[] not null default '{}',
  reason_codes text[] not null default '{}',
  supersedes_decision_id bigint references catalog.field_decisions(id) on delete restrict,
  effective_from timestamptz not null default now(),
  decided_at timestamptz not null default now(),
  decision_fingerprint character(64) not null unique,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (field_name ~ '^[a-z][a-z0-9_]{0,63}$'),
  check (decision_status in ('selected','unknown','stale','conflicted','rejected')),
  check (confidence is null or confidence between 0 and 1),
  check (length(resolver_version) between 1 and 100),
  check (decision_fingerprint ~ '^[0-9a-f]{64}$'),
  check (jsonb_typeof(metadata) = 'object'),
  check (
    decision_status <> 'selected'
    or value_text is not null
    or value_json is not null
  )
);

create index field_decisions_current_idx
  on catalog.field_decisions (establishment_id, field_name, decided_at desc, id desc);
create index field_decisions_supersedes_idx
  on catalog.field_decisions (supersedes_decision_id)
  where supersedes_decision_id is not null;

create or replace view catalog.current_field_decisions
with (security_invoker = true) as
select distinct on (establishment_id, field_name) *
from catalog.field_decisions
order by establishment_id, field_name, decided_at desc, id desc;

create table review.field_conflicts (
  id bigint generated always as identity primary key,
  establishment_id uuid not null references public.establishments(id) on delete restrict,
  field_name text not null,
  decision_id bigint references catalog.field_decisions(id) on delete restrict,
  reason text not null,
  evidence_ids uuid[] not null default '{}',
  priority smallint not null default 50,
  state text not null default 'pending',
  created_at timestamptz not null default now(),
  resolved_at timestamptz,
  resolved_by text,
  resolution_notes text,
  check (field_name ~ '^[a-z][a-z0-9_]{0,63}$'),
  check (priority between 1 and 100),
  check (state in ('pending','resolved','dismissed')),
  check ((state = 'pending' and resolved_at is null) or state <> 'pending')
);

create unique index field_conflicts_pending_unique
  on review.field_conflicts (establishment_id, field_name, reason)
  where state = 'pending';
create index field_conflicts_pending_priority_idx
  on review.field_conflicts (priority desc, created_at)
  where state = 'pending';

create or replace function governance.enforce_field_observation_rights()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  policy governance.source_policy_versions%rowtype;
  field_policy governance.source_field_policies%rowtype;
begin
  select * into policy
  from governance.source_policy_versions
  where id = new.source_policy_id and source = new.source;
  if not found then
    raise exception 'unknown or mismatched source policy for %', new.source;
  end if;

  select * into field_policy
  from governance.source_field_policies
  where source_policy_id = new.source_policy_id and field_name = new.field_name;
  if not found
     or policy.effective_from > new.observed_at
     or (policy.effective_to is not null and policy.effective_to <= new.observed_at)
     or not policy.normalized_persistence_allowed
     or not policy.canonical_derivation_allowed
     or not field_policy.durable_storage_allowed
     or not field_policy.canonical_derivation_allowed then
    raise exception 'durable canonical use is not allowed for %.%', new.source, new.field_name;
  end if;

  if cardinality(field_policy.allowed_license_ids) > 0
     and not (new.license_ids <@ field_policy.allowed_license_ids) then
    raise exception 'unapproved license for %.%: %', new.source, new.field_name, new.license_ids;
  end if;
  if new.authority > field_policy.authority then
    raise exception 'authority exceeds policy for %.%', new.source, new.field_name;
  end if;
  if policy.max_retention is not null
     and (new.expires_at is null
          or new.expires_at > new.observed_at + policy.max_retention) then
    raise exception 'retention exceeds policy for %.%', new.source, new.field_name;
  end if;
  return new;
end;
$$;

create trigger enforce_field_observation_rights
before insert on catalog.field_observations
for each row execute function governance.enforce_field_observation_rights();

create or replace function governance.reject_append_only_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception '%.% is append-only', tg_table_schema, tg_table_name;
end;
$$;

create trigger field_observations_append_only
before update or delete on catalog.field_observations
for each row execute function governance.reject_append_only_mutation();
create trigger field_decisions_append_only
before update or delete on catalog.field_decisions
for each row execute function governance.reject_append_only_mutation();

create table catalog.hours_schedules (
  observation_id uuid primary key
    references catalog.field_observations(id) on delete restrict,
  establishment_id uuid not null references public.establishments(id) on delete restrict,
  timezone_name text not null,
  schema_version text not null,
  valid_from timestamptz,
  valid_to timestamptz,
  created_at timestamptz not null default now(),
  check (schema_version = 'paloma-hours-v1'),
  check (length(timezone_name) between 1 and 100),
  check (valid_to is null or (valid_from is not null and valid_to > valid_from))
);

create table catalog.hours_weekly_intervals (
  id bigint generated always as identity primary key,
  observation_id uuid not null
    references catalog.hours_schedules(observation_id) on delete restrict,
  iso_weekday smallint not null,
  opens_at time not null,
  closes_at time not null,
  closes_day_offset smallint not null default 0,
  created_at timestamptz not null default now(),
  unique (observation_id, iso_weekday, opens_at, closes_at, closes_day_offset),
  check (iso_weekday between 1 and 7),
  check (closes_day_offset between 0 and 1)
);

create index hours_weekly_observation_idx
  on catalog.hours_weekly_intervals (observation_id, iso_weekday, opens_at);

create table catalog.hours_special_intervals (
  id bigint generated always as identity primary key,
  observation_id uuid not null
    references catalog.hours_schedules(observation_id) on delete restrict,
  service_date date not null,
  closed boolean not null default false,
  opens_at time,
  closes_at time,
  closes_day_offset smallint,
  created_at timestamptz not null default now(),
  check (closes_day_offset is null or closes_day_offset between 0 and 1),
  check (
    (closed and opens_at is null and closes_at is null and closes_day_offset is null)
    or (not closed and opens_at is not null and closes_at is not null
        and closes_day_offset is not null)
  )
);

create index hours_special_observation_date_idx
  on catalog.hours_special_intervals (observation_id, service_date);

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
    observation_id, establishment_id, timezone_name, schema_version, valid_from, valid_to
  ) values (
    new.id, new.establishment_id, new.value_json->>'timezone',
    new.value_json->>'schema_version', new.valid_from, new.valid_to
  );

  for interval_item in
    select value from jsonb_array_elements(coalesce(new.value_json->'weekly', '[]'::jsonb))
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
    select value from jsonb_array_elements(coalesce(new.value_json->'special', '[]'::jsonb))
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

create trigger project_normalized_hours
after insert on catalog.field_observations
for each row execute function catalog.project_normalized_hours();

create trigger hours_schedules_append_only
before update or delete on catalog.hours_schedules
for each row execute function governance.reject_append_only_mutation();
create trigger hours_weekly_intervals_append_only
before update or delete on catalog.hours_weekly_intervals
for each row execute function governance.reject_append_only_mutation();
create trigger hours_special_intervals_append_only
before update or delete on catalog.hours_special_intervals
for each row execute function governance.reject_append_only_mutation();

create table governance.contribution_terms_versions (
  version text primary key,
  document_path text not null,
  document_sha256 character(64) not null,
  state text not null default 'draft',
  effective_from timestamptz,
  retired_at timestamptz,
  approved_by text,
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  check (length(version) between 1 and 100),
  check (document_sha256 ~ '^[0-9a-f]{64}$'),
  check (state in ('draft','active','retired')),
  check (
    (state = 'active' and effective_from is not null and approved_by is not null
      and approved_at is not null)
    or state <> 'active'
  )
);

insert into governance.contribution_terms_versions (
  version, document_path, document_sha256, state
) values (
  '2026-08-v1',
  'docs/contributor-terms-2026-08-v1.md',
  '5b3a93bf8c1de08dd7b49f07bb2b0ae6ba661d8399e672c0518ef8cc6940fbae',
  'draft'
) on conflict (version) do nothing;

create table public.merchant_claim_requests (
  id uuid primary key default gen_random_uuid(),
  establishment_id uuid not null references public.establishments(id) on delete restrict,
  claimant_id uuid not null references auth.users(id) on delete restrict,
  business_role text not null,
  business_email text,
  verification_notes text,
  terms_version text not null
    references governance.contribution_terms_versions(version) on delete restrict,
  attests_authority boolean not null,
  state text not null default 'pending',
  submitted_at timestamptz not null default now(),
  reviewed_at timestamptz,
  reviewed_by text,
  check (length(business_role) between 1 and 200),
  check (business_email is null or length(business_email) between 3 and 320),
  check (attests_authority),
  check (state in ('pending','verified','rejected','withdrawn')),
  check ((state = 'pending' and reviewed_at is null) or state <> 'pending')
);

create unique index merchant_claim_requests_active_unique
  on public.merchant_claim_requests (establishment_id, claimant_id)
  where state in ('pending','verified');
create index merchant_claim_requests_claimant_idx
  on public.merchant_claim_requests (claimant_id, submitted_at desc);
create index merchant_claim_requests_pending_idx
  on public.merchant_claim_requests (submitted_at)
  where state = 'pending';

create table public.establishment_contributions (
  id uuid primary key default gen_random_uuid(),
  establishment_id uuid not null references public.establishments(id) on delete restrict,
  contributor_id uuid not null references auth.users(id) on delete restrict,
  contribution_kind text not null,
  field_name text not null,
  proposed_value jsonb not null,
  observed_at timestamptz not null,
  terms_version text not null
    references governance.contribution_terms_versions(version) on delete restrict,
  attests_firsthand_or_authorized boolean not null,
  state text not null default 'pending',
  submitted_at timestamptz not null default now(),
  reviewed_at timestamptz,
  reviewed_by text,
  review_reason text,
  check (contribution_kind in ('merchant','firsthand')),
  check (field_name in (
    'display_name','address','phone_e164','website_url','primary_type_slug',
    'operating_status','latitude','longitude','hours','price_level','setting_slug'
  )),
  check (jsonb_typeof(proposed_value) in ('string','number','object','array','boolean')),
  check (attests_firsthand_or_authorized),
  check (observed_at <= submitted_at + interval '5 minutes'),
  check (state in ('pending','accepted','rejected','withdrawn')),
  check ((state = 'pending' and reviewed_at is null) or state <> 'pending')
);

create index establishment_contributions_contributor_idx
  on public.establishment_contributions (contributor_id, submitted_at desc);
create index establishment_contributions_pending_idx
  on public.establishment_contributions (submitted_at)
  where state = 'pending';
create index establishment_contributions_establishment_field_idx
  on public.establishment_contributions (establishment_id, field_name, submitted_at desc);

create table catalog.contribution_reviews (
  id bigint generated always as identity primary key,
  contribution_id uuid not null
    references public.establishment_contributions(id) on delete restrict,
  decision text not null,
  reviewer text not null,
  reason text not null,
  observation_id uuid references catalog.field_observations(id) on delete restrict,
  reviewed_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb,
  check (decision in ('accepted','rejected')),
  check (length(reviewer) between 1 and 200),
  check (jsonb_typeof(metadata) = 'object'),
  check ((decision = 'accepted' and observation_id is not null) or decision = 'rejected')
);

create index contribution_reviews_contribution_idx
  on catalog.contribution_reviews (contribution_id, reviewed_at desc);

create table catalog.merchant_claim_reviews (
  id bigint generated always as identity primary key,
  merchant_claim_id uuid not null
    references public.merchant_claim_requests(id) on delete restrict,
  decision text not null,
  reviewer text not null,
  reason text not null,
  reviewed_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb,
  check (decision in ('verified','rejected')),
  check (length(reviewer) between 1 and 200),
  check (jsonb_typeof(metadata) = 'object')
);

create index merchant_claim_reviews_claim_idx
  on catalog.merchant_claim_reviews (merchant_claim_id, reviewed_at desc);

create or replace function governance.validate_contribution()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if (select auth.uid()) is not null and (select auth.uid()) <> new.contributor_id then
    raise exception 'contributor identity mismatch';
  end if;
  if not exists (
    select 1
    from governance.contribution_terms_versions terms
    where terms.version = new.terms_version
      and terms.state = 'active'
      and terms.effective_from <= now()
      and (terms.retired_at is null or terms.retired_at > now())
  ) then
    raise exception 'contribution terms are not active';
  end if;
  if new.contribution_kind = 'merchant' and not exists (
    select 1
    from public.merchant_claim_requests claim
    where claim.establishment_id = new.establishment_id
      and claim.claimant_id = new.contributor_id
      and claim.state = 'verified'
  ) then
    raise exception 'a verified merchant claim is required';
  end if;
  return new;
end;
$$;

create trigger validate_establishment_contribution
before insert on public.establishment_contributions
for each row execute function governance.validate_contribution();

create trigger contribution_reviews_append_only
before update or delete on catalog.contribution_reviews
for each row execute function governance.reject_append_only_mutation();
create trigger merchant_claim_reviews_append_only
before update or delete on catalog.merchant_claim_reviews
for each row execute function governance.reject_append_only_mutation();
create trigger source_policy_versions_append_only
before update or delete on governance.source_policy_versions
for each row execute function governance.reject_append_only_mutation();
create trigger source_field_policies_append_only
before update or delete on governance.source_field_policies
for each row execute function governance.reject_append_only_mutation();

alter table public.merchant_claim_requests enable row level security;
alter table public.establishment_contributions enable row level security;

revoke all on public.merchant_claim_requests, public.establishment_contributions
  from public, anon, authenticated, service_role, paloma_ingest;
grant select, insert on public.merchant_claim_requests, public.establishment_contributions
  to authenticated;
grant select, update on public.merchant_claim_requests, public.establishment_contributions
  to paloma_ingest;

create policy merchant_claim_requests_own_select
  on public.merchant_claim_requests for select to authenticated
  using ((select auth.uid()) = claimant_id);
create policy merchant_claim_requests_own_insert
  on public.merchant_claim_requests for insert to authenticated
  with check ((select auth.uid()) is not null and (select auth.uid()) = claimant_id);
create policy paloma_ingest_manage_merchant_claim_requests
  on public.merchant_claim_requests for all to paloma_ingest
  using (true) with check (true);

create policy establishment_contributions_own_select
  on public.establishment_contributions for select to authenticated
  using ((select auth.uid()) = contributor_id);
create policy establishment_contributions_own_insert
  on public.establishment_contributions for insert to authenticated
  with check ((select auth.uid()) is not null and (select auth.uid()) = contributor_id);
create policy paloma_ingest_manage_establishment_contributions
  on public.establishment_contributions for all to paloma_ingest
  using (true) with check (true);

create or replace view catalog.establishment_field_coverage
with (security_invoker = true) as
with fields(field_name) as (
  values ('display_name'), ('primary_type_slug'), ('price_level'), ('neighborhood'),
         ('directory_status'), ('operating_status'), ('setting_slug'), ('hours'),
         ('address'), ('latitude'), ('longitude'), ('website_url'), ('phone_e164')
)
select establishment.id as establishment_id,
       fields.field_name,
       case when fields.field_name = 'directory_status' then 'selected'
            when conflict.pending then 'conflicted'
            else decision.decision_status end as decision_status,
       case when fields.field_name = 'directory_status' then 1.0
            else decision.confidence end as confidence,
       decision.decided_at as last_decided_at,
       observation.latest_observed_at,
       observation.expires_at,
       coalesce(cardinality(decision.independent_origin_keys), 0) as independent_origin_count,
       case
         when fields.field_name = 'directory_status' then 'selected'
         when conflict.pending then 'conflicted'
         when decision.id is null then 'unknown'
         when decision.decision_status = 'conflicted' then 'conflicted'
         when decision.decision_status = 'stale'
           or (observation.expires_at is not null and observation.expires_at <= now()) then 'stale'
         else decision.decision_status
       end as coverage_status
from public.establishments establishment
cross join fields
left join catalog.current_field_decisions decision
  on decision.establishment_id = establishment.id
 and decision.field_name = fields.field_name
left join lateral (
  select max(observed_at) as latest_observed_at, max(expires_at) as expires_at
  from catalog.field_observations evidence
  where evidence.establishment_id = establishment.id
    and evidence.field_name = fields.field_name
    and evidence.id = any(coalesce(decision.evidence_ids, '{}'::uuid[]))
) observation on true
left join lateral (
  select true as pending
  from review.field_conflicts item
  where item.establishment_id = establishment.id
    and item.field_name = fields.field_name
    and item.state = 'pending'
  limit 1
) conflict on true
where establishment.publication_state = 'published';

-- Move expiring provider state out of the durable ingest/evidence boundary. ALTER TABLE SET
-- SCHEMA preserves table OIDs, foreign keys, indexes, RLS, grants, and owned sequences.
alter table if exists ingest.runtime_provider_links set schema runtime;
alter table if exists ingest.provider_response_cache set schema runtime;
alter table if exists ingest.provider_refresh_leases set schema runtime;
alter table if exists ingest.provider_match_state set schema runtime;
alter table if exists ingest.live_detail_user_limits set schema runtime;
alter table if exists ingest.live_detail_global_limit set schema runtime;

revoke all on schema runtime from public, anon, authenticated, service_role;
grant usage on schema runtime to paloma_runtime, paloma_ingest;

create or replace function ingest.clear_cache_for_changed_provider_link()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if old.provider_place_id is distinct from new.provider_place_id
     or old.retired_at is distinct from new.retired_at then
    delete from runtime.provider_response_cache
    where provider_link_id = old.id;
    delete from runtime.provider_refresh_leases
    where provider_link_id = old.id;
  end if;
  return new;
end;
$$;

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
  delete from runtime.provider_response_cache cache
  where cache.expires_at <= pg_catalog.now()
     or not exists (
       select 1
       from runtime.runtime_provider_links runtime_link
       where runtime_link.id = cache.provider_link_id
         and runtime_link.provider = cache.provider
         and runtime_link.retired_at is null
     );
  get diagnostics deleted_responses = row_count;

  delete from runtime.provider_refresh_leases lease
  where lease.lease_expires_at <= pg_catalog.now()
     or not exists (
       select 1
       from runtime.runtime_provider_links runtime_link
       where runtime_link.id = lease.provider_link_id
         and runtime_link.provider = lease.provider
         and runtime_link.retired_at is null
     );
  get diagnostics deleted_leases = row_count;

  update runtime.provider_match_state
  set state = 'retryable',
      retry_after = least(
        coalesce(retry_after, pg_catalog.now()),
        pg_catalog.now() + interval '1 hour'
      ),
      updated_at = pg_catalog.now()
  where state = 'matching'
    and updated_at < pg_catalog.now() - interval '5 minutes';

  return query select deleted_responses, deleted_leases;
end;
$$;

-- Existing evidence is copied only when the new rights gate can prove eligibility. The old
-- mutable table remains for migration audit but loses write privileges.
insert into catalog.field_observations (
  id, establishment_id, field_name, value_text, normalized_value, value_json,
  value_hash, source, source_record_id, claim_kind, observation_status,
  evidence_confidence, identity_confidence, authority, upstream_origin_keys,
  license_ids, source_items, source_policy_id, source_updated_at, observed_at,
  observation_fingerprint, metadata
)
select evidence.id, evidence.establishment_id, evidence.field_name,
       evidence.value_text, evidence.normalized_value, evidence.value_json,
       encode(extensions.digest(convert_to(
         coalesce(evidence.normalized_value, evidence.value_text, evidence.value_json::text),
         'UTF8'
       ), 'sha256'), 'hex'),
       evidence.source, evidence.source_record_id,
       case when evidence.claim_kind in ('observed','owner_attested','firsthand','manual','derived')
            then evidence.claim_kind else 'observed' end,
       'asserted',
       evidence.evidence_confidence, evidence.identity_confidence,
       least(evidence.authority, policy.authority),
       coalesce(nullif(source_record.origin_keys, '{}'), array[evidence.source]::text[]),
       array[source_record.data_license]::text[], '[]'::jsonb,
       policy.source_policy_id, evidence.source_updated_at, evidence.observed_at,
       encode(extensions.digest(convert_to(
         concat_ws(chr(31), evidence.establishment_id::text, evidence.field_name,
                   evidence.source, evidence.source_record_id,
                   coalesce(evidence.normalized_value, evidence.value_text, evidence.value_json::text),
                   coalesce(evidence.source_updated_at::text, evidence.observed_at::text)),
         'UTF8'), 'sha256'), 'hex'),
       evidence.metadata || jsonb_build_object('legacy_evidence_id', evidence.id)
from ingest.establishment_field_evidence evidence
join ingest.source_records source_record
  on source_record.source = evidence.source
 and source_record.source_record_id = split_part(evidence.source_record_id, '#', 1)
join governance.current_source_field_policies policy
  on policy.source = evidence.source and policy.field_name = evidence.field_name
where policy.durable_storage_allowed
  and policy.canonical_derivation_allowed
  and array[source_record.data_license]::text[] <@ policy.allowed_license_ids
  and (
    evidence.field_name <> 'hours'
    or evidence.value_json->>'schema_version' = 'paloma-hours-v1'
  )
on conflict (id) do nothing;

revoke insert, update, delete on ingest.establishment_field_evidence from paloma_ingest;

alter table governance.source_policy_versions enable row level security;
alter table governance.source_field_policies enable row level security;
alter table catalog.field_observations enable row level security;
alter table catalog.field_decisions enable row level security;
alter table catalog.hours_schedules enable row level security;
alter table catalog.hours_weekly_intervals enable row level security;
alter table catalog.hours_special_intervals enable row level security;
alter table catalog.contribution_reviews enable row level security;
alter table catalog.merchant_claim_reviews enable row level security;
alter table review.field_conflicts enable row level security;

revoke all on all tables in schema governance, catalog, review
  from public, anon, authenticated, service_role, paloma_ingest;
revoke all on all sequences in schema governance, catalog, review
  from public, anon, authenticated, service_role, paloma_ingest;
revoke execute on all functions in schema governance, catalog
  from public, anon, authenticated, service_role, paloma_ingest;

grant select on governance.source_policy_versions,
  governance.source_field_policies, governance.current_source_field_policies,
  governance.contribution_terms_versions to paloma_ingest;
grant select, insert on catalog.field_observations, catalog.field_decisions,
  catalog.hours_schedules, catalog.hours_weekly_intervals,
  catalog.hours_special_intervals, catalog.contribution_reviews,
  catalog.merchant_claim_reviews to paloma_ingest;
grant select on catalog.current_field_decisions,
  catalog.establishment_field_coverage to paloma_ingest;
grant select, insert, update on review.field_conflicts to paloma_ingest;
grant usage, select on all sequences in schema catalog, review to paloma_ingest;

create policy paloma_ingest_read_source_policy_versions
  on governance.source_policy_versions for select to paloma_ingest using (true);
create policy paloma_ingest_read_source_field_policies
  on governance.source_field_policies for select to paloma_ingest using (true);
create policy paloma_ingest_read_contribution_terms
  on governance.contribution_terms_versions for select to paloma_ingest using (true);
create policy paloma_ingest_insert_field_observations
  on catalog.field_observations for insert to paloma_ingest with check (true);
create policy paloma_ingest_read_field_observations
  on catalog.field_observations for select to paloma_ingest using (true);
create policy paloma_ingest_insert_field_decisions
  on catalog.field_decisions for insert to paloma_ingest with check (true);
create policy paloma_ingest_read_field_decisions
  on catalog.field_decisions for select to paloma_ingest using (true);
create policy paloma_ingest_insert_hours_schedules
  on catalog.hours_schedules for insert to paloma_ingest with check (true);
create policy paloma_ingest_read_hours_schedules
  on catalog.hours_schedules for select to paloma_ingest using (true);
create policy paloma_ingest_insert_hours_weekly
  on catalog.hours_weekly_intervals for insert to paloma_ingest with check (true);
create policy paloma_ingest_read_hours_weekly
  on catalog.hours_weekly_intervals for select to paloma_ingest using (true);
create policy paloma_ingest_insert_hours_special
  on catalog.hours_special_intervals for insert to paloma_ingest with check (true);
create policy paloma_ingest_read_hours_special
  on catalog.hours_special_intervals for select to paloma_ingest using (true);
create policy paloma_ingest_manage_field_conflicts
  on review.field_conflicts for all to paloma_ingest using (true) with check (true);
create policy paloma_ingest_insert_contribution_reviews
  on catalog.contribution_reviews for insert to paloma_ingest with check (true);
create policy paloma_ingest_read_contribution_reviews
  on catalog.contribution_reviews for select to paloma_ingest using (true);
create policy paloma_ingest_insert_merchant_claim_reviews
  on catalog.merchant_claim_reviews for insert to paloma_ingest with check (true);
create policy paloma_ingest_read_merchant_claim_reviews
  on catalog.merchant_claim_reviews for select to paloma_ingest using (true);

grant execute on function governance.enforce_field_observation_rights() to paloma_ingest;
grant execute on function governance.reject_append_only_mutation() to paloma_ingest;
grant execute on function governance.validate_contribution() to authenticated, paloma_ingest;
grant execute on function catalog.project_normalized_hours() to paloma_ingest;

comment on schema governance is 'Versioned legal/storage policy. Unknown rights fail closed.';
comment on schema catalog is 'Durable append-only observations, decisions, and normalized schedules.';
comment on schema runtime is 'TTL-restricted provider payloads and runtime matching state only.';
comment on table catalog.field_observations is
  'Immutable field-level evidence admitted by a versioned rights policy.';
comment on table catalog.field_decisions is
  'Immutable resolver outcomes. Current state is the latest decision projection.';
comment on table governance.contribution_terms_versions is
  'Terms remain draft until owner/counsel approval; contributions fail closed meanwhile.';
