-- Development expansion may proceed after one healthy refresh week. The checked-in manifest
-- independently refuses a production phase configured below two weeks.

alter table governance.catalog_expansion_release_events
  drop constraint catalog_expansion_release_events_check1;

alter table governance.catalog_expansion_release_events
  add constraint catalog_expansion_release_events_approval_policy_check
  check (
    event_type = 'revoked'
    or (
      cardinality(scope_cities) > 0
      and maximum_new_publications between 1 and 500
      and baseline_publications >= 0
      and decision_version ~ '^v[0-9]+$'
      and minimum_healthy_refresh_weeks between 1 and 8
      and refresh_history_days between 14 and 90
      and maximum_latest_refresh_age_hours between 12 and 168
      and failed_run_lookback_days between 7 and 90
      and terms_version is not null
      and coverage_snapshot <> '{}'::jsonb
      and coverage_accepted_by is not null
      and coverage_accepted_at is not null
      and expires_at is not null
    )
  );
