-- Provider-agnostic, rights-aware establishment artwork pipeline.
--
-- A source being geographically nearby never makes it the establishment. Sources are
-- immutable, identity decisions are append-only, generated assets cannot be published until
-- their source review and responsive variants satisfy the publication contract, and only the
-- final public derivatives are exposed to the app.

insert into governance.source_policy_versions (
  source, policy_version, source_owner, acquisition_method, terms_url,
  license_ids, storage_class, raw_persistence_allowed,
  normalized_persistence_allowed, canonical_derivation_allowed, display_allowed,
  max_retention, attribution_text, effective_from, reviewed_at, reviewed_by, notes
) values
  (
    'mapillary', '2026-09-media-v1', 'Mapillary contributors', 'official_api',
    'https://help.mapillary.com/hc/en-us/articles/115001770409-CC-BY-SA-license-for-open-data',
    array['CC-BY-SA-4.0'], 'durable_open', true, true, true, true, null,
    'Mapillary image by {creator}, CC BY-SA 4.0; modified by Paloma',
    '2026-09-01', now(), 'paloma-data-policy-review',
    'Signed delivery URLs are ephemeral. Persist the source asset ID, source page, attribution, and a private original only.'
  ),
  (
    'wikimedia_commons', '2026-09-media-v1', 'Wikimedia Commons contributors',
    'official_api', 'https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use',
    array[
      'CC0-1.0', 'Public-Domain',
      'CC-BY-2.0', 'CC-BY-2.5', 'CC-BY-3.0', 'CC-BY-4.0',
      'CC-BY-SA-2.0', 'CC-BY-SA-2.5', 'CC-BY-SA-3.0', 'CC-BY-SA-4.0'
    ],
    'durable_open', true, true, true, true, null,
    'Attribution and license are retained from each Commons file description page.',
    '2026-09-01', now(), 'paloma-data-policy-review',
    'Each file license is checked independently; noncommercial, no-derivatives, and unknown licenses are rejected.'
  )
on conflict (source, policy_version) do nothing;

insert into governance.source_field_policies (
  source_policy_id, field_name, durable_storage_allowed,
  canonical_derivation_allowed, display_allowed, authority,
  allowed_license_ids, recommended_max_age
)
select policy.id, 'cover_image_url', true, true, true, 0.80,
       policy.license_ids, interval '5 years'
from governance.source_policy_versions policy
where (policy.source, policy.policy_version) in (
  ('mapillary', '2026-09-media-v1'),
  ('wikimedia_commons', '2026-09-media-v1')
)
on conflict (source_policy_id, field_name) do nothing;

create table catalog.establishment_media_sources (
  id uuid primary key default gen_random_uuid(),
  establishment_id uuid not null
    references public.establishments(id) on delete restrict,
  source_policy_id bigint not null
    references governance.source_policy_versions(id) on delete restrict,
  provider text not null,
  source_asset_id text not null,
  source_page_url text not null,
  creator text,
  captured_at timestamptz,
  latitude double precision,
  longitude double precision,
  distance_meters double precision,
  camera_heading_degrees double precision,
  bearing_to_target_degrees double precision,
  heading_delta_degrees double precision,
  license_id text not null,
  license_url text not null,
  terms_url text not null,
  commercial_use_allowed boolean not null,
  derivatives_allowed boolean not null,
  raw_persistence_allowed boolean not null,
  attribution_required boolean not null,
  share_alike_required boolean not null,
  attribution_text text not null,
  discovered_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb,
  unique (establishment_id, provider, source_asset_id),
  unique (id, establishment_id),
  check (provider ~ '^[a-z][a-z0-9_]{0,63}$'),
  check (length(source_asset_id) between 1 and 500),
  check (source_page_url ~ '^https://'),
  check (license_url ~ '^https://'),
  check (terms_url ~ '^https://'),
  check (latitude is null or latitude between -90 and 90),
  check (longitude is null or longitude between -180 and 180),
  check (distance_meters is null or distance_meters >= 0),
  check (camera_heading_degrees is null or camera_heading_degrees between 0 and 360),
  check (bearing_to_target_degrees is null or bearing_to_target_degrees between 0 and 360),
  check (heading_delta_degrees is null or heading_delta_degrees between 0 and 180),
  check (jsonb_typeof(metadata) = 'object')
);

