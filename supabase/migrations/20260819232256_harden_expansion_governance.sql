-- Close advisor findings in the data-governance schema and remove the completed one-shot cutover.

alter table governance.contribution_terms_versions enable row level security;

create index catalog_expansion_release_events_terms_idx
  on governance.catalog_expansion_release_events (terms_version)
  where terms_version is not null;
create index contribution_reviews_observation_idx
  on catalog.contribution_reviews (observation_id)
  where observation_id is not null;
create index field_observations_source_run_idx
  on catalog.field_observations (source_run_id)
  where source_run_id is not null;
create index hours_schedules_establishment_idx
  on catalog.hours_schedules (establishment_id);
create index field_conflicts_decision_idx
  on review.field_conflicts (decision_id)
  where decision_id is not null;
create index establishment_contributions_terms_idx
  on public.establishment_contributions (terms_version);
create index merchant_claim_requests_terms_idx
  on public.merchant_claim_requests (terms_version);

drop function if exists ingest.reset_legacy_public_catalog(text, integer);
drop table if exists ingest.catalog_cutover_control;
