-- The ingest hardening migration enabled RLS on source_records after the live
-- details role had already been granted read access.  Without a matching
-- policy, RLS hid every linked Foursquare row and made all published venues
-- fail the Edge Function's eligibility query.
--
-- Keep the repair deliberately narrow: the runtime may read only the columns
-- and Foursquare rows required to establish that a linked place is current,
-- consumer-facing, walk-in, and free of hard-negative quality flags.  Provider
-- payload and enrichment columns remain inaccessible to the runtime role.

revoke select on ingest.source_records from paloma_runtime;

grant select (
  source,
  source_record_id,
  retired_at,
  source_status,
  consumer_facing,
  public_access,
  quality_flags
) on ingest.source_records to paloma_runtime;

drop policy if exists paloma_runtime_read_eligible_fsq_source_records
  on ingest.source_records;
create policy paloma_runtime_read_eligible_fsq_source_records
  on ingest.source_records for select to paloma_runtime
  using (
    source = 'fsq'
    and retired_at is null
    and source_status = 'open'
    and consumer_facing
    and public_access = 'walk_in'
    and not (quality_flags && array[
      'closed',
      'delete',
      'doesnt_exist',
      'does_not_exist',
      'duplicate',
      'inappropriate',
      'privatevenue',
      'private_venue'
    ]::text[])
  );

comment on policy paloma_runtime_read_eligible_fsq_source_records
  on ingest.source_records is
  'Allows venue-live-details to validate only current eligible Foursquare links; rich provider fields remain column-inaccessible.';
