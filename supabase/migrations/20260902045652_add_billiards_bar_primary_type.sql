-- Billiards bars are a consumer-experience subtype of bars, not a new legal license class.
-- Keep the row additive so existing establishments and future materialization can resolve it.
insert into public.primary_types (slug, name)
values ('billiards_bar', 'Billiards Bar')
on conflict (slug) do update
set name = excluded.name;
