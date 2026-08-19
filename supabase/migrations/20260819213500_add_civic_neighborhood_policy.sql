-- Admit the reviewed SF Find boundary dataset as durable, derived neighborhood evidence.
-- The dataset page identifies gfpk-269f as Public Domain U.S. Government data; labels are
-- general location names, not legal or administrative boundaries.

insert into governance.source_policy_versions (
  source, policy_version, source_owner, acquisition_method, terms_url,
  license_ids, storage_class, raw_persistence_allowed,
  normalized_persistence_allowed, canonical_derivation_allowed, display_allowed,
  max_retention, attribution_text, effective_from, reviewed_at, reviewed_by, notes
) values (
  'datasf_neighborhoods', 'sf-find-2026-v1',
  'City and County of San Francisco Planning Department',
  'open_data_export',
  'https://data.sfgov.org/Geographic-Locations-and-Boundaries/SF-Find-Neighborhoods/gfpk-269f',
  array['Public-Domain-US-Government'], 'durable_open', true, true, true, true, null,
  'DataSF SF Find Neighborhoods', '2026-08-01', now(),
  'paloma-data-policy-review',
  'Point-in-polygon display labels only; the source states these are general neighborhood locations, not hard boundaries.'
)
on conflict (source, policy_version) do nothing;

insert into governance.source_field_policies (
  source_policy_id, field_name, durable_storage_allowed,
  canonical_derivation_allowed, display_allowed, authority,
  allowed_license_ids, recommended_max_age
)
select id, 'neighborhood', true, true, true, 0.94,
       array['Public-Domain-US-Government'], interval '365 days'
from governance.source_policy_versions
where source = 'datasf_neighborhoods' and policy_version = 'sf-find-2026-v1'
on conflict (source_policy_id, field_name) do nothing;
