-- Durable background-job control plane for catalog maintenance.
--
-- pgmq owns message visibility and delivery. The ingest tables own durable job state,
-- attempts, deduplication, and logical runs. Workers can only reach the fixed queue through
-- privilege-contained functions; the queue is intentionally not exposed through PostgREST.

create extension if not exists pgmq;

select pgmq.create('paloma_pipeline');

alter table pgmq.q_paloma_pipeline enable row level security;
alter table pgmq.a_paloma_pipeline enable row level security;

revoke usage on schema pgmq
  from public, anon, authenticated, service_role, paloma_ingest;
revoke all on pgmq.q_paloma_pipeline, pgmq.a_paloma_pipeline
  from public, anon, authenticated, service_role, paloma_ingest;
revoke all on sequence pgmq.q_paloma_pipeline_msg_id_seq
  from public, anon, authenticated, service_role, paloma_ingest;

create table ingest.pipeline_runs (
  id uuid primary key default gen_random_uuid(),
  run_type text not null,
  requested_by text not null,
  state text not null default 'queued',
  metadata jsonb not null default '{}'::jsonb,
  job_count integer not null default 0,
  succeeded_count integer not null default 0,
  dead_count integer not null default 0,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  updated_at timestamptz not null default now(),
  check (run_type ~ '^[a-z][a-z0-9_]{0,63}$'),
  check (length(requested_by) between 1 and 200),
  check (jsonb_typeof(metadata) = 'object'),
  check (pg_column_size(metadata) <= 65536),
  check (state in ('queued', 'running', 'succeeded', 'partial', 'failed')),
  check (job_count >= 0),
  check (succeeded_count between 0 and job_count),
  check (dead_count between 0 and job_count),
  check (succeeded_count + dead_count <= job_count)
);

create table ingest.pipeline_jobs (
  id uuid primary key default gen_random_uuid(),
  job_type text not null,
  dedupe_key text not null,
  payload jsonb not null default '{}'::jsonb,
  state text not null default 'queued',
  attempt_count integer not null default 0,
  max_attempts integer not null default 5,
  last_message_id bigint,
  next_attempt_at timestamptz not null default now(),
  locked_by text,
  locked_at timestamptz,
  lease_expires_at timestamptz,
  result jsonb,
  error_code text,
  error_summary text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  updated_at timestamptz not null default now(),
  check (job_type ~ '^[a-z][a-z0-9_]{0,63}$'),
  check (length(dedupe_key) between 1 and 500),
  check (jsonb_typeof(payload) = 'object'),
  check (pg_column_size(payload) <= 65536),
  check (result is null or jsonb_typeof(result) = 'object'),
  check (result is null or pg_column_size(result) <= 65536),
  check (state in ('queued', 'running', 'succeeded', 'dead')),
  check (max_attempts between 1 and 25),
  check (attempt_count between 0 and max_attempts),
  check (locked_by is null or length(locked_by) between 1 and 200),
  check (error_code is null or length(error_code) <= 100),
  check (error_summary is null or length(error_summary) <= 2000),
  check (
    (state = 'running' and locked_by is not null and locked_at is not null
      and lease_expires_at is not null)
    or state <> 'running'
  )
);

create unique index pipeline_jobs_active_dedupe_idx
  on ingest.pipeline_jobs (job_type, dedupe_key)
  where state in ('queued', 'running');

create index pipeline_jobs_claim_idx
  on ingest.pipeline_jobs (next_attempt_at, created_at)
  where state = 'queued';

create index pipeline_jobs_running_lease_idx
  on ingest.pipeline_jobs (lease_expires_at)
  where state = 'running';

create index pipeline_jobs_terminal_idx
  on ingest.pipeline_jobs (finished_at)
  where state in ('succeeded', 'dead');

