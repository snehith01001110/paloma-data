-- A compact, source-owned storefront set gives every published establishment a
-- durable cover immediately. Templates are shared intentionally: exact or
-- venue-specific media can supersede them without duplicating the fallback
-- objects for every establishment.

create table catalog.establishment_media_templates (
  template_key text primary key,
  asset_kind text not null default 'category_illustration',
  generator text not null,
  generator_version text not null,
  prompt_sha256 character(64) not null,
  source_master_sha256 character(64) not null,
  disclosure_text text not null,
  output_license_id text not null,
  quality_reviewed_at timestamptz not null,
  quality_reviewed_by text not null,
  quality_review_notes text not null,
  created_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb,
  check (template_key ~ '^(family|category)-[a-z0-9]+(-[a-z0-9]+)*$'),
  check (asset_kind = 'category_illustration'),
  check (length(generator) between 1 and 200),
  check (length(generator_version) between 1 and 200),
  check (prompt_sha256 ~ '^[0-9a-f]{64}$'),
  check (source_master_sha256 ~ '^[0-9a-f]{64}$'),
  check (length(disclosure_text) between 1 and 1000),
  check (length(quality_reviewed_by) between 1 and 200),
  check (length(quality_review_notes) between 1 and 5000),
  check (jsonb_typeof(metadata) = 'object')
);

create table catalog.establishment_media_template_variants (
  id uuid primary key default gen_random_uuid(),
  template_key text not null
    references catalog.establishment_media_templates(template_key) on delete restrict,
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
  unique (template_key, variant),
  unique (bucket_id, object_path),
  check (variant in ('hero', 'card', 'thumbnail')),
  check (bucket_id = 'paloma-establishment-media'),
  check (length(object_path) between 1 and 1000),
  check (public_url ~ '^https://'),
  check (mime_type = 'image/jpeg'),
  check (
    (variant = 'hero' and width = 1600 and height = 1000)
    or (variant = 'card' and width = 960 and height = 600)
    or (variant = 'thumbnail' and width = 320 and height = 200)
  ),
  check (byte_size > 0),
  check (sha256 ~ '^[0-9a-f]{64}$')
);