create index establishment_media_sources_establishment_discovered_idx
  on catalog.establishment_media_sources (establishment_id, discovered_at desc, id);
create index establishment_media_sources_policy_idx
  on catalog.establishment_media_sources (source_policy_id);

create table catalog.establishment_media_source_files (
  id bigint generated always as identity primary key,
  source_id uuid not null
    references catalog.establishment_media_sources(id) on delete restrict,
  bucket_id text not null default 'paloma-establishment-media-sources',
  object_path text not null,
  mime_type text not null,
  width integer not null,
  height integer not null,
  byte_size bigint not null,
  sha256 character(64) not null,
  fetched_at timestamptz not null default now(),
  unique (source_id, sha256),
  unique (bucket_id, object_path),
  check (length(object_path) between 1 and 1_000),
  check (bucket_id = 'paloma-establishment-media-sources'),
  check (mime_type in ('image/jpeg', 'image/png', 'image/webp')),
  check (width > 0 and height > 0),
  check (byte_size > 0),
  check (sha256 ~ '^[0-9a-f]{64}$')
);

create index establishment_media_source_files_source_idx
  on catalog.establishment_media_source_files (source_id, fetched_at desc, id desc);

create table review.establishment_media_source_reviews (
  id bigint generated always as identity primary key,
  source_id uuid not null
    references catalog.establishment_media_sources(id) on delete restrict,
  verdict text not null,
  reviewed_by text not null,
  notes text not null,
  reviewed_at timestamptz not null default now(),
  supersedes_review_id bigint
    references review.establishment_media_source_reviews(id) on delete restrict,
  metadata jsonb not null default '{}'::jsonb,
  check (verdict in (
    'exact_storefront', 'exact_building', 'site_context', 'not_venue', 'unusable'
  )),
  check (length(reviewed_by) between 1 and 200),
  check (length(notes) between 1 and 5_000),
  check (jsonb_typeof(metadata) = 'object')
);

create index establishment_media_source_reviews_source_time_idx
  on review.establishment_media_source_reviews (source_id, reviewed_at desc, id desc);
create index establishment_media_source_reviews_supersedes_idx
  on review.establishment_media_source_reviews (supersedes_review_id)
  where supersedes_review_id is not null;

create or replace view review.current_establishment_media_source_reviews
with (security_invoker = true) as
select distinct on (source_id) *
from review.establishment_media_source_reviews
order by source_id, reviewed_at desc, id desc;

create table catalog.establishment_media_assets (
  id uuid primary key default gen_random_uuid(),
  establishment_id uuid not null
    references public.establishments(id) on delete restrict,
  source_id uuid,
  role text not null default 'cover',
  asset_kind text not null,
  state text not null default 'draft',
  generator text not null,
  generator_version text not null,
  prompt_sha256 character(64) not null,
  input_sha256 character(64),
  attribution_text text,
  disclosure_text text not null,
  output_license_id text not null,
  output_license_url text,
  quality_reviewed_at timestamptz,
  quality_reviewed_by text,
  quality_review_notes text,
  published_at timestamptz,
  retired_at timestamptz,
  created_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb,
  foreign key (source_id, establishment_id)
    references catalog.establishment_media_sources(id, establishment_id) on delete restrict,
  check (role = 'cover'),
  check (asset_kind in (
    'licensed_photo', 'storefront_illustration',
    'location_illustration', 'category_illustration'
  )),
  check (state in (
    'draft', 'rendered', 'quality_approved', 'published', 'retired', 'rejected'
  )),
  check (length(generator) between 1 and 200),
  check (length(generator_version) between 1 and 200),
  check (prompt_sha256 ~ '^[0-9a-f]{64}$'),
  check (input_sha256 is null or input_sha256 ~ '^[0-9a-f]{64}$'),
  check (length(disclosure_text) between 1 and 1_000),
  check (output_license_url is null or output_license_url ~ '^https://'),
  check (jsonb_typeof(metadata) = 'object'),
  check ((state <> 'published') or published_at is not null),
  check ((state <> 'retired') or retired_at is not null),
  check (
    state not in ('quality_approved', 'published')
    or (
      quality_reviewed_at is not null
      and nullif(btrim(quality_reviewed_by), '') is not null
      and nullif(btrim(quality_review_notes), '') is not null
    )
  ),
  check (source_id is null or input_sha256 is not null),
  check (
    (asset_kind = 'category_illustration' and source_id is null)
    or (asset_kind <> 'category_illustration' and source_id is not null)
  )
);