create table ingest.pipeline_run_jobs (
  run_id uuid not null references ingest.pipeline_runs(id) on delete cascade,
  job_id uuid not null references ingest.pipeline_jobs(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (run_id, job_id)
);

create index pipeline_run_jobs_job_idx
  on ingest.pipeline_run_jobs (job_id, run_id);

create table ingest.pipeline_job_attempts (
  id bigint generated always as identity primary key,
  job_id uuid not null references ingest.pipeline_jobs(id) on delete cascade,
  attempt_no integer not null,
  message_id bigint not null,
  worker_id text not null,
  state text not null default 'running',
  retryable boolean,
  result jsonb,
  error_code text,
  error_summary text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  constraint pipeline_job_attempt_unique unique (job_id, attempt_no),
  check (attempt_no between 1 and 25),
  check (length(worker_id) between 1 and 200),
  check (state in ('running', 'succeeded', 'failed')),
  check (result is null or jsonb_typeof(result) = 'object'),
  check (result is null or pg_column_size(result) <= 65536),
  check (error_code is null or length(error_code) <= 100),
  check (error_summary is null or length(error_summary) <= 2000)
);

create index pipeline_job_attempts_started_idx
  on ingest.pipeline_job_attempts (started_at desc);

alter table ingest.pipeline_runs enable row level security;
alter table ingest.pipeline_jobs enable row level security;
alter table ingest.pipeline_run_jobs enable row level security;
alter table ingest.pipeline_job_attempts enable row level security;

revoke all on ingest.pipeline_runs, ingest.pipeline_jobs,
  ingest.pipeline_run_jobs, ingest.pipeline_job_attempts
  from public, anon, authenticated, service_role;
revoke all on sequence ingest.pipeline_job_attempts_id_seq
  from public, anon, authenticated, service_role;

drop policy if exists paloma_ingest_read_pipeline_runs on ingest.pipeline_runs;
create policy paloma_ingest_read_pipeline_runs
  on ingest.pipeline_runs for select to paloma_ingest using (true);

drop policy if exists paloma_ingest_read_pipeline_jobs on ingest.pipeline_jobs;
create policy paloma_ingest_read_pipeline_jobs
  on ingest.pipeline_jobs for select to paloma_ingest using (true);

drop policy if exists paloma_ingest_read_pipeline_run_jobs on ingest.pipeline_run_jobs;
create policy paloma_ingest_read_pipeline_run_jobs
  on ingest.pipeline_run_jobs for select to paloma_ingest using (true);

drop policy if exists paloma_ingest_read_pipeline_job_attempts
  on ingest.pipeline_job_attempts;
create policy paloma_ingest_read_pipeline_job_attempts
  on ingest.pipeline_job_attempts for select to paloma_ingest using (true);

grant select on ingest.pipeline_runs, ingest.pipeline_jobs,
  ingest.pipeline_run_jobs, ingest.pipeline_job_attempts to paloma_ingest;

create or replace function ingest.refresh_pipeline_runs_for_job(p_job_id uuid)
returns void
language sql
security definer
set search_path = pg_catalog
as $$
  with affected as (
    select run_id
    from ingest.pipeline_run_jobs
    where job_id = p_job_id
  ), aggregate_state as (
    select
      run_job.run_id,
      count(*)::integer as job_count,
      count(*) filter (where job.state = 'succeeded')::integer as succeeded_count,
      count(*) filter (where job.state = 'dead')::integer as dead_count,
      count(*) filter (where job.state = 'running')::integer as running_count,
      min(job.started_at) as first_started_at,
      max(job.finished_at) as last_finished_at
    from ingest.pipeline_run_jobs run_job
    join ingest.pipeline_jobs job on job.id = run_job.job_id
    where run_job.run_id in (select run_id from affected)
    group by run_job.run_id
  )
  update ingest.pipeline_runs run
  set job_count = aggregate_state.job_count,
      succeeded_count = aggregate_state.succeeded_count,
      dead_count = aggregate_state.dead_count,
      state = case
        when aggregate_state.succeeded_count + aggregate_state.dead_count
               = aggregate_state.job_count
          then case
            when aggregate_state.dead_count = 0 then 'succeeded'
            when aggregate_state.succeeded_count = 0 then 'failed'
            else 'partial'
          end
        when aggregate_state.running_count > 0 then 'running'
        else 'queued'
      end,
      started_at = coalesce(run.started_at, aggregate_state.first_started_at),
      finished_at = case
        when aggregate_state.succeeded_count + aggregate_state.dead_count
               = aggregate_state.job_count
          then aggregate_state.last_finished_at
        else null
      end,
      updated_at = now()
  from aggregate_state
  where run.id = aggregate_state.run_id;
$$;

create or replace function ingest.create_pipeline_run(
  p_run_type text,
  p_requested_by text,
  p_metadata jsonb default '{}'::jsonb
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_run_id uuid;
begin
  if p_run_type is null or p_run_type !~ '^[a-z][a-z0-9_]{0,63}$' then
    raise exception 'invalid pipeline run type';
  end if;
  if p_requested_by is null or length(p_requested_by) not between 1 and 200 then
    raise exception 'invalid pipeline requester';
  end if;
  if p_metadata is null or jsonb_typeof(p_metadata) <> 'object'
     or pg_column_size(p_metadata) > 65536 then
    raise exception 'pipeline run metadata must be an object no larger than 64 KiB';
  end if;

  insert into ingest.pipeline_runs (run_type, requested_by, metadata)
  values (p_run_type, p_requested_by, p_metadata)
  returning id into v_run_id;
  return v_run_id;
end;
$$;

create or replace function ingest.enqueue_pipeline_job(
  p_run_id uuid,
  p_job_type text,
  p_dedupe_key text,
  p_payload jsonb,
  p_max_attempts integer default 5,
  p_delay_seconds integer default 0
)
returns table (job_id uuid, created boolean, job_state text)
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_job_id uuid;
  v_created boolean := false;
  v_state text;
  v_message_id bigint;
begin
  if not exists (select 1 from ingest.pipeline_runs where id = p_run_id) then
    raise exception 'pipeline run does not exist';
  end if;
  if p_job_type is null or p_job_type !~ '^[a-z][a-z0-9_]{0,63}$' then
    raise exception 'invalid pipeline job type';
  end if;
  if p_dedupe_key is null or length(p_dedupe_key) not between 1 and 500 then
    raise exception 'invalid pipeline dedupe key';
  end if;
  if p_payload is null or jsonb_typeof(p_payload) <> 'object'
     or pg_column_size(p_payload) > 65536 then
    raise exception 'pipeline payload must be an object no larger than 64 KiB';
  end if;
  if p_max_attempts not between 1 and 25 then
    raise exception 'max attempts must be between 1 and 25';
  end if;
  if p_delay_seconds not between 0 and 604800 then
    raise exception 'delay must be between 0 and 604800 seconds';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_job_type || chr(31) || p_dedupe_key, 0)
  );

  select id, state
  into v_job_id, v_state
  from ingest.pipeline_jobs
  where job_type = p_job_type
    and dedupe_key = p_dedupe_key
    and state in ('queued', 'running')
  for update;

  if v_job_id is null then
    insert into ingest.pipeline_jobs (
      job_type, dedupe_key, payload, max_attempts, next_attempt_at
    ) values (
      p_job_type,
      p_dedupe_key,
      p_payload,
      p_max_attempts,
      clock_timestamp() + make_interval(secs => p_delay_seconds)
    )
    returning id, state into v_job_id, v_state;

    select message_id
    into v_message_id
    from pgmq.send(
      'paloma_pipeline',
      jsonb_build_object('job_id', v_job_id),
      (clock_timestamp() + make_interval(secs => p_delay_seconds))::timestamptz
    ) as message_id;

    update ingest.pipeline_jobs
    set last_message_id = v_message_id, updated_at = now()
    where id = v_job_id;
    v_created := true;
  end if;

  insert into ingest.pipeline_run_jobs (run_id, job_id)
  values (p_run_id, v_job_id)
  on conflict do nothing;

  perform ingest.refresh_pipeline_runs_for_job(v_job_id);

  job_id := v_job_id;
  created := v_created;
  job_state := v_state;
  return next;
