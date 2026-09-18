-- Atomically reserve plan allowance when a render job is created.
-- Existing projects should run this migration after 20260828000000_initial_schema.sql.

update public.plan_catalog set theme_color = case id
  when 'free' then '#60a5fa'
  when 'pro' then '#1e40af'
  when 'pro_ultra' then '#7c3aed'
end
where id in ('free', 'pro', 'pro_ultra');

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
  v_period_start timestamptz;
  v_reset_at timestamptz;
begin
  select current_plan_id, videos_used, plan_period_start, plan_reset_at
    into v_plan_id, v_used, v_period_start, v_reset_at
    from public.profiles
    where id = p_user_id
    for update;

  if not found then
    return jsonb_build_object('allowed', false, 'error', 'Profile not found');
  end if;

  if v_reset_at <= now() then
    v_used := 0;
    v_period_start := now();
    v_reset_at := now() + interval '30 days';
    update public.profiles
      set videos_used = 0, plan_period_start = v_period_start, plan_reset_at = v_reset_at
      where id = p_user_id;
    insert into public.usage_events (user_id, event_type, quantity, period_start, metadata)
      values (p_user_id, 'quota_reset', 0, v_period_start, jsonb_build_object('automatic', true));
  end if;

  select video_limit into v_limit from public.plan_catalog where id = v_plan_id and active;
  if v_limit is null then
    return jsonb_build_object('allowed', false, 'error', 'Active plan not found');
  end if;
  if v_used >= v_limit then
    return jsonb_build_object('allowed', false, 'used', v_used, 'limit', v_limit, 'reset_at', v_reset_at);
  end if;

  insert into public.video_jobs (
    id, user_id, source_document_id, prompt, audience, style,
    requested_duration_seconds, mode, quality_mode, status, stage,
    progress, detail, request_payload
  ) values (
    p_job_id, p_user_id, p_source_document_id, p_prompt, p_audience, p_style,
    p_requested_duration_seconds, p_mode, p_quality_mode, 'queued', 'queued',
    0, 'Queued', coalesce(p_request_payload, '{}'::jsonb)
  );

  update public.profiles set videos_used = v_used + 1 where id = p_user_id;
  insert into public.usage_events (user_id, job_id, event_type, quantity, period_start)
    values (p_user_id, p_job_id, 'video_started', 1, v_period_start);

  return jsonb_build_object(
    'allowed', true,
    'used', v_used + 1,
    'limit', v_limit,
    'remaining', v_limit - v_used - 1,
    'reset_at', v_reset_at
  );
end;
$$;

-- A slot is consumed at job creation, so completion records usage without
-- incrementing the profile counter a second time.
create or replace function public.record_completed_video() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  insert into public.usage_events (user_id, job_id, event_type, period_start)
  select new.user_id, new.id, 'video_completed', plan_period_start
    from public.profiles where id = new.user_id;
  return new;
end;
$$;

-- Failed or cancelled renders return their reserved slot exactly once.
create or replace function public.log_job_change() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  insert into public.generation_events (user_id, job_id, event_type, stage, progress, message, payload)
  values (new.user_id, new.id, 'job_updated', new.stage, new.progress, new.detail, jsonb_build_object('status', new.status));

  if new.status = 'failed' and old.status is distinct from new.status then
    update public.profiles set videos_used = greatest(videos_used - 1, 0) where id = new.user_id;
    insert into public.usage_events (user_id, job_id, event_type, quantity, period_start, metadata)
    select new.user_id, new.id, 'video_failed', -1, plan_period_start,
      jsonb_build_object('error', new.error_message, 'quota_refunded', true)
      from public.profiles where id = new.user_id;
  elsif new.status = 'canceled' and old.status is distinct from new.status then
    update public.profiles set videos_used = greatest(videos_used - 1, 0) where id = new.user_id;
    insert into public.usage_events (user_id, job_id, event_type, quantity, period_start, metadata)
    select new.user_id, new.id, 'credit_adjustment', -1, plan_period_start,
      jsonb_build_object('reason', 'job_canceled', 'quota_refunded', true)
      from public.profiles where id = new.user_id;
  end if;
  return new;
end;
$$;

revoke all on function public.create_video_job_with_quota(uuid, text, uuid, text, text, text, integer, text, text, jsonb) from public, anon, authenticated;
grant execute on function public.create_video_job_with_quota(uuid, text, uuid, text, text, text, integer, text, text, jsonb) to service_role;
