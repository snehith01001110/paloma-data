-- Human identity decisions are privileged catalog evidence and must remain attributable.

create table ingest.candidate_match_review_resolutions (
  id bigint generated always as identity primary key,
  review_id bigint not null unique
    references ingest.candidate_match_reviews(id) on delete restrict,
  resolution text not null,
  resolved_by text not null,
  note text,
  evidence jsonb not null,
  created_at timestamptz not null default now(),
  check (resolution in ('same_place', 'not_same_or_stale')),
  check (char_length(btrim(resolved_by)) between 3 and 200),
  check (note is null or char_length(note) between 1 and 2000),
  check (jsonb_typeof(evidence) = 'object'),
  check (octet_length(evidence::text) <= 65536)
);

create index candidate_match_review_resolutions_created_idx
  on ingest.candidate_match_review_resolutions (created_at desc);

-- Preserve the pre-migration decisions without inventing reviewer attribution. Their evidence and
-- original resolution timestamp remain useful, while resolved_by makes the provenance gap explicit.
insert into ingest.candidate_match_review_resolutions (
  review_id, resolution, resolved_by, note, evidence, created_at
)
select
  id,
  case state
    when 'accepted' then 'same_place'
    when 'rejected' then 'not_same_or_stale'
  end,
  'legacy:pre-audit-migration',
  'Backfilled resolved review; the original reviewer was not recorded.',
  jsonb_build_object(
    'candidate_id', candidate_id,
    'source', source,
    'source_record_id', source_record_id,
    'reason', reason,
    'score', score,
    'review_evidence', evidence
  ),
  coalesce(resolved_at, created_at)
from ingest.candidate_match_reviews
where state in ('accepted', 'rejected');

create trigger candidate_match_review_resolutions_append_only
before update or delete on ingest.candidate_match_review_resolutions
for each row execute function governance.reject_append_only_mutation();

create or replace function ingest.enforce_candidate_match_review_transition()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  expected_resolution text;
begin
  if old.state <> 'pending' then
    raise exception 'resolved candidate match reviews are immutable';
  end if;
  if new.state = 'pending' then
    if new.resolved_at is not null then
      raise exception 'pending candidate match reviews cannot have resolved_at';
    end if;
    return new;
  end if;
  if new.resolved_at is null then
    raise exception 'resolved candidate match reviews require resolved_at';
  end if;
  if new.state in ('accepted', 'rejected') then
    expected_resolution := case new.state
      when 'accepted' then 'same_place'
      when 'rejected' then 'not_same_or_stale'
    end;
    if not exists (
      select 1
      from ingest.candidate_match_review_resolutions resolution
      where resolution.review_id = old.id
        and resolution.resolution = expected_resolution
    ) then
      raise exception 'manual candidate match review resolution lacks an audit event';
    end if;
  end if;
  return new;
end;
$$;

create trigger enforce_candidate_match_review_transition
before update on ingest.candidate_match_reviews
for each row execute function ingest.enforce_candidate_match_review_transition();

alter table ingest.candidate_match_review_resolutions enable row level security;

create policy paloma_ingest_insert_candidate_match_review_resolutions
  on ingest.candidate_match_review_resolutions
  for insert to paloma_ingest with check (true);

create policy paloma_ingest_read_candidate_match_review_resolutions
  on ingest.candidate_match_review_resolutions
  for select to paloma_ingest using (true);

revoke all on ingest.candidate_match_review_resolutions from public, anon, authenticated;
revoke execute on function ingest.enforce_candidate_match_review_transition()
  from public, anon, authenticated;
grant select, insert on ingest.candidate_match_review_resolutions to paloma_ingest;
grant usage, select on sequence ingest.candidate_match_review_resolutions_id_seq
  to paloma_ingest;
grant execute on function ingest.enforce_candidate_match_review_transition()
  to paloma_ingest;

comment on table ingest.candidate_match_review_resolutions is
  'Immutable reviewer-attributed decisions over candidate match-review evidence snapshots.';
