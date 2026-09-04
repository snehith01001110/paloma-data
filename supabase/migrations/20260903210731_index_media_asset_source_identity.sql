-- Cover the composite source/establishment foreign key used to prove that an
-- artwork source belongs to the same establishment as its rendered asset.
create index establishment_media_assets_source_establishment_idx
  on catalog.establishment_media_assets (source_id, establishment_id);
