-- Cover the composite provider-link foreign keys used for cascading cleanup.
create index provider_response_cache_provider_link_idx
  on ingest.provider_response_cache (provider_link_id, provider);

create index provider_refresh_leases_provider_link_idx
  on ingest.provider_refresh_leases (provider_link_id, provider);