end;
$$;

create or replace function ingest.claim_pipeline_jobs(
  p_worker_id text,
  p_visibility_seconds integer default 900,
  p_quantity integer default 1
)
returns table (
  job_id uuid,
  message_id bigint,
  job_type text,
  payload jsonb,
  attempt_no integer,
  max_attempts integer,
  run_ids uuid[]
)
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_message record;
  v_job record;
  v_job_id uuid;
  v_attempt_no integer;
  v_wait_seconds integer;
begin
  if p_worker_id is null or length(p_worker_id) not between 1 and 200 then
    raise exception 'invalid pipeline worker id';
  end if;
  if p_visibility_seconds not between 30 and 7200 then
    raise exception 'visibility timeout must be between 30 and 7200 seconds';
  end if;
  if p_quantity not between 1 and 100 then
    raise exception 'claim quantity must be between 1 and 100';
  end if;

  for v_message in
    select * from pgmq.read('paloma_pipeline', p_visibility_seconds, p_quantity)
  loop
    begin
      v_job_id := nullif(v_message.message->>'job_id', '')::uuid;
    exception when invalid_text_representation then
      perform pgmq.archive('paloma_pipeline', v_message.msg_id);
      continue;
    end;

    if v_job_id is null then
      perform pgmq.archive('paloma_pipeline', v_message.msg_id);
      continue;
    end if;

    select * into v_job
    from ingest.pipeline_jobs
    where id = v_job_id
    for update;

    if not found
       or v_job.state in ('succeeded', 'dead')
       or v_job.last_message_id is distinct from v_message.msg_id then
      perform pgmq.archive('paloma_pipeline', v_message.msg_id);
      continue;
    end if;

    if v_job.next_attempt_at > clock_timestamp() then
      v_wait_seconds := greatest(
        1,
        ceil(extract(epoch from (v_job.next_attempt_at - clock_timestamp())))::integer
      );
      perform 1 from pgmq.set_vt('paloma_pipeline', v_message.msg_id, v_wait_seconds);
      continue;
    end if;

    if v_job.state = 'running' and v_job.lease_expires_at > clock_timestamp() then
      v_wait_seconds := greatest(
        1,
        ceil(extract(epoch from (v_job.lease_expires_at - clock_timestamp())))::integer
      );
      perform 1 from pgmq.set_vt('paloma_pipeline', v_message.msg_id, v_wait_seconds);
      continue;
    end if;

    if v_job.state = 'running' then
      update ingest.pipeline_job_attempts
      set state = 'failed',
          retryable = v_job.attempt_count < v_job.max_attempts,
          error_code = 'lease_expired',
          error_summary = 'worker lease expired before the attempt was acknowledged',
          finished_at = now()
      where job_id = v_job_id
        and attempt_no = v_job.attempt_count
        and state = 'running';
    end if;

    if v_job.attempt_count >= v_job.max_attempts then
      update ingest.pipeline_jobs
      set state = 'dead',
          locked_by = null,
          locked_at = null,
          lease_expires_at = null,
          finished_at = now(),
          error_code = coalesce(error_code, 'attempts_exhausted'),
          error_summary = coalesce(error_summary, 'maximum attempts exhausted'),
          updated_at = now()
      where id = v_job_id;
      perform pgmq.archive('paloma_pipeline', v_message.msg_id);
      perform ingest.refresh_pipeline_runs_for_job(v_job_id);
      continue;
    end if;

    v_attempt_no := v_job.attempt_count + 1;
    update ingest.pipeline_jobs
    set state = 'running',
        attempt_count = v_attempt_no,
        locked_by = p_worker_id,
        locked_at = now(),
        lease_expires_at = clock_timestamp() + make_interval(secs => p_visibility_seconds),
        started_at = coalesce(started_at, now()),
        error_code = null,
        error_summary = null,
        updated_at = now()
    where id = v_job_id;

    insert into ingest.pipeline_job_attempts (
      job_id, attempt_no, message_id, worker_id
    ) values (
      v_job_id, v_attempt_no, v_message.msg_id, p_worker_id
    )
    on conflict on constraint pipeline_job_attempt_unique do update set
      message_id = excluded.message_id,
      worker_id = excluded.worker_id,
      state = 'running',
      retryable = null,
      result = null,
      error_code = null,
      error_summary = null,
      started_at = now(),
      finished_at = null;

    perform ingest.refresh_pipeline_runs_for_job(v_job_id);

    job_id := v_job_id;
    message_id := v_message.msg_id;
    job_type := v_job.job_type;
    payload := v_job.payload;
    attempt_no := v_attempt_no;
    max_attempts := v_job.max_attempts;
    select coalesce(array_agg(run_id order by run_id), '{}'::uuid[])
      into run_ids
    from ingest.pipeline_run_jobs
    where ingest.pipeline_run_jobs.job_id = v_job_id;
    return next;
  end loop;
