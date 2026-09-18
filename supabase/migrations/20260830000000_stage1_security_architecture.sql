-- Stage 1 production hardening: durable render queue, abuse controls and
-- server-only billing transitions. Apply after 20260829000000.

-- A browser must never be able to grant itself a paid plan. The demo helper
-- remains available only to the backend service role for explicitly enabled
-- local/staging demonstrations.
revoke all on function public.complete_demo_checkout(text, integer)
  from public, anon, authenticated;
drop function public.complete_demo_checkout(text, integer);

alter table public.plan_catalog
  add column if not exists max_duration_seconds integer not null default 300
    check (max_duration_seconds between 20 and 1800),
  add column if not exists max_concurrent_jobs integer not null default 1
    check (max_concurrent_jobs between 1 and 20);

update public.plan_catalog set
  max_duration_seconds = case id
    when 'free' then 60
    when 'pro' then 300
    when 'pro_ultra' then 900
  end,
  max_concurrent_jobs = case id
    when 'free' then 1
    when 'pro' then 2
    when 'pro_ultra' then 3
  end
where id in ('free', 'pro', 'pro_ultra');

alter table public.video_jobs
  add column if not exists available_at timestamptz not null default now(),
  add column if not exists locked_at timestamptz,
  add column if not exists locked_by text,
  add column if not exists heartbeat_at timestamptz,
  add column if not exists attempt_count integer not null default 0
    check (attempt_count >= 0),
  add column if not exists max_attempts integer not null default 3
    check (max_attempts between 1 and 10);

create index if not exists video_jobs_queue_idx
  on public.video_jobs(status, available_at, created_at)
  where status = 'queued';

