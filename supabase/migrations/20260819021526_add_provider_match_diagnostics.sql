-- Persist only a bounded, Paloma-owned error classification. Provider response
-- bodies, request URLs, credentials, and establishment/provider identifiers do
-- not belong in operational diagnostics.
alter table ingest.provider_match_state
  add column last_error_code text;

alter table ingest.provider_match_state
  add constraint provider_match_state_error_code_check check (
    last_error_code is null
    or last_error_code ~ '^[a-z][a-z0-9_]{0,63}$'
  );

update ingest.provider_match_state
set last_error_code = 'unclassified'
where outcome = 'error'
  and last_error_code is null;

comment on column ingest.provider_match_state.last_error_code is
  'Safe Paloma-owned failure classification only; never provider content, request data, IDs, or credentials.';