end;
$$;

create or replace function ingest.complete_pipeline_job(
  p_job_id uuid,
  p_message_id bigint,
  p_worker_id text,
  p_result jsonb default '{}'::jsonb
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_job record;
  v_archived boolean;
begin
  if p_result is null or jsonb_typeof(p_result) <> 'object'
     or pg_column_size(p_result) > 65536 then
    raise exception 'pipeline result must be an object no larger than 64 KiB';
  end if;

  select * into v_job
  from ingest.pipeline_jobs
  where id = p_job_id
  for update;

  if not found
     or v_job.state <> 'running'
     or v_job.locked_by is distinct from p_worker_id
     or v_job.last_message_id is distinct from p_message_id then
    raise exception 'pipeline job lease is not owned by this worker';
  end if;

  select pgmq.archive('paloma_pipeline', p_message_id) into v_archived;
  if not coalesce(v_archived, false) then
    raise exception 'pipeline message could not be archived';
  end if;

  update ingest.pipeline_job_attempts
  set state = 'succeeded',
      retryable = false,
      result = p_result,
      finished_at = now()
  where job_id = p_job_id and attempt_no = v_job.attempt_count;

  update ingest.pipeline_jobs
  set state = 'succeeded',
      result = p_result,
      locked_by = null,
      locked_at = null,
      lease_expires_at = null,
      error_code = null,
      error_summary = null,
      finished_at = now(),
      updated_at = now()
  where id = p_job_id;

  perform ingest.refresh_pipeline_runs_for_job(p_job_id);
  return true;
end;
$$;

create or replace function ingest.fail_pipeline_job(
  p_job_id uuid,
  p_message_id bigint,
  p_worker_id text,
  p_error_code text,
  p_error_summary text,
  p_retryable boolean,
  p_retry_delay_seconds integer default 60
)
returns table (job_state text, next_attempt_at timestamptz)
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_job record;
  v_message_id bigint;
  v_archived boolean;
  v_retry boolean;
  v_next_attempt_at timestamptz;
begin
  if p_error_code is null or length(p_error_code) not between 1 and 100 then
    raise exception 'invalid pipeline error code';
  end if;
  if p_error_summary is null or length(p_error_summary) not between 1 and 2000 then
    raise exception 'invalid pipeline error summary';
  end if;
  if p_retry_delay_seconds not between 0 and 604800 then
    raise exception 'retry delay must be between 0 and 604800 seconds';
  end if;

  select * into v_job
  from ingest.pipeline_jobs
  where id = p_job_id
  for update;

  if not found
     or v_job.state <> 'running'
     or v_job.locked_by is distinct from p_worker_id
     or v_job.last_message_id is distinct from p_message_id then
    raise exception 'pipeline job lease is not owned by this worker';
  end if;

  v_retry := p_retryable and v_job.attempt_count < v_job.max_attempts;

  update ingest.pipeline_job_attempts
  set state = 'failed',
      retryable = v_retry,
      error_code = p_error_code,
      error_summary = p_error_summary,
      finished_at = now()
  where job_id = p_job_id and attempt_no = v_job.attempt_count;

  if v_retry then
    v_next_attempt_at := clock_timestamp()
      + make_interval(secs => p_retry_delay_seconds);
    select message_id
    into v_message_id
    from pgmq.send(
      'paloma_pipeline',
      jsonb_build_object('job_id', p_job_id),
      v_next_attempt_at::timestamptz
    ) as message_id;

    select pgmq.archive('paloma_pipeline', p_message_id) into v_archived;
    if not coalesce(v_archived, false) then
      raise exception 'pipeline message could not be archived for retry';
    end if;

    update ingest.pipeline_jobs
    set state = 'queued',
        last_message_id = v_message_id,
        next_attempt_at = v_next_attempt_at,
        locked_by = null,
        locked_at = null,
        lease_expires_at = null,
        error_code = p_error_code,
        error_summary = p_error_summary,
        updated_at = now()
    where id = p_job_id;
    job_state := 'queued';
    next_attempt_at := v_next_attempt_at;
  else
    select pgmq.archive('paloma_pipeline', p_message_id) into v_archived;
    if not coalesce(v_archived, false) then
      raise exception 'pipeline message could not be archived after failure';
    end if;

    update ingest.pipeline_jobs
    set state = 'dead',
        locked_by = null,
        locked_at = null,
        lease_expires_at = null,
        error_code = p_error_code,
        error_summary = p_error_summary,
        finished_at = now(),
        updated_at = now()
    where id = p_job_id;
    job_state := 'dead';
    next_attempt_at := null;
  end if;

  perform ingest.refresh_pipeline_runs_for_job(p_job_id);
  return next;
end;
$$;

create or replace function ingest.renew_pipeline_job_lease(
  p_job_id uuid,
  p_message_id bigint,
  p_worker_id text,
  p_visibility_seconds integer default 900
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_updated integer;
begin
  if p_visibility_seconds not between 30 and 7200 then
    raise exception 'visibility timeout must be between 30 and 7200 seconds';
  end if;

  perform 1
  from ingest.pipeline_jobs
  where id = p_job_id
    and state = 'running'
    and locked_by = p_worker_id
    and last_message_id = p_message_id
  for update;
  if not found then
    raise exception 'pipeline job lease is not owned by this worker';
  end if;

  perform 1 from pgmq.set_vt(
    'paloma_pipeline', p_message_id, p_visibility_seconds
  );
  get diagnostics v_updated = row_count;
  if v_updated <> 1 then
    raise exception 'pipeline message visibility could not be renewed';
  end if;

  update ingest.pipeline_jobs
  set lease_expires_at = clock_timestamp()
        + make_interval(secs => p_visibility_seconds),
      updated_at = now()
  where id = p_job_id;
  return true;
end;
$$;

create or replace function ingest.pipeline_queue_metrics()
returns table (
  queue_visible bigint,
  queue_total bigint,
  jobs_queued bigint,
  jobs_running bigint,
  jobs_succeeded_24h bigint,
  jobs_dead_24h bigint,
  oldest_queued_seconds bigint
)
language sql
security definer
set search_path = pg_catalog
as $$
  select
    (select count(*) from pgmq.q_paloma_pipeline where vt <= clock_timestamp()),
    (select count(*) from pgmq.q_paloma_pipeline),
    count(*) filter (where state = 'queued'),
    count(*) filter (where state = 'running'),
    count(*) filter (
      where state = 'succeeded' and finished_at >= now() - interval '24 hours'
    ),
    count(*) filter (
      where state = 'dead' and finished_at >= now() - interval '24 hours'
    ),
    coalesce(
      extract(
        epoch from (
          clock_timestamp() - min(created_at) filter (where state = 'queued')
        )
      )::bigint,
      0
    )
  from ingest.pipeline_jobs;
$$;

create or replace function ingest.purge_pipeline_history()
returns table (
  archived_messages_deleted bigint,
  runs_deleted bigint,
  jobs_deleted bigint
)
language plpgsql
security definer
set search_path = pg_catalog
as $$
begin
  delete from pgmq.a_paloma_pipeline
  where archived_at < now() - interval '30 days';
  get diagnostics archived_messages_deleted = row_count;

  delete from ingest.pipeline_runs
  where state in ('succeeded', 'partial', 'failed')
    and finished_at < now() - interval '180 days';
  get diagnostics runs_deleted = row_count;

  delete from ingest.pipeline_jobs job
  where job.state in ('succeeded', 'dead')
    and job.finished_at < now() - interval '180 days'
    and not exists (
      select 1 from ingest.pipeline_run_jobs run_job where run_job.job_id = job.id
    );
  get diagnostics jobs_deleted = row_count;

  return next;
end;
$$;

revoke all on function ingest.refresh_pipeline_runs_for_job(uuid) from public;
revoke all on function ingest.create_pipeline_run(text, text, jsonb) from public;
revoke all on function ingest.enqueue_pipeline_job(
  uuid, text, text, jsonb, integer, integer
) from public;
revoke all on function ingest.claim_pipeline_jobs(text, integer, integer) from public;
revoke all on function ingest.complete_pipeline_job(uuid, bigint, text, jsonb) from public;
revoke all on function ingest.fail_pipeline_job(
  uuid, bigint, text, text, text, boolean, integer
) from public;
revoke all on function ingest.renew_pipeline_job_lease(uuid, bigint, text, integer)
  from public;
revoke all on function ingest.pipeline_queue_metrics() from public;
revoke all on function ingest.purge_pipeline_history() from public;

grant execute on function ingest.create_pipeline_run(text, text, jsonb)
  to paloma_ingest;
grant execute on function ingest.enqueue_pipeline_job(
  uuid, text, text, jsonb, integer, integer
) to paloma_ingest;
grant execute on function ingest.claim_pipeline_jobs(text, integer, integer)
  to paloma_ingest;
grant execute on function ingest.complete_pipeline_job(uuid, bigint, text, jsonb)
  to paloma_ingest;
grant execute on function ingest.fail_pipeline_job(
  uuid, bigint, text, text, text, boolean, integer
) to paloma_ingest;
grant execute on function ingest.renew_pipeline_job_lease(uuid, bigint, text, integer)
  to paloma_ingest;
grant execute on function ingest.pipeline_queue_metrics() to paloma_ingest;

do $$
declare
  existing_job_id bigint;
begin
  select jobid into existing_job_id
  from cron.job
  where jobname = 'paloma-pipeline-history-purge';

  if existing_job_id is not null then
    perform cron.unschedule(existing_job_id);
  end if;

  perform cron.schedule(
    'paloma-pipeline-history-purge',
    '17 9 * * *',
    'select ingest.purge_pipeline_history();'
  );
end;
$$;

comment on table ingest.pipeline_runs is
  'Logical, auditable batches of deduplicated background catalog work.';
comment on table ingest.pipeline_jobs is
  'Durable job state paired with messages in the private paloma_pipeline pgmq queue.';
comment on table ingest.pipeline_job_attempts is
  'Immutable-per-attempt execution history for retries, errors, and worker ownership.';
comment on function ingest.enqueue_pipeline_job(uuid, text, text, jsonb, integer, integer) is
  'Idempotently creates or joins an active job and sends one private pgmq message.';