insert into catalog.establishment_media_templates (
  template_key, generator, generator_version, prompt_sha256,
  source_master_sha256, disclosure_text, output_license_id,
  quality_reviewed_at, quality_reviewed_by, quality_review_notes, metadata
) values
  (
    'family-spirits', 'openai-imagegen', 'codex-builtin',
    '8ac6d33f8a5471ab90fe7e122be20b313271f73c37ac2e4108a78960b8cbeb4e',
    '06c54be5e628758a7217de08e57987877ae35cd99d001d7703c5b8fb9a2b6dc6',
    'Paloma concept artwork; not a photograph or depiction of this establishment',
    'Paloma-Proprietary', now(), 'codex-media-qa',
    'Reviewed for clean 8:5 crops, legibility under overlays, absence of names and logos, and clear generic storefront identity.',
    '{"release":"fallback-storefronts-v1","family":"spirits","not_venue_depiction":true}'
  ),
  (
    'family-beer', 'openai-imagegen', 'codex-builtin',
    '392b71fe494fda7b2e155871f97aab3f9980fc635721e65f64f895ec582cab27',
    '42bda9021c2d6deaa6aa0b4f5ebae3d1fd6405c9cab19c6b26f1a94a05087e3c',
    'Paloma concept artwork; not a photograph or depiction of this establishment',
    'Paloma-Proprietary', now(), 'codex-media-qa',
    'Reviewed for clean 8:5 crops, legibility under overlays, absence of names and logos, and recognizable brewery cues.',
    '{"release":"fallback-storefronts-v1","family":"beer","style_reference":"family-spirits","not_venue_depiction":true}'
  ),
  (
    'family-wine', 'openai-imagegen', 'codex-builtin',
    'e84a5c90e71b9c6a63f9397400beec689840f93138adfaf42040a2da1d9cc4bf',
    '7fba5e4dabd3c84c3a4cf617ecfa3633a2cf1c88176945587e5696372aa02aeb',
    'Paloma concept artwork; not a photograph or depiction of this establishment',
    'Paloma-Proprietary', now(), 'codex-media-qa',
    'Reviewed for clean 8:5 crops, legibility under overlays, absence of names and logos, and recognizable wine-bar cues.',
    '{"release":"fallback-storefronts-v1","family":"wine","style_reference":"family-spirits","not_venue_depiction":true}'
  ),
  (
    'family-other', 'openai-imagegen', 'codex-builtin',
    '821bb96c33611735f9b7c144602920472fe22262d7b9b6c68ec57b0f3a281714',
    'c901d7231235390fa0896a0bf8e28caddfa1be3c4ac2b3e3431b231e3052661c',
    'Paloma concept artwork; not a photograph or depiction of this establishment',
    'Paloma-Proprietary', now(), 'codex-media-qa',
    'Reviewed for clean 8:5 crops, legibility under overlays, absence of names and logos, and broad neighborhood-bar applicability.',
    '{"release":"fallback-storefronts-v1","family":"other","style_reference":"family-spirits","not_venue_depiction":true}'
  ),
  (
    'category-billiards-bar', 'openai-imagegen', 'codex-builtin',
    '191c1561665703ec5594044b697955a17d1142c665ef157c0c19d9936d76e037',
    '4e9cff04d5ed9f5fce3476c5e408205ea4cea5b662dce8388b41fd6ffe63e1ce',
    'Paloma billiards concept artwork; not a photograph or depiction of this establishment',
    'Paloma-Proprietary', now(), 'codex-media-qa',
    'Reviewed for clean 8:5 crops, legibility under overlays, absence of names and logos, and recognizable billiards cues.',
    '{"release":"fallback-storefronts-v1","category":"billiards_bar","style_reference":"family-spirits","not_venue_depiction":true}'
  );

