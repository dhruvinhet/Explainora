-- User-owned generation cancellation. The application calls this function
-- with its service role after authenticating the caller.

create or replace function public.cancel_video_job(p_user_id uuid, p_job_id text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_status text;
begin
  select status into v_status
  from public.video_jobs
  where id = p_job_id and user_id = p_user_id
  for update;

  if not found then
    return jsonb_build_object('outcome', 'not_found');
  end if;

  if v_status = 'canceled' then
    return jsonb_build_object('outcome', 'already_canceled', 'status', v_status);
  end if;

  if v_status not in ('queued', 'running') then
    return jsonb_build_object('outcome', 'not_cancelable', 'status', v_status);
  end if;

  update public.video_jobs
  set status = 'canceled',
      stage = 'canceled',
      detail = 'Canceled by user',
      error_code = 'user_canceled',
      error_message = null,
      completed_at = now(),
      locked_by = null,
      locked_at = null,
      heartbeat_at = null
  where id = p_job_id and user_id = p_user_id;

  -- The existing log_job_change trigger refunds the reserved plan slot once
  -- when the status first changes to canceled.
  return jsonb_build_object('outcome', 'canceled', 'status', 'canceled');
end;
$$;

revoke all on function public.cancel_video_job(uuid, text) from public, anon, authenticated;
grant execute on function public.cancel_video_job(uuid, text) to service_role;

comment on function public.cancel_video_job(uuid, text) is
  'Atomically cancels an owned queued/running render. Service-role only.';