create index establishment_media_assets_establishment_created_idx
  on catalog.establishment_media_assets (establishment_id, created_at desc, id);
create index establishment_media_assets_source_idx
  on catalog.establishment_media_assets (source_id)
  where source_id is not null;
create index establishment_media_assets_review_queue_idx
  on catalog.establishment_media_assets (state, created_at, id)
  where state in ('rendered', 'quality_approved');
create unique index establishment_media_assets_one_published_cover_idx
  on catalog.establishment_media_assets (establishment_id, role)
  where state = 'published';

create table catalog.establishment_media_variants (
  id uuid primary key default gen_random_uuid(),
  asset_id uuid not null
    references catalog.establishment_media_assets(id) on delete restrict,
  variant text not null,
  bucket_id text not null default 'paloma-establishment-media',
  object_path text not null,
  public_url text not null,
  mime_type text not null,
  width integer not null,
  height integer not null,
  byte_size bigint not null,
  sha256 character(64) not null,
  created_at timestamptz not null default now(),
  unique (asset_id, variant),
  unique (bucket_id, object_path),
  check (variant in ('hero', 'card', 'thumbnail')),
  check (length(object_path) between 1 and 1_000),
  check (bucket_id = 'paloma-establishment-media'),
  check (public_url ~ '^https://'),
  check (mime_type in ('image/jpeg', 'image/png', 'image/webp')),
  check (width > 0 and height > 0),
  check (
    (variant = 'hero' and width = 1600 and height = 1000)
    or (variant = 'card' and width = 960 and height = 600)
    or (variant = 'thumbnail' and width = 320 and height = 200)
  ),
  check (byte_size > 0),
  check (sha256 ~ '^[0-9a-f]{64}$')
);

alter table public.establishments
  add column if not exists cover_media_asset_id uuid,
  add column if not exists cover_image_card_url text,
  add column if not exists cover_image_thumbnail_url text,
  add column if not exists cover_image_kind text,
  add column if not exists cover_image_attribution text,
  add column if not exists cover_image_source_url text,
  add column if not exists cover_image_license_id text,
  add column if not exists cover_image_license_url text,
  add column if not exists cover_image_disclosure text,
  add column if not exists cover_image_width integer,
  add column if not exists cover_image_height integer,
  add column if not exists cover_image_updated_at timestamptz;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'establishments_cover_media_asset_id_fkey'
      and conrelid = 'public.establishments'::regclass
  ) then
    alter table public.establishments
      add constraint establishments_cover_media_asset_id_fkey
      foreign key (cover_media_asset_id)
      references catalog.establishment_media_assets(id) on delete restrict;
  end if;
  if not exists (
    select 1 from pg_constraint
    where conname = 'establishments_cover_image_kind_check'
      and conrelid = 'public.establishments'::regclass
  ) then
    alter table public.establishments
      add constraint establishments_cover_image_kind_check check (
        cover_image_kind is null or cover_image_kind in (
          'licensed_photo', 'storefront_illustration',
          'location_illustration', 'category_illustration'
        )
      );
  end if;
  if not exists (
    select 1 from pg_constraint
    where conname = 'establishments_cover_image_dimensions_check'
      and conrelid = 'public.establishments'::regclass
  ) then
    alter table public.establishments
      add constraint establishments_cover_image_dimensions_check check (
        (cover_image_width is null and cover_image_height is null)
        or (cover_image_width > 0 and cover_image_height > 0)
      );
  end if;
end
$$;

