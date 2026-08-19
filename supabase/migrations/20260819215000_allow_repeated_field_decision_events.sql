-- A content fingerprint identifies a decision value, not an event. Append-only history must be
-- able to record a return to a previously seen value after intervening evidence changes.

alter table catalog.field_decisions
  drop constraint if exists field_decisions_decision_fingerprint_key;

create index if not exists field_decisions_fingerprint_idx
  on catalog.field_decisions (decision_fingerprint);