insert into catalog.establishment_media_template_variants (
  template_key, variant, object_path, public_url, mime_type,
  width, height, byte_size, sha256
) values
  (
    'family-spirits', 'hero',
    'templates/v1/family-spirits/hero-06c54be5e628758a.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-spirits/hero-06c54be5e628758a.jpg',
    'image/jpeg', 1600, 1000, 345214,
    '06c54be5e628758a7217de08e57987877ae35cd99d001d7703c5b8fb9a2b6dc6'
  ),
  (
    'family-spirits', 'card',
    'templates/v1/family-spirits/card-e46751b663f2afed.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-spirits/card-e46751b663f2afed.jpg',
    'image/jpeg', 960, 600, 120665,
    'e46751b663f2afede7f14e5b46a202c50d1fd6486efd55a535c1e99f25d34c7c'
  ),
  (
    'family-spirits', 'thumbnail',
    'templates/v1/family-spirits/thumbnail-049d40d654c388c1.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-spirits/thumbnail-049d40d654c388c1.jpg',
    'image/jpeg', 320, 200, 12810,
    '049d40d654c388c1492346907398100fc1e06055569c2a03a8de6ee28f470c4a'
  ),
  (
    'family-beer', 'hero',
    'templates/v1/family-beer/hero-42bda9021c2d6dea.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-beer/hero-42bda9021c2d6dea.jpg',
    'image/jpeg', 1600, 1000, 360362,
    '42bda9021c2d6deaa6aa0b4f5ebae3d1fd6405c9cab19c6b26f1a94a05087e3c'
  ),
  (
    'family-beer', 'card',
    'templates/v1/family-beer/card-eeb35d924b0f2e9c.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-beer/card-eeb35d924b0f2e9c.jpg',
    'image/jpeg', 960, 600, 126303,
    'eeb35d924b0f2e9cefef7526fed66556b7193e0519ec673d890ab64e922836f2'
  ),
  (
    'family-beer', 'thumbnail',
    'templates/v1/family-beer/thumbnail-0f9bb6962d936989.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-beer/thumbnail-0f9bb6962d936989.jpg',
    'image/jpeg', 320, 200, 12180,
    '0f9bb6962d93698945e0eb81ae72ce361f139880ce6aba8ccbe7dcfcba68e081'
  ),
  (
    'family-wine', 'hero',
    'templates/v1/family-wine/hero-7fba5e4dabd3c84c.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-wine/hero-7fba5e4dabd3c84c.jpg',
    'image/jpeg', 1600, 1000, 316298,
    '7fba5e4dabd3c84c3a4cf617ecfa3633a2cf1c88176945587e5696372aa02aeb'
  ),
  (
    'family-wine', 'card',
    'templates/v1/family-wine/card-cc1a0d19a375cc48.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-wine/card-cc1a0d19a375cc48.jpg',
    'image/jpeg', 960, 600, 115046,
    'cc1a0d19a375cc48e6d5f3a9e043d23eb10a488aca1b96c0e26efee03a5ec8ed'
  ),
  (
    'family-wine', 'thumbnail',
    'templates/v1/family-wine/thumbnail-8d7219759585bc56.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-wine/thumbnail-8d7219759585bc56.jpg',
    'image/jpeg', 320, 200, 11639,
    '8d7219759585bc56944ac87bc74c5018cc007cf83ef894a7d55883bb9280270e'
  ),
  (
    'family-other', 'hero',
    'templates/v1/family-other/hero-c901d7231235390f.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-other/hero-c901d7231235390f.jpg',
    'image/jpeg', 1600, 1000, 309597,
    'c901d7231235390fa0896a0bf8e28caddfa1be3c4ac2b3e3431b231e3052661c'
  ),
  (
    'family-other', 'card',
    'templates/v1/family-other/card-636561fced7ec9ed.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-other/card-636561fced7ec9ed.jpg',
    'image/jpeg', 960, 600, 106954,
    '636561fced7ec9edabdc145a83a15be7210745b5c0de268ff2e6ed45cac2810d'
  ),
  (
    'family-other', 'thumbnail',
    'templates/v1/family-other/thumbnail-56c7e4f87bc0df43.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/family-other/thumbnail-56c7e4f87bc0df43.jpg',
    'image/jpeg', 320, 200, 10873,
    '56c7e4f87bc0df43dd59d1732c709ca4709903e6d667e0bb57409a5ecc82bc27'
  ),
  (
    'category-billiards-bar', 'hero',
    'templates/v1/category-billiards-bar/hero-4e9cff04d5ed9f5f.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/category-billiards-bar/hero-4e9cff04d5ed9f5f.jpg',
    'image/jpeg', 1600, 1000, 304492,
    '4e9cff04d5ed9f5fce3476c5e408205ea4cea5b662dce8388b41fd6ffe63e1ce'
  ),
  (
    'category-billiards-bar', 'card',
    'templates/v1/category-billiards-bar/card-787464b3a614f8a9.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/category-billiards-bar/card-787464b3a614f8a9.jpg',
    'image/jpeg', 960, 600, 108230,
    '787464b3a614f8a9ee198ab481d73d7f24c140d623bcd1032d23110103114361'
  ),
  (
    'category-billiards-bar', 'thumbnail',
    'templates/v1/category-billiards-bar/thumbnail-bf964a37487b4098.jpg',
    'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v1/category-billiards-bar/thumbnail-bf964a37487b4098.jpg',
    'image/jpeg', 320, 200, 11264,
    'bf964a37487b40986a8adaaa61514ce90fd7db7237c965a742cc68a0468aa366'
  );

alter table public.establishments
  add column cover_media_template_key text;

