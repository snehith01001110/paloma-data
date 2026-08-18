-- The worker has table-level grants, but these RLS policies are also required
-- to resolve objective setting claims through the public settings vocabulary.
drop policy if exists paloma_ingest_read_settings on public.settings;
create policy paloma_ingest_read_settings
  on public.settings
  for select
  to paloma_ingest
  using (true);

drop policy if exists paloma_ingest_manage_establishment_settings
  on public.establishment_settings;
create policy paloma_ingest_manage_establishment_settings
  on public.establishment_settings
  for all
  to paloma_ingest
  using (true)
  with check (true);