create index establishments_cover_media_asset_idx
  on public.establishments (cover_media_asset_id)
  where cover_media_asset_id is not null;

create or replace function governance.enforce_establishment_media_source_rights()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_policy governance.source_policy_versions%rowtype;
  v_field_policy governance.source_field_policies%rowtype;
begin
  select * into v_policy
  from governance.source_policy_versions
  where id = new.source_policy_id and source = new.provider;
  if not found then
    raise exception 'unknown or mismatched media source policy for %', new.provider;
  end if;

  select * into v_field_policy
  from governance.source_field_policies
  where source_policy_id = new.source_policy_id
    and field_name = 'cover_image_url';
  if not found
     or v_policy.effective_from > new.discovered_at
     or (v_policy.effective_to is not null and v_policy.effective_to <= new.discovered_at)
     or not v_policy.raw_persistence_allowed
     or not v_policy.canonical_derivation_allowed
     or not v_policy.display_allowed
     or not v_field_policy.durable_storage_allowed
     or not v_field_policy.canonical_derivation_allowed
     or not v_field_policy.display_allowed then
    raise exception 'durable media use is not allowed by policy for %', new.provider;
  end if;

  if not (new.license_id = any(v_field_policy.allowed_license_ids)) then
    raise exception 'unapproved media license for %: %', new.provider, new.license_id;
  end if;
  if not new.commercial_use_allowed
     or not new.derivatives_allowed
     or not new.raw_persistence_allowed then
    raise exception 'media source does not grant all required durable derivative rights';
  end if;
  return new;
end;
$$;

create trigger enforce_establishment_media_source_rights
before insert on catalog.establishment_media_sources
for each row execute function governance.enforce_establishment_media_source_rights();

create or replace function governance.enforce_establishment_media_asset_lifecycle()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    if new.state not in ('draft', 'rendered') then
      raise exception 'new media assets must begin in draft or rendered state';
    end if;
    if new.quality_reviewed_at is not null
       or new.quality_reviewed_by is not null
       or new.quality_review_notes is not null
       or new.published_at is not null
       or new.retired_at is not null then
      raise exception 'new media assets cannot carry lifecycle review timestamps';
    end if;
    return new;
  end if;

  if row(
       old.establishment_id, old.source_id, old.role, old.asset_kind,
       old.generator, old.generator_version, old.prompt_sha256, old.input_sha256,
       old.attribution_text, old.disclosure_text, old.output_license_id,
       old.output_license_url, old.created_at, old.metadata
     ) is distinct from row(
       new.establishment_id, new.source_id, new.role, new.asset_kind,
       new.generator, new.generator_version, new.prompt_sha256, new.input_sha256,
       new.attribution_text, new.disclosure_text, new.output_license_id,
       new.output_license_url, new.created_at, new.metadata
     ) then
    raise exception 'media asset identity, provenance, and disclosure are immutable';
  end if;

  if not (
    (old.state = 'draft' and new.state in ('rendered', 'rejected'))
    or (old.state = 'rendered' and new.state in ('quality_approved', 'rejected'))
    or (old.state = 'quality_approved' and new.state in ('published', 'rejected'))
    or (old.state = 'published' and new.state = 'retired')
  ) then
    raise exception 'invalid media asset state transition: % -> %', old.state, new.state;
  end if;

  if new.state = 'quality_approved' and (
    new.quality_reviewed_at is null
    or nullif(btrim(new.quality_reviewed_by), '') is null
    or nullif(btrim(new.quality_review_notes), '') is null
  ) then
    raise exception 'quality approval requires reviewer, notes, and timestamp';
  end if;
  if new.state = 'published' and new.published_at is null then
    raise exception 'publication requires a timestamp';
  end if;
  if new.state = 'retired' and new.retired_at is null then
    raise exception 'retirement requires a timestamp';
  end if;
  return new;
end;
$$;

create trigger enforce_establishment_media_asset_lifecycle
before insert or update on catalog.establishment_media_assets
for each row execute function governance.enforce_establishment_media_asset_lifecycle();