alter table public.establishments
  add constraint establishments_cover_media_template_key_fkey
  foreign key (cover_media_template_key)
  references catalog.establishment_media_templates(template_key) on delete restrict;

alter table public.establishments
  add constraint establishments_one_cover_media_origin_check check (
    cover_media_asset_id is null or cover_media_template_key is null
  );

create index establishments_cover_media_template_idx
  on public.establishments (cover_media_template_key)
  where cover_media_template_key is not null;

create or replace function catalog.default_establishment_media_template(
  p_primary_type_id smallint
)
returns text
language sql
stable
security invoker
set search_path = ''
as $$
  select case replace(primary_type.slug, '_', '-')
    when 'billiards-bar' then 'category-billiards-bar'
    when 'cocktail-bar' then 'family-spirits'
    when 'lounge' then 'family-spirits'
    when 'nightclub' then 'family-spirits'
    when 'distillery' then 'family-spirits'
    when 'wine-bar' then 'family-wine'
    when 'winery' then 'family-wine'
    when 'tasting-room' then 'family-wine'
    when 'brewery' then 'family-beer'
    when 'taproom' then 'family-beer'
    when 'brewpub' then 'family-beer'
    when 'beer-bar' then 'family-beer'
    when 'pub' then 'family-beer'
    when 'dive-bar' then 'family-beer'
    when 'sports-bar' then 'family-beer'
    else 'family-other'
  end
  from public.primary_types primary_type
  where primary_type.id = p_primary_type_id
$$;

create or replace function catalog.assign_default_establishment_cover()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_template catalog.establishment_media_templates%rowtype;
  v_hero catalog.establishment_media_template_variants%rowtype;
  v_card catalog.establishment_media_template_variants%rowtype;
  v_thumbnail catalog.establishment_media_template_variants%rowtype;
  v_template_key text;
begin
  if new.cover_media_asset_id is not null then
    new.cover_media_template_key := null;
    return new;
  end if;
  if new.publication_state <> 'published' then
    return new;
  end if;
  if new.cover_image_url is not null and new.cover_media_template_key is null then
    return new;
  end if;
  if tg_op = 'UPDATE'
     and new.primary_type_id = old.primary_type_id
     and new.cover_media_template_key is not null
     and new.cover_image_url is not null then
    return new;
  end if;

  v_template_key := catalog.default_establishment_media_template(new.primary_type_id);
  select * into v_template
  from catalog.establishment_media_templates
  where template_key = v_template_key;
  if not found then
    return new;
  end if;
  select * into v_hero
  from catalog.establishment_media_template_variants
  where template_key = v_template_key and variant = 'hero';
  select * into v_card
  from catalog.establishment_media_template_variants
  where template_key = v_template_key and variant = 'card';
  select * into v_thumbnail
  from catalog.establishment_media_template_variants
  where template_key = v_template_key and variant = 'thumbnail';
  if v_hero.id is null or v_card.id is null or v_thumbnail.id is null then
    raise exception 'media template % is missing a responsive variant', v_template_key;
  end if;

  new.cover_media_template_key := v_template_key;
  new.cover_image_url := v_hero.public_url;
  new.cover_image_card_url := v_card.public_url;
  new.cover_image_thumbnail_url := v_thumbnail.public_url;
  new.cover_image_kind := v_template.asset_kind;
  new.cover_image_attribution := null;
  new.cover_image_source_url := null;
  new.cover_image_license_id := v_template.output_license_id;
  new.cover_image_license_url := null;
  new.cover_image_disclosure := v_template.disclosure_text;
  new.cover_image_width := v_hero.width;
  new.cover_image_height := v_hero.height;
  new.cover_image_updated_at := now();
  return new;
end;
$$;

create trigger assign_default_establishment_cover
before insert or update of publication_state, primary_type_id,
  cover_media_asset_id, cover_image_url
on public.establishments
for each row execute function catalog.assign_default_establishment_cover();

