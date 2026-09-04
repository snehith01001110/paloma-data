-- Give each active primary type its own reviewed category concept. The broad
-- v1 family templates remain as a safe fallback for unknown future types.

with release (
  template_key, category_slug, prompt_sha256, source_master_sha256,
  style_reference, hero_sha256, hero_bytes, card_sha256, card_bytes,
  thumbnail_sha256, thumbnail_bytes, extra_metadata
) as (
  values
    (
      'category-bar', 'bar',
      '390c145f2fedb37eb1122de3ac80ae4714620f74fcbe017b5a6063e0e1142e3c',
      '062389ab81a9b8c76502c1a049e7e328dd31ace0711f8c284feacf5da77b65f9',
      'family-other',
      '062389ab81a9b8c76502c1a049e7e328dd31ace0711f8c284feacf5da77b65f9', 418241,
      'a40514f09f9c04509b097ca80d27e77fcbdb8b94dbd9c16a6994b2bee9d67431', 145165,
      '9910504cd9f9019858d7aef9aeed4c85104d700245186377efb58023522c50ad', 13061,
      '{}'::jsonb
    ),
    (
      'category-beer-bar', 'beer_bar',
      '23c3f2993e965728288a8a6a702c9eb69ce1c3468a90e453d9b1445c42ad64b4',
      'c698c60ef7b4071e408cdb3f1ff1969011982c9855018f4a9c0db64f0555f49c',
      'family-beer',
      'c698c60ef7b4071e408cdb3f1ff1969011982c9855018f4a9c0db64f0555f49c', 445859,
      '7293df72a5f93640a85f9ad3aea63764125a6d75540cd24b7fd794216f636ac7', 161041,
      '9cb0d2917c4344704be8af94376074d3bd6d24af287d21c338ad1adadc13d503', 15900,
      '{}'::jsonb
    ),
    (
      'category-brewery', 'brewery',
      '6f92b51712501c0e2f8025431735c25e8d5715a8b3d3b68d9d3e7d2f5374335b',
      '2f0f459c2ec9a96410e23496b60d0309c6f880652ea0220fab25564d43714cc5',
      'family-beer',
      '2f0f459c2ec9a96410e23496b60d0309c6f880652ea0220fab25564d43714cc5', 461238,
      'a6cf92d8832b4e7bc590c3f0e3a030318beaf6fb29af0ad91e92651b2a78e5a3', 164875,
      '74b277d235628fb1d9ddb8e60e2dabfb4d048acc52d8d79c48a7b6c4e69911b4', 15522,
      '{}'::jsonb
    ),
    (
      'category-brewpub', 'brewpub',
      '08a944317035a83ae7093db10ef0ae618f397c3cc3ed7ab139d9514ba3d88188',
      '7db12ec3bdcf9e2d750bdef94269a268fb17a7ff8baf73f6a0590e21adf3711b',
      'family-beer',
      '7db12ec3bdcf9e2d750bdef94269a268fb17a7ff8baf73f6a0590e21adf3711b', 462260,
      '67dd9e7fddca57e066bd1000013291baf13edbe898f9641f74d9e2e22809b24b', 164898,
      'f56a2f8d3c7410e95bb308e1d5cc207f3b436cf19935045cceb161fb5d8826d8', 16179,
      '{}'::jsonb
    ),
    (
      'category-cocktail-bar', 'cocktail_bar',
      'd2cdc1293ebbc1da773f31ff6b8f593b8c43f4431e330c096553432531bcc146',
      '034caacbcada0e8538bbc3074d811bafac2a028c44593ddfddc3751bb7a144ae',
      'family-spirits',
      '034caacbcada0e8538bbc3074d811bafac2a028c44593ddfddc3751bb7a144ae', 311227,
      '45d0bba3ae1104473bf676608a7321a02e859d7f2fd1498b6b0f6ed9729807e1', 111471,
      '8f8fe37e87d927bbec42ca0caa6b5bdb6c4f9198e38b7be4220f635041f279ae', 11974,
      '{}'::jsonb
    ),
    (
      'category-distillery', 'distillery',
      '05d56aadb348f708c068de87632bc4447261766719de3e70987a81875e6a5ee7',
      '5be6bb1db793d76d8b081c2d3cc6502d095a3bdaa9cfedcba5d68a0987010b34',
      'family-spirits',
      '5be6bb1db793d76d8b081c2d3cc6502d095a3bdaa9cfedcba5d68a0987010b34', 378188,
      '03aaadf33a002c6c28668f5c5d7ce76d97cf81106360e9fa610a8a4652911656', 130453,
      '06a0c1eaa930121706f26896a693621fe54b19bccffed81f6fac32b48954ab89', 12271,
      '{}'::jsonb
    ),
    (
      'category-dive-bar', 'dive_bar',
      '199169ec54cb9447c3639ede9d2f0be3316bc51376bc2413938cc094858ce754',
      '80f1d11e8cb1b3bd74646f44f7329a8ed5a625c8e30a17555e239a6f97e79fa0',
      'family-other',
      '80f1d11e8cb1b3bd74646f44f7329a8ed5a625c8e30a17555e239a6f97e79fa0', 334508,
      '6d93399eb3a4bc3c5f7844a721ed82295a1d38e8a03bf4f660967bbb8cdba781', 113334,
      '8a74c45c0c63f726b6b7986bd635fcaaa9d4b881f97c63cc1b099cac6d88e904', 11294,
      '{}'::jsonb
    ),
    (
      'category-lounge', 'lounge',
      '6aaba564a991a6114992bbbdd90ba7600337d5ba770b64cfc9fb703a952b45d2',
      '20d08c06807fd0ad3990ccaab70adbc3481f1478e89a2dedeb1d368add3a8577',
      'family-spirits',
      '20d08c06807fd0ad3990ccaab70adbc3481f1478e89a2dedeb1d368add3a8577', 282236,
      '19cec26f8e07a84768708b6f49f89a79742ca14893ae6020614b60403266276f', 103940,
      '4a54b85af03aa0672fae8ec5af9aae297c3ec7e36b7b62255ad9fd1cbf335ea1', 12267,
      '{}'::jsonb
    ),
    (
      'category-nightclub', 'nightclub',
      'f5c71d8c5fb687ec9024b15a8d65504d68efaee04ad96dadd3cd9e9ef67637a5',
      '0f80d918a2cd68037bba09e221fe6405561fe4ab0a6a004e659015164cf44a09',
      'family-spirits',
      '0f80d918a2cd68037bba09e221fe6405561fe4ab0a6a004e659015164cf44a09', 294655,
      '6c9cf56511cf34e26368fff4f779646bc32c8e495aab3878afbeb1e427c60877', 104774,
      'be7868a55a7d2b292c7b2c4536e11cfe1fc00d6c419865b385eaea82a862b95f', 11483,
      '{}'::jsonb
    ),
    (
      'category-pub', 'pub',
      '53cfa04085a918bc737844d2fe2ef187bf37dc54bb3094e51603f7fa1a922f5b',
      '948a832b5cec9238138e0eae8697381661c920dc40f22d5599bd38c96b5376b8',
      'family-other',
      '948a832b5cec9238138e0eae8697381661c920dc40f22d5599bd38c96b5376b8', 439536,
      '4b9fb24a033dec0cca1fb4251fd9a6d56f936d845301a6c219e7c58d2ef4547c', 158421,
      'fe60a1622fb2ae1d55fe2e4bc14151b6158935430b23e14cb173fce8bc0a4de4', 14799,
      '{}'::jsonb
    ),
    (
      'category-sports-bar', 'sports_bar',
      'e5bc67aa6ceaee5ddf28732341e10e74e2a655d0c5ed05f69bb6c9962e3c98e5',
      'ce470d1c43a4d10b6f04c8419aedd57c1d2f51478dff8b75a8af6df77bc41b67',
      'family-beer',
      'ce470d1c43a4d10b6f04c8419aedd57c1d2f51478dff8b75a8af6df77bc41b67', 447496,
      '177c017ab6dd39c455aff23881426bd8c29c6ed73b7f81b923533ccdbf144b35', 167824,
      '2f8605d5bb2e7f1d4ebc6ff4cea3516d20593080021659bd85c98a1be77c7633', 17235,
      '{"edit_prompt_sha256":"fcf3f6500157729b3ced47aa6e632dd07682f4f86a437033b5a06bcec807d122"}'::jsonb
    ),
    (
      'category-taproom', 'taproom',
      '795cffd046383faecdbfe30c43b21313d56fac6fdcea93ba27e4b64f87c17f3d',
      '324bba52bf94f26ad62c20dfcb051db2d53f5df87057deeed8ae31c815c8bdee',
      'family-beer',
      '324bba52bf94f26ad62c20dfcb051db2d53f5df87057deeed8ae31c815c8bdee', 440705,
      '3e92ac042046ef2dcb2a2ffa30dfff7fe08502f4f17f316d4ae8c76bdfc7012f', 169141,
      '314d865b7bada32d2defeb55726697a3a5d6f8fe0f16d05c5d511a38be06b59b', 18592,
      '{}'::jsonb
    ),
    (
      'category-tasting-room', 'tasting_room',
      '53827f596acb80f9aa78f1ef211d42dca649cd77c013adcdc22eaada01b8e15e',
      'e61caf422289d455895fc6774cb8460769eec730c63665bd89a8a6b142d05dbd',
      'family-wine',
      'e61caf422289d455895fc6774cb8460769eec730c63665bd89a8a6b142d05dbd', 446732,
      'ad965cb4fbb470ed712567927528fee53672b9c2ec08cc0181f9fa82b16ba8d8', 168952,
      '45f9530df2aa25b24e4a823419f685aeb112bdd42d53d3d6aa57a0fa3d8e6b2e', 18282,
      '{}'::jsonb
    ),
    (
      'category-wine-bar', 'wine_bar',
      'febc9c094e1e2a1258b4f0388459ed2131f972e16c05793ab863ffd57188d1b6',
      'dea819795e119b7b1df5916ba871fdfeab5832811fe3061668b4d81c0fe69367',
      'family-wine',
      'dea819795e119b7b1df5916ba871fdfeab5832811fe3061668b4d81c0fe69367', 288459,
      'd9a18cc61fb11a65cfe1bad85bd795a95518f9976bae4c2948de518c7e42492f', 100520,
      '0ae5019ac999f1c6bbc217443aac5899641db159df03cb63298be6373635c496', 10795,
      '{}'::jsonb
    ),
    (
      'category-winery', 'winery',
      '87cb3ad8c498ab8bb2eda9d09a51d2ceef3567d4bc375477af7cbdc7d2f69fe0',
      'd756589a709e66760380b26216da275fd39cae2ed2afe2ed246e5eb3c1daf8ea',
      'family-wine',
      'd756589a709e66760380b26216da275fd39cae2ed2afe2ed246e5eb3c1daf8ea', 446249,
      '92ccfe7ea6f51b8822a8bf733866c93cbdcf0ffde2b1bc5a0f2a1306d07464eb', 170832,
      'fbc52cd7c500617252bfa7252fd0f03b4e6df49ef4959d6190b1042636afbcf2', 18368,
      '{}'::jsonb
    )
), inserted_templates as (
  insert into catalog.establishment_media_templates (
    template_key, generator, generator_version, prompt_sha256,
    source_master_sha256, disclosure_text, output_license_id,
    quality_reviewed_at, quality_reviewed_by, quality_review_notes, metadata
  )
  select template_key,
         'openai-imagegen',
         'codex-builtin',
         prompt_sha256,
         source_master_sha256,
         'Paloma category concept artwork; not a photograph or depiction of this establishment',
         'Paloma-Proprietary',
         now(),
         'codex-media-qa',
         'Reviewed as a 16-image contact sheet and individually for clean 8:5 crops, thumbnail readability, category-specific cues, visual differentiation, and absence of business names and trademarks.',
         jsonb_build_object(
           'release', 'category-storefronts-v2',
           'category', category_slug,
           'style_reference', style_reference,
           'not_venue_depiction', true
         ) || extra_metadata
  from release
  returning template_key
), variant_rows as (
  select release.template_key,
         variant.variant,
         variant.sha256,
         variant.byte_size,
         variant.width,
         variant.height
  from release
  join inserted_templates using (template_key)
  cross join lateral (
    values
      ('hero', release.hero_sha256, release.hero_bytes, 1600, 1000),
      ('card', release.card_sha256, release.card_bytes, 960, 600),
      ('thumbnail', release.thumbnail_sha256, release.thumbnail_bytes, 320, 200)
  ) as variant(variant, sha256, byte_size, width, height)
)
insert into catalog.establishment_media_template_variants (
  template_key, variant, object_path, public_url, mime_type,
  width, height, byte_size, sha256
)
select template_key,
       variant,
       'templates/v2/' || template_key || '/' || variant || '-' || left(sha256, 16) || '.jpg',
       'https://lighcnfzajgvfbdoekzt.supabase.co/storage/v1/object/public/paloma-establishment-media/templates/v2/'
         || template_key || '/' || variant || '-' || left(sha256, 16) || '.jpg',
       'image/jpeg',
       width,
       height,
       byte_size,
       sha256
from variant_rows;

create or replace function catalog.default_establishment_media_template(
  p_primary_type_id smallint
)
returns text
language sql
stable
security invoker
set search_path = ''
as $$
  select coalesce(
    (
      select template.template_key
      from catalog.establishment_media_templates template
      where template.template_key =
        'category-' || replace(primary_type.slug, '_', '-')
    ),
    case replace(primary_type.slug, '_', '-')
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
  )
  from public.primary_types primary_type
  where primary_type.id = p_primary_type_id
$$;

with desired as (
  select establishment.id,
         catalog.default_establishment_media_template(establishment.primary_type_id)
           as template_key
  from public.establishments establishment
  where establishment.publication_state = 'published'
    and establishment.cover_media_asset_id is null
    and establishment.cover_media_template_key is not null
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
where establishment.id = desired.id
  and establishment.cover_media_template_key is distinct from desired.template_key;

comment on function catalog.default_establishment_media_template(smallint) is
  'Prefers a reviewed category cover and falls back to a broad family for unknown future types.';

notify pgrst, 'reload schema';