create trigger preserve_establishment_media_sources
before update or delete on catalog.establishment_media_sources
for each row execute function governance.reject_append_only_mutation();
create trigger preserve_establishment_media_source_files
before update or delete on catalog.establishment_media_source_files
for each row execute function governance.reject_append_only_mutation();
create trigger preserve_establishment_media_source_reviews
before update or delete on review.establishment_media_source_reviews
for each row execute function governance.reject_append_only_mutation();
create trigger preserve_establishment_media_variants
before update or delete on catalog.establishment_media_variants
for each row execute function governance.reject_append_only_mutation();

create or replace function catalog.approve_establishment_media_asset(
  p_asset_id uuid,
  p_reviewed_by text,
  p_notes text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
begin
  if nullif(btrim(p_reviewed_by), '') is null
     or length(p_reviewed_by) > 200 then
    raise exception 'quality reviewer is required and must not exceed 200 characters';
  end if;
  if nullif(btrim(p_notes), '') is null or length(p_notes) > 5_000 then
    raise exception 'quality review notes are required and must not exceed 5000 characters';
  end if;

  update catalog.establishment_media_assets
  set state = 'quality_approved',
      quality_reviewed_at = now(),
      quality_reviewed_by = btrim(p_reviewed_by),
      quality_review_notes = btrim(p_notes)
  where id = p_asset_id and state = 'rendered';
  if not found then
    raise exception 'media asset % must be rendered before quality approval', p_asset_id;
  end if;
  return p_asset_id;
end;
$$;

create or replace function catalog.publish_establishment_cover_media(p_asset_id uuid)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_asset catalog.establishment_media_assets%rowtype;
  v_source catalog.establishment_media_sources%rowtype;
  v_review review.establishment_media_source_reviews%rowtype;
  v_hero catalog.establishment_media_variants%rowtype;
  v_card catalog.establishment_media_variants%rowtype;
  v_thumbnail catalog.establishment_media_variants%rowtype;
begin
  select * into v_asset
  from catalog.establishment_media_assets
  where id = p_asset_id
  for update;
  if not found then
    raise exception 'unknown media asset %', p_asset_id;
  end if;
  if v_asset.state <> 'quality_approved' then
    raise exception 'media asset % must be quality_approved before publication', p_asset_id;
  end if;

  perform 1
  from public.establishments
  where id = v_asset.establishment_id and publication_state = 'published'
  for update;
  if not found then
    raise exception 'media asset establishment must currently be published';
  end if;

  if v_asset.source_id is null then
    if v_asset.asset_kind <> 'category_illustration' then
      raise exception 'only category illustrations may publish without a reviewed source';
    end if;
  else
    select * into v_source
    from catalog.establishment_media_sources
    where id = v_asset.source_id
      and establishment_id = v_asset.establishment_id;
    if not found then
      raise exception 'media source does not belong to the asset establishment';
    end if;

    select * into v_review
    from review.establishment_media_source_reviews
    where source_id = v_source.id
    order by reviewed_at desc, id desc
    limit 1;
    if not found then
      raise exception 'media source has no identity review';
    end if;
    if v_asset.asset_kind = 'storefront_illustration'
       and v_review.verdict <> 'exact_storefront' then
      raise exception 'storefront illustration requires an exact_storefront source review';
    end if;
    if v_asset.asset_kind = 'licensed_photo'
       and v_review.verdict not in ('exact_storefront', 'exact_building') then
      raise exception 'licensed cover photo requires an exact storefront or building source';
    end if;
    if v_asset.asset_kind = 'location_illustration'
       and v_review.verdict not in ('exact_storefront', 'exact_building', 'site_context') then
      raise exception 'location illustration requires an approved location source';
    end if;
    if v_source.attribution_required
       and nullif(btrim(v_asset.attribution_text), '') is null then
      raise exception 'source attribution is required';
    end if;
    if v_source.share_alike_required
       and v_asset.output_license_id <> v_source.license_id then
      raise exception 'share-alike derivative must retain source license %', v_source.license_id;
    end if;
    if v_asset.output_license_url is null then
      raise exception 'source-derived media must expose its output license URL';
    end if;
  end if;

  select * into v_hero
  from catalog.establishment_media_variants
  where asset_id = p_asset_id and variant = 'hero';
  select * into v_card
  from catalog.establishment_media_variants
  where asset_id = p_asset_id and variant = 'card';
  select * into v_thumbnail
  from catalog.establishment_media_variants
  where asset_id = p_asset_id and variant = 'thumbnail';
  if v_hero.id is null or v_card.id is null or v_thumbnail.id is null then
    raise exception 'hero, card, and thumbnail variants are all required';
  end if;

  update catalog.establishment_media_assets
  set state = 'retired', retired_at = now()
  where establishment_id = v_asset.establishment_id
    and role = 'cover'
    and state = 'published'
    and id <> p_asset_id;

  update catalog.establishment_media_assets
  set state = 'published', published_at = now(), retired_at = null
  where id = p_asset_id;

  update public.establishments
  set cover_media_asset_id = p_asset_id,
      cover_image_url = v_hero.public_url,
      cover_image_card_url = v_card.public_url,
      cover_image_thumbnail_url = v_thumbnail.public_url,
      cover_image_kind = v_asset.asset_kind,
      cover_image_attribution = v_asset.attribution_text,
      cover_image_source_url = case
        when v_asset.source_id is null then null else v_source.source_page_url end,
      cover_image_license_id = v_asset.output_license_id,
      cover_image_license_url = v_asset.output_license_url,
      cover_image_disclosure = v_asset.disclosure_text,
      cover_image_width = v_hero.width,
      cover_image_height = v_hero.height,
      cover_image_updated_at = now()
  where id = v_asset.establishment_id;

  return p_asset_id;
end;
$$;

create or replace view catalog.establishment_media_work_queue
with (security_invoker = true) as
with source_rollup as (
  select source.establishment_id,
         count(*) filter (where current_review.id is null) as unreviewed_sources,
         count(*) filter (
           where current_review.verdict in ('exact_storefront', 'exact_building', 'site_context')
         ) as approved_sources
  from catalog.establishment_media_sources source
  left join review.current_establishment_media_source_reviews current_review
    on current_review.source_id = source.id
  group by source.establishment_id
), asset_rollup as (
  select establishment_id,
         count(*) filter (where state in ('draft', 'rendered')) as in_progress_assets,
         count(*) filter (where state = 'quality_approved') as publishable_assets,
         count(*) filter (where state = 'published') as published_assets
  from catalog.establishment_media_assets
  group by establishment_id
)
select establishment.id as establishment_id,
       establishment.name,
       establishment.city,
       establishment.primary_type_id,
       case
         when coalesce(asset.published_assets, 0) > 0 then 'published'
         when coalesce(asset.publishable_assets, 0) > 0 then 'ready_to_publish'
         when coalesce(asset.in_progress_assets, 0) > 0 then 'generation_or_quality_review'
         when coalesce(source.approved_sources, 0) > 0 then 'ready_for_generation'
         when coalesce(source.unreviewed_sources, 0) > 0 then 'needs_identity_review'
         else 'needs_source_discovery'
       end as next_action,
       coalesce(source.unreviewed_sources, 0) as unreviewed_source_count,
       coalesce(source.approved_sources, 0) as approved_source_count,
       coalesce(asset.in_progress_assets, 0) as in_progress_asset_count,
       coalesce(asset.publishable_assets, 0) as publishable_asset_count,
       coalesce(asset.published_assets, 0) as published_asset_count
from public.establishments establishment
left join source_rollup source on source.establishment_id = establishment.id
left join asset_rollup asset on asset.establishment_id = establishment.id
where establishment.publication_state = 'published';

insert into storage.buckets (
  id, name, public, file_size_limit, allowed_mime_types
) values
  (
    'paloma-establishment-media-sources',
    'paloma-establishment-media-sources', false, 20971520,
    array['image/jpeg', 'image/png', 'image/webp']
  ),
  (
    'paloma-establishment-media',
    'paloma-establishment-media', true, 6291456,
    array['image/jpeg', 'image/png', 'image/webp']
  )
on conflict (id) do update
set public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

alter table catalog.establishment_media_sources enable row level security;
alter table catalog.establishment_media_source_files enable row level security;
alter table catalog.establishment_media_assets enable row level security;
alter table catalog.establishment_media_variants enable row level security;
alter table review.establishment_media_source_reviews enable row level security;

revoke all on catalog.establishment_media_sources,
  catalog.establishment_media_source_files,
  catalog.establishment_media_assets,
  catalog.establishment_media_variants,
  review.establishment_media_source_reviews
  from public, anon, authenticated, service_role, paloma_ingest;
revoke all on sequence catalog.establishment_media_source_files_id_seq,
  review.establishment_media_source_reviews_id_seq
  from public, anon, authenticated, service_role, paloma_ingest;
revoke execute on function governance.enforce_establishment_media_source_rights(),
  governance.enforce_establishment_media_asset_lifecycle(),
  catalog.approve_establishment_media_asset(uuid, text, text),
  catalog.publish_establishment_cover_media(uuid)
  from public, anon, authenticated, service_role, paloma_ingest;

grant select, insert on catalog.establishment_media_sources,
  catalog.establishment_media_source_files,
  catalog.establishment_media_variants,
  review.establishment_media_source_reviews to paloma_ingest;
grant select, insert on catalog.establishment_media_assets to paloma_ingest;
grant select on review.current_establishment_media_source_reviews,
  catalog.establishment_media_work_queue to paloma_ingest;
grant usage, select on sequence catalog.establishment_media_source_files_id_seq,
  review.establishment_media_source_reviews_id_seq to paloma_ingest;
grant execute on function governance.enforce_establishment_media_source_rights(),
  governance.enforce_establishment_media_asset_lifecycle(),
  catalog.approve_establishment_media_asset(uuid, text, text),
  catalog.publish_establishment_cover_media(uuid) to paloma_ingest;

create policy paloma_ingest_read_establishment_media_sources
  on catalog.establishment_media_sources for select to paloma_ingest using (true);
create policy paloma_ingest_insert_establishment_media_sources
  on catalog.establishment_media_sources for insert to paloma_ingest with check (true);
create policy paloma_ingest_read_establishment_media_source_files
  on catalog.establishment_media_source_files for select to paloma_ingest using (true);
create policy paloma_ingest_insert_establishment_media_source_files
  on catalog.establishment_media_source_files for insert to paloma_ingest with check (true);
create policy paloma_ingest_read_establishment_media_reviews
  on review.establishment_media_source_reviews for select to paloma_ingest using (true);
create policy paloma_ingest_insert_establishment_media_reviews
  on review.establishment_media_source_reviews for insert to paloma_ingest with check (true);
create policy paloma_ingest_read_establishment_media_assets
  on catalog.establishment_media_assets for select to paloma_ingest using (true);
create policy paloma_ingest_insert_establishment_media_assets
  on catalog.establishment_media_assets for insert to paloma_ingest with check (true);
create policy paloma_ingest_read_establishment_media_variants
  on catalog.establishment_media_variants for select to paloma_ingest using (true);
create policy paloma_ingest_insert_establishment_media_variants
  on catalog.establishment_media_variants for insert to paloma_ingest with check (true);

comment on table catalog.establishment_media_sources is
  'Immutable rights-qualified source candidates. Proximity is never treated as establishment identity.';
comment on table review.establishment_media_source_reviews is
  'Append-only visual identity decisions for media sources.';
comment on table catalog.establishment_media_assets is
  'Generated or licensed establishment media with an explicit quality and publication lifecycle.';
comment on table catalog.establishment_media_variants is
  'Immutable, content-addressed public derivatives for hero, card, and thumbnail use.';
comment on function catalog.publish_establishment_cover_media(uuid) is
  'Atomically publishes a reviewed asset and projects its responsive URLs and provenance to the public establishment.';
comment on function catalog.approve_establishment_media_asset(uuid, text, text) is
  'Advances one rendered asset through the independent quality-review gate.';
comment on column public.establishments.cover_image_disclosure is
  'User-facing distinction between a photo, storefront illustration, location illustration, and category concept.';