with desired as (
  select establishment.id,
         catalog.default_establishment_media_template(establishment.primary_type_id)
           as template_key
  from public.establishments establishment
  where establishment.publication_state = 'published'
    and establishment.cover_media_asset_id is null
    and establishment.cover_image_url is null
), variants as (
  select template.template_key,
         template.asset_kind,
         template.disclosure_text,
         template.output_license_id,
         max(variant.public_url) filter (where variant.variant = 'hero') as hero_url,
         max(variant.public_url) filter (where variant.variant = 'card') as card_url,
         max(variant.public_url) filter (where variant.variant = 'thumbnail') as thumbnail_url
  from catalog.establishment_media_templates template
  join catalog.establishment_media_template_variants variant
    on variant.template_key = template.template_key
  group by template.template_key, template.asset_kind,
           template.disclosure_text, template.output_license_id
)
update public.establishments establishment
set cover_media_template_key = desired.template_key,
    cover_image_url = variants.hero_url,
    cover_image_card_url = variants.card_url,
    cover_image_thumbnail_url = variants.thumbnail_url,
    cover_image_kind = variants.asset_kind,
    cover_image_attribution = null,
    cover_image_source_url = null,
    cover_image_license_id = variants.output_license_id,
    cover_image_license_url = null,
    cover_image_disclosure = variants.disclosure_text,
    cover_image_width = 1600,
    cover_image_height = 1000,
    cover_image_updated_at = now()
from desired
join variants on variants.template_key = desired.template_key
where establishment.id = desired.id;

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
         else 'needs_venue_concept_generation'
       end as next_action,
       coalesce(source.unreviewed_sources, 0) as unreviewed_source_count,
       coalesce(source.approved_sources, 0) as approved_source_count,
       coalesce(asset.in_progress_assets, 0) as in_progress_asset_count,
       coalesce(asset.publishable_assets, 0) as publishable_asset_count,
       coalesce(asset.published_assets, 0) as published_asset_count,
       establishment.cover_media_template_key,
       case
         when coalesce(source.approved_sources, 0) > 0 then 'source_ready'
         when coalesce(source.unreviewed_sources, 0) > 0 then 'needs_identity_review'
         else 'needs_source_discovery'
       end as source_upgrade_action
from public.establishments establishment
left join source_rollup source on source.establishment_id = establishment.id
left join asset_rollup asset on asset.establishment_id = establishment.id
where establishment.publication_state = 'published';

alter table catalog.establishment_media_templates enable row level security;
alter table catalog.establishment_media_template_variants enable row level security;

revoke all on catalog.establishment_media_templates,
  catalog.establishment_media_template_variants
  from public, anon, authenticated, service_role, paloma_ingest;
revoke execute on function
  catalog.default_establishment_media_template(smallint),
  catalog.assign_default_establishment_cover()
  from public, anon, authenticated, service_role, paloma_ingest;

grant select on catalog.establishment_media_templates,
  catalog.establishment_media_template_variants to paloma_ingest;
grant execute on function
  catalog.default_establishment_media_template(smallint),
  catalog.assign_default_establishment_cover() to paloma_ingest;

create policy paloma_ingest_read_establishment_media_templates
  on catalog.establishment_media_templates for select to paloma_ingest using (true);
create policy paloma_ingest_read_establishment_media_template_variants
  on catalog.establishment_media_template_variants for select to paloma_ingest using (true);

comment on table catalog.establishment_media_templates is
  'Reviewed source-owned fallback concepts shared by establishments until venue-specific media is published.';
comment on table catalog.establishment_media_template_variants is
  'Immutable responsive Storage objects for a shared fallback concept.';
comment on column public.establishments.cover_media_template_key is
  'Durable shared cover provenance; mutually exclusive with a venue-specific media asset.';
comment on function catalog.assign_default_establishment_cover() is
  'Assigns a reviewed fallback template when a published establishment has no venue-specific cover.';

notify pgrst, 'reload schema';