-- Replace the quota reservation function so allowance, plan duration and
-- per-user concurrency are enforced in one transaction.
create or replace function public.create_video_job_with_quota(
  p_user_id uuid,
  p_job_id text,
  p_source_document_id uuid,
  p_prompt text,
  p_audience text,
  p_style text,
  p_requested_duration_seconds integer,
  p_mode text,
  p_quality_mode text,
  p_request_payload jsonb
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_plan_id text;
  v_used integer;
  v_limit integer;
  v_max_duration integer;
  v_max_concurrent integer;
  v_active integer;
  v_period_start timestamptz;
  v_reset_at timestamptz;
begin
  select current_plan_id, videos_used, plan_period_start, plan_reset_at
    into v_plan_id, v_used, v_period_start, v_reset_at
    from public.profiles where id = p_user_id for update;

  if not found then
    return jsonb_build_object('allowed', false, 'error', 'Profile not found');
  end if;

  if v_reset_at <= now() then
    v_used := 0;
    v_period_start := now();
    v_reset_at := now() + interval '30 days';
    update public.profiles
      set videos_used = 0, plan_period_start = v_period_start,
          plan_reset_at = v_reset_at
      where id = p_user_id;
    insert into public.usage_events
      (user_id, event_type, quantity, period_start, metadata)
      values (p_user_id, 'quota_reset', 0, v_period_start,
              jsonb_build_object('automatic', true));
  end if;

  select video_limit, max_duration_seconds, max_concurrent_jobs
    into v_limit, v_max_duration, v_max_concurrent
    from public.plan_catalog where id = v_plan_id and active;

  if v_limit is null then
    return jsonb_build_object('allowed', false, 'error', 'Active plan not found');
  end if;
  if p_requested_duration_seconds > v_max_duration then
    return jsonb_build_object(
      'allowed', false, 'reason', 'duration_limit',
      'max_duration_seconds', v_max_duration
    );
  end if;

  select count(*) into v_active from public.video_jobs
    where user_id = p_user_id and status in ('queued', 'running');
  if v_active >= v_max_concurrent then
    return jsonb_build_object(
      'allowed', false, 'reason', 'concurrency_limit',
      'active', v_active, 'limit', v_max_concurrent
    );
  end if;
  if v_used >= v_limit then
    return jsonb_build_object(
      'allowed', false, 'reason', 'quota_limit', 'used', v_used,
      'limit', v_limit, 'reset_at', v_reset_at
    );
  end if;

  insert into public.video_jobs (
    id, user_id, source_document_id, prompt, audience, style,
    requested_duration_seconds, mode, quality_mode, status, stage,
    progress, detail, request_payload, available_at
  ) values (
    p_job_id, p_user_id, p_source_document_id, p_prompt, p_audience, p_style,
    p_requested_duration_seconds, p_mode, p_quality_mode, 'queued', 'queued',
    0, 'Queued', coalesce(p_request_payload, '{}'::jsonb), now()
  );

  update public.profiles set videos_used = v_used + 1 where id = p_user_id;
  insert into public.usage_events
    (user_id, job_id, event_type, quantity, period_start)
    values (p_user_id, p_job_id, 'video_started', 1, v_period_start);

  return jsonb_build_object(
    'allowed', true, 'used', v_used + 1, 'limit', v_limit,
    'remaining', v_limit - v_used - 1, 'reset_at', v_reset_at
  );
end;
$$;

-- Workers claim one job using SKIP LOCKED, so multiple worker processes can
-- safely poll without rendering the same job twice.
create or replace function public.claim_video_job(p_worker_id text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare v_job public.video_jobs;
begin
  select * into v_job from public.video_jobs
    where status = 'queued'
      and available_at <= now()
      and attempt_count < max_attempts
    order by created_at
    for update skip locked
    limit 1;
  if not found then return null; end if;

  update public.video_jobs set
    status = 'running', stage = 'starting', detail = 'Worker claimed job',
    locked_by = left(p_worker_id, 200), locked_at = now(),
    heartbeat_at = now(), started_at = coalesce(started_at, now()),
    attempt_count = attempt_count + 1
  where id = v_job.id
  returning * into v_job;

  return to_jsonb(v_job);
end;
$$;

create or replace function public.heartbeat_video_job(
  p_job_id text, p_worker_id text
) returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.video_jobs set heartbeat_at = now()
    where id = p_job_id and status = 'running' and locked_by = p_worker_id;
  return found;
end;
$$;

-- Run periodically. Retry interrupted jobs while attempts remain; terminally
-- fail exhausted jobs so the existing trigger refunds their reserved quota.
create or replace function public.recover_stale_video_jobs(
  p_stale_after_seconds integer default 300
) returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare v_count integer;
begin
  with recovered as (
    update public.video_jobs set
      status = case when attempt_count >= max_attempts then 'failed' else 'queued' end,
      stage = case when attempt_count >= max_attempts then 'failed' else 'queued' end,
      detail = case when attempt_count >= max_attempts
        then 'Worker stopped responding' else 'Retrying interrupted render' end,
      error_code = case when attempt_count >= max_attempts then 'worker_timeout' else null end,
      error_message = case when attempt_count >= max_attempts
        then 'The render worker stopped responding.' else null end,
      available_at = now() + interval '15 seconds',
      locked_by = null, locked_at = null, heartbeat_at = null
    where status = 'running'
      and coalesce(heartbeat_at, locked_at, started_at, created_at)
          < now() - make_interval(secs => greatest(p_stale_after_seconds, 60))
    returning 1
  ) select count(*) into v_count from recovered;
  return v_count;
end;
$$;

create table if not exists public.api_rate_limits (
  bucket_key text primary key,
  request_count integer not null default 0 check (request_count >= 0),
  window_started_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.api_rate_limits enable row level security;
revoke all on public.api_rate_limits from public, anon, authenticated;

create or replace function public.consume_rate_limit(
  p_bucket_key text, p_limit integer, p_window_seconds integer
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare v_row public.api_rate_limits; v_retry integer;
begin
  if p_limit < 1 or p_window_seconds < 1 then
    raise exception 'Invalid rate limit configuration';
  end if;
  insert into public.api_rate_limits(bucket_key, request_count)
    values (left(p_bucket_key, 300), 0)
    on conflict (bucket_key) do nothing;
  select * into v_row from public.api_rate_limits
    where bucket_key = left(p_bucket_key, 300) for update;
  if v_row.window_started_at + make_interval(secs => p_window_seconds) <= now() then
    update public.api_rate_limits set request_count = 1,
      window_started_at = now(), updated_at = now()
      where bucket_key = v_row.bucket_key;
    return jsonb_build_object('allowed', true, 'remaining', p_limit - 1);
  end if;
  if v_row.request_count >= p_limit then
    v_retry := greatest(1, ceil(extract(epoch from
      (v_row.window_started_at + make_interval(secs => p_window_seconds) - now())))::integer);
    return jsonb_build_object('allowed', false, 'remaining', 0,
                              'retry_after_seconds', v_retry);
  end if;
  update public.api_rate_limits set request_count = request_count + 1,
    updated_at = now() where bucket_key = v_row.bucket_key;
  return jsonb_build_object('allowed', true,
                            'remaining', p_limit - v_row.request_count - 1);
end;
$$;

revoke all on function public.claim_video_job(text) from public, anon, authenticated;
revoke all on function public.heartbeat_video_job(text, text) from public, anon, authenticated;
revoke all on function public.recover_stale_video_jobs(integer) from public, anon, authenticated;
revoke all on function public.consume_rate_limit(text, integer, integer) from public, anon, authenticated;
grant execute on function public.claim_video_job(text) to service_role;
grant execute on function public.heartbeat_video_job(text, text) to service_role;
grant execute on function public.recover_stale_video_jobs(integer) to service_role;
grant execute on function public.consume_rate_limit(text, integer, integer) to service_role;
