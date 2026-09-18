-- Stage 2: durable worker presence for readiness, alerting, and operations.

create table if not exists public.render_workers (
  worker_id text primary key check (char_length(worker_id) between 1 and 200),
  status text not null default 'starting'
    check (status in ('starting', 'idle', 'busy', 'stopped')),
  current_job_id text references public.video_jobs(id) on delete set null,
  started_at timestamptz not null default now(),
  last_heartbeat_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb
);

create index if not exists render_workers_heartbeat_idx
  on public.render_workers(last_heartbeat_at desc);

alter table public.render_workers enable row level security;
revoke all on public.render_workers from public, anon, authenticated;
grant select, insert, update, delete on public.render_workers to service_role;

comment on table public.render_workers is
  'Service-role-only worker presence. A worker is stale when last_heartbeat_at exceeds the configured alert threshold.';
