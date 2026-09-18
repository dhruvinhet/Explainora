-- Security events intentionally outlive account deletion and contain no raw
-- user id, email address, IP address, access token, or password material.
create table if not exists public.account_security_audit (
  id bigint generated always as identity primary key,
  subject_hash text not null check (char_length(subject_hash) = 64),
  event_type text not null check (char_length(event_type) between 1 and 80),
  outcome text not null check (outcome in ('requested', 'succeeded', 'rejected', 'blocked', 'failed')),
  request_id text check (request_id is null or char_length(request_id) <= 80),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists account_security_audit_subject_created_idx
  on public.account_security_audit(subject_hash, created_at desc);
create index if not exists account_security_audit_event_created_idx
  on public.account_security_audit(event_type, created_at desc);

alter table public.account_security_audit enable row level security;
revoke all on public.account_security_audit from public, anon, authenticated;
revoke all on sequence public.account_security_audit_id_seq from public, anon, authenticated;
grant select, insert, delete on public.account_security_audit to service_role;
grant usage, select on sequence public.account_security_audit_id_seq to service_role;

comment on table public.account_security_audit is
  'Pseudonymous account-security events. Apply the documented 365-day retention policy.';

-- Serialize deletion against quota reservation. A failed Storage cleanup keeps
-- the account locked against new jobs so a later deletion retry is safe.
alter table public.profiles
  add column if not exists account_deletion_pending boolean not null default false;

create or replace function public.prepare_account_deletion(p_user_id uuid)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_user_id uuid;
begin
  if auth.role() <> 'service_role' then
    raise exception 'service_role_required';
  end if;

  select id into v_user_id
  from public.profiles
  where id = p_user_id
  for update;

  if v_user_id is null then
    raise exception 'account_not_found';
  end if;
  if exists (
    select 1 from public.video_jobs
    where user_id = p_user_id and status in ('queued', 'running')
  ) then
    raise exception 'active_jobs';
  end if;

  update public.profiles
  set account_deletion_pending = true
  where id = p_user_id;
  return true;
end;
$$;

create or replace function public.prevent_jobs_during_account_deletion()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if exists (
    select 1 from public.profiles
    where id = new.user_id and account_deletion_pending
  ) then
    raise exception 'account_deletion_pending';
  end if;
  return new;
end;
$$;

drop trigger if exists video_jobs_block_account_deletion on public.video_jobs;
create trigger video_jobs_block_account_deletion
before insert on public.video_jobs
for each row execute function public.prevent_jobs_during_account_deletion();

revoke all on function public.prepare_account_deletion(uuid) from public, anon, authenticated;
revoke all on function public.prevent_jobs_during_account_deletion() from public, anon, authenticated;
grant execute on function public.prepare_account_deletion(uuid) to service_role;
