-- Let the scoped catalog worker resolve durable provider IDs before user traffic.
-- It may store only Paloma-owned decision metadata and the provider ID; licensed
-- response attributes remain inaccessible to this role and stay in the bounded
-- runtime cache managed by paloma_runtime.

alter table ingest.provider_match_state
  add column decision_reason text;

alter table ingest.provider_match_state
  add constraint provider_match_state_decision_reason_check check (
    decision_reason is null
    or decision_reason ~ '^[a-z][a-z0-9_]{0,63}$'
  );

grant select, insert, update on ingest.provider_match_state to paloma_ingest;

create policy paloma_ingest_manage_provider_match_state
  on ingest.provider_match_state for all to paloma_ingest
  using (true) with check (true);

comment on column ingest.provider_match_state.decision_reason is
  'Safe Paloma-owned match decision only; never provider content, request data, IDs, or credentials.';

comment on policy paloma_ingest_manage_provider_match_state
  on ingest.provider_match_state is
  'Allows the scoped scheduled catalog worker to share cooldown and single-flight state with the runtime matcher.';
