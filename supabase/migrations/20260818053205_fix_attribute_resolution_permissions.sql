-- Compatibility with the original evidence constraint name and least-privilege
-- permissions required by snapshot-based machine attribute replacement.
alter table ingest.establishment_field_evidence
  drop constraint if exists establishment_field_evidence_field_check,
  drop constraint if exists establishment_field_evidence_field_name_check;

alter table ingest.establishment_field_evidence
  add constraint establishment_field_evidence_field_check check (
    field_name = any (array[
      'display_name', 'legal_name', 'address', 'phone_e164', 'website_url',
      'primary_type_slug', 'status', 'latitude', 'longitude', 'neighborhood',
      'hours', 'price_level', 'setting_slug'
    ]::text[])
  );

grant delete on ingest.establishment_field_evidence to paloma_ingest;
grant select on public.settings to paloma_ingest;
grant select, insert, update, delete on public.establishment_settings to paloma_ingest;
