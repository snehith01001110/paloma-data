-- Bound paid, on-demand provider lookups without retaining provider attributes.
-- These rows contain only aggregate request counters; no Foursquare response data,
-- establishment IDs, or request history is stored.

create table if not exists ingest.live_detail_user_limits (
  user_id uuid primary key references auth.users(id) on delete cascade,
  minute_started_at timestamptz not null default date_trunc('minute', now()),
  minute_count integer not null default 0 check (minute_count >= 0),
  day_started_at date not null default (now() at time zone 'utc')::date,
  day_count integer not null default 0 check (day_count >= 0),
  updated_at timestamptz not null default now()
);

create table if not exists ingest.live_detail_global_limit (
  singleton boolean primary key default true check (singleton),
  second_started_at timestamptz not null default date_trunc('second', now()),
  request_count integer not null default 0 check (request_count >= 0),
  updated_at timestamptz not null default now()
);

alter table ingest.live_detail_user_limits enable row level security;
alter table ingest.live_detail_global_limit enable row level security;

revoke all on ingest.live_detail_user_limits from public, anon, authenticated;
revoke all on ingest.live_detail_global_limit from public, anon, authenticated;

comment on table ingest.live_detail_user_limits is
  'Aggregate per-user counters for paid transient venue-detail lookups; stores no provider data.';
comment on table ingest.live_detail_global_limit is
  'Global per-second counter protecting the paid venue-detail provider.';
