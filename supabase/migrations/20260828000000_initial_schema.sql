  -- Simi Studio: Auth, profiles, billing, quotas, generation jobs, videos and assets.
  -- Passwords stay exclusively in Supabase Auth (auth.users); never copy them into public tables.

  create extension if not exists pgcrypto;

  create table public.plan_catalog (
    id text primary key check (id in ('free', 'pro', 'pro_ultra')),
    display_name text not null unique,
    monthly_price_cents integer not null default 0 check (monthly_price_cents >= 0),
    currency text not null default 'USD',
    video_limit integer not null check (video_limit > 0),
    theme_color text not null,
    features jsonb not null default '[]'::jsonb,
    active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
  );

  insert into public.plan_catalog (id, display_name, monthly_price_cents, video_limit, theme_color, features) values
    ('free', 'Free', 0, 5, '#60a5fa', '["Standard video quality","Personal video library","Shareable exports"]'),
    ('pro', 'Pro', 1900, 10, '#1e40af', '["High-quality exports","Priority generation","Everything in Free"]'),
    ('pro_ultra', 'Pro Ultra', 3900, 20, '#7c3aed', '["Premium video quality","Fastest render priority","Everything in Pro"]')
  on conflict (id) do update set display_name = excluded.display_name, monthly_price_cents = excluded.monthly_price_cents, video_limit = excluded.video_limit, theme_color = excluded.theme_color, features = excluded.features;

  create table public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    full_name text not null default '',
    avatar_url text,
    phone text,
    timezone text not null default 'UTC',
    locale text not null default 'en',
    current_plan_id text not null default 'free' references public.plan_catalog(id),
    plan_period_start timestamptz not null default now(),
    plan_reset_at timestamptz not null default (now() + interval '30 days'),
    videos_used integer not null default 0 check (videos_used >= 0),
    onboarding_completed boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
  );

  create table public.user_preferences (
    user_id uuid primary key references public.profiles(id) on delete cascade,
    default_audience text not null default 'beginner',
    default_style text not null default 'default',
    default_duration_seconds integer not null default 120,
    captions_enabled boolean not null default true,
    email_notifications boolean not null default true,
    reduced_motion boolean,
    metadata jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
  );

  create table public.subscriptions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id) on delete cascade,
    plan_id text not null references public.plan_catalog(id),
    status text not null check (status in ('trialing','active','past_due','canceled','expired')),
    provider text not null default 'demo',
    provider_customer_id text,
    provider_subscription_id text,
    current_period_start timestamptz not null default now(),
    current_period_end timestamptz not null default (now() + interval '30 days'),
    cancel_at_period_end boolean not null default false,
    canceled_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
  );

  create unique index subscriptions_one_current_per_user on public.subscriptions(user_id) where status in ('trialing','active','past_due');

  create table public.payment_transactions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id) on delete cascade,
    subscription_id uuid references public.subscriptions(id) on delete set null,
    plan_id text not null references public.plan_catalog(id),
    provider text not null default 'demo',
    provider_payment_id text,
    status text not null check (status in ('pending','succeeded','failed','refunded')),
    amount_cents integer not null check (amount_cents >= 0),
    currency text not null default 'USD',
    receipt_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    confirmed_at timestamptz
  );

  create table public.source_documents (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id) on delete cascade,
    original_filename text not null,
    storage_path text,
    mime_type text,
    size_bytes bigint check (size_bytes is null or size_bytes >= 0),
    extracted_text_length integer not null default 0,
    checksum_sha256 text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
  );

  create table public.video_jobs (
    id text primary key check (id ~ '^[0-9a-f]{12}$'),
    user_id uuid not null references public.profiles(id) on delete cascade,
    source_document_id uuid references public.source_documents(id) on delete set null,
    prompt text not null,
    audience text not null default 'beginner',
    style text not null default 'default',
    requested_duration_seconds integer not null,
    mode text not null default 'batch',
    quality_mode text not null default 'balanced',
    status text not null check (status in ('queued','running','completed','failed','canceled')),
    stage text not null default 'queued',
    progress integer not null default 0 check (progress between 0 and 100),
    detail text,
    error_code text,
    error_message text,
    request_payload jsonb not null default '{}'::jsonb,
    runtime_config jsonb not null default '{}'::jsonb,
    llm_usage jsonb not null default '{}'::jsonb,
    estimated_cost_usd numeric(12,6),
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
  );

  create table public.videos (
    id text primary key references public.video_jobs(id) on delete cascade,
    user_id uuid not null references public.profiles(id) on delete cascade,
    title text not null,
    description text not null default '',
    audience text not null,
    style text not null,
    duration_seconds numeric(10,3) not null,
    scene_count integer not null default 0,
    video_storage_path text,
    poster_storage_path text,
    manifest_storage_path text,
    evaluation_storage_path text,
    video_url text,
    poster_url text,
    manifest jsonb not null default '{}'::jsonb,
    coverage jsonb not null default '[]'::jsonb,
    is_public boolean not null default false,
    share_slug text unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
  );

  create table public.video_scenes (
    id uuid primary key default gen_random_uuid(),
    job_id text not null references public.video_jobs(id) on delete cascade,
    video_id text references public.videos(id) on delete cascade,
    user_id uuid not null references public.profiles(id) on delete cascade,
    scene_index integer not null check (scene_index >= 0),
    title text not null,
    narration text not null default '',
    visual_prompt text,
    status text not null default 'pending' check (status in ('pending','active','done','failed')),
    duration_seconds numeric(10,3),
    image_storage_path text,
    image_url text,
    scene_manifest jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(job_id, scene_index)
  );

  create table public.video_assets (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id) on delete cascade,
    job_id text not null references public.video_jobs(id) on delete cascade,
    video_id text references public.videos(id) on delete cascade,
    scene_id uuid references public.video_scenes(id) on delete cascade,
    asset_type text not null check (asset_type in ('source','image','audio','segment','video','poster','manifest','evaluation','caption')),
    storage_bucket text not null,
    storage_path text not null,
    mime_type text,
    size_bytes bigint,
    checksum_sha256 text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique(storage_bucket, storage_path)
  );

  create table public.generation_events (
    id bigint generated always as identity primary key,
    user_id uuid not null references public.profiles(id) on delete cascade,
    job_id text not null references public.video_jobs(id) on delete cascade,
    event_type text not null,
    stage text,
    progress integer,
    message text,
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
  );

  create table public.usage_events (
    id bigint generated always as identity primary key,
    user_id uuid not null references public.profiles(id) on delete cascade,
    job_id text references public.video_jobs(id) on delete set null,
    event_type text not null check (event_type in ('video_started','video_completed','video_failed','quota_reset','credit_adjustment')),
    quantity integer not null default 1,
    period_start timestamptz not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
  );

  create index video_jobs_user_created_idx on public.video_jobs(user_id, created_at desc);
  create index videos_user_created_idx on public.videos(user_id, created_at desc);
  create index video_scenes_job_idx on public.video_scenes(job_id, scene_index);
  create index generation_events_job_idx on public.generation_events(job_id, created_at);
  create index usage_events_user_period_idx on public.usage_events(user_id, period_start);
  create index payments_user_created_idx on public.payment_transactions(user_id, created_at desc);

  create or replace function public.set_updated_at() returns trigger language plpgsql set search_path = '' as $$
  begin new.updated_at = now(); return new; end; $$;

  create trigger profiles_set_updated_at before update on public.profiles for each row execute function public.set_updated_at();
  create trigger preferences_set_updated_at before update on public.user_preferences for each row execute function public.set_updated_at();
  create trigger subscriptions_set_updated_at before update on public.subscriptions for each row execute function public.set_updated_at();
  create trigger jobs_set_updated_at before update on public.video_jobs for each row execute function public.set_updated_at();
  create trigger videos_set_updated_at before update on public.videos for each row execute function public.set_updated_at();
  create trigger scenes_set_updated_at before update on public.video_scenes for each row execute function public.set_updated_at();

  create or replace function public.log_job_change() returns trigger language plpgsql security definer set search_path = '' as $$
  begin
    insert into public.generation_events (user_id, job_id, event_type, stage, progress, message, payload)
    values (new.user_id, new.id, 'job_updated', new.stage, new.progress, new.detail, jsonb_build_object('status', new.status));
    if new.status = 'failed' and old.status is distinct from new.status then
      insert into public.usage_events (user_id, job_id, event_type, period_start, metadata)
      values (new.user_id, new.id, 'video_failed', new.created_at, jsonb_build_object('error', new.error_message));
    end if;
    return new;
  end; $$;

  create trigger video_jobs_log_change after update on public.video_jobs for each row execute function public.log_job_change();

  create or replace function public.record_completed_video() returns trigger language plpgsql security definer set search_path = '' as $$
  begin
    update public.profiles set videos_used = videos_used + 1 where id = new.user_id;
    insert into public.usage_events (user_id, job_id, event_type, period_start)
    select new.user_id, new.id, 'video_completed', plan_period_start from public.profiles where id = new.user_id;
    return new;
  end; $$;

  create trigger videos_record_completion after insert on public.videos for each row execute function public.record_completed_video();

  create or replace function public.handle_new_user() returns trigger language plpgsql security definer set search_path = '' as $$
  begin
    insert into public.profiles (id, full_name) values (new.id, coalesce(new.raw_user_meta_data ->> 'full_name', ''));
    insert into public.user_preferences (user_id) values (new.id);
    insert into public.subscriptions (user_id, plan_id, status, provider) values (new.id, 'free', 'active', 'system');
    return new;
  end; $$;

  create trigger on_auth_user_created after insert on auth.users for each row execute function public.handle_new_user();

  -- DEMO ONLY. Replace this RPC with a verified payment-provider webhook before production billing.
  create or replace function public.complete_demo_checkout(target_plan text, amount_cents integer default 0)
  returns uuid language plpgsql security definer set search_path = '' as $$
  declare caller uuid := auth.uid(); payment_id uuid; subscription_id uuid; charged_amount integer := greatest(amount_cents, 0);
  begin
    if caller is null then raise exception 'Authentication required'; end if;
    if not exists (select 1 from public.plan_catalog where id = target_plan and active) then raise exception 'Unknown plan'; end if;
    update public.subscriptions set status = 'expired', current_period_end = now() where user_id = caller and status in ('trialing','active','past_due');
    insert into public.subscriptions (user_id, plan_id, status, provider) values (caller, target_plan, 'active', 'demo') returning id into subscription_id;
    insert into public.payment_transactions (user_id, subscription_id, plan_id, provider, status, amount_cents, confirmed_at, receipt_data)
      values (caller, subscription_id, target_plan, 'demo', 'succeeded', charged_amount, now(), jsonb_build_object('test_mode', true)) returning id into payment_id;
    update public.profiles set current_plan_id = target_plan, videos_used = 0, plan_period_start = now(), plan_reset_at = now() + interval '30 days' where id = caller;
    return payment_id;
  end; $$;

  alter table public.plan_catalog enable row level security;
  alter table public.profiles enable row level security;
  alter table public.user_preferences enable row level security;
  alter table public.subscriptions enable row level security;
  alter table public.payment_transactions enable row level security;
  alter table public.source_documents enable row level security;
  alter table public.video_jobs enable row level security;
  alter table public.videos enable row level security;
  alter table public.video_scenes enable row level security;
  alter table public.video_assets enable row level security;
  alter table public.generation_events enable row level security;
  alter table public.usage_events enable row level security;

  create policy "plans are readable" on public.plan_catalog for select to anon, authenticated using (active);
  create policy "users read own profile" on public.profiles for select to authenticated using ((select auth.uid()) = id);
  create policy "users update own profile" on public.profiles for update to authenticated using ((select auth.uid()) = id) with check ((select auth.uid()) = id);
  create policy "users manage own preferences" on public.user_preferences for all to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
  create policy "users read own subscriptions" on public.subscriptions for select to authenticated using ((select auth.uid()) = user_id);
  create policy "users read own payments" on public.payment_transactions for select to authenticated using ((select auth.uid()) = user_id);
  create policy "users manage own sources" on public.source_documents for all to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
  create policy "users read own jobs" on public.video_jobs for select to authenticated using ((select auth.uid()) = user_id);
  create policy "users read own videos" on public.videos for select to authenticated using ((select auth.uid()) = user_id);
  create policy "public videos are readable" on public.videos for select to anon using (is_public);
  create policy "users read own scenes" on public.video_scenes for select to authenticated using ((select auth.uid()) = user_id);
  create policy "users read own assets" on public.video_assets for select to authenticated using ((select auth.uid()) = user_id);
  create policy "users read own generation events" on public.generation_events for select to authenticated using ((select auth.uid()) = user_id);
  create policy "users read own usage" on public.usage_events for select to authenticated using ((select auth.uid()) = user_id);

  revoke all on all tables in schema public from anon;
  revoke all on public.plan_catalog, public.profiles, public.user_preferences, public.subscriptions, public.payment_transactions, public.source_documents, public.video_jobs, public.videos, public.video_scenes, public.video_assets, public.generation_events, public.usage_events from authenticated;
  grant select on public.plan_catalog to anon, authenticated;
  grant select on public.profiles, public.subscriptions, public.payment_transactions, public.video_jobs, public.videos, public.video_scenes, public.video_assets, public.generation_events, public.usage_events to authenticated;
  grant select, insert, update, delete on public.user_preferences, public.source_documents to authenticated;
  grant update (full_name, avatar_url, phone, timezone, locale, onboarding_completed) on public.profiles to authenticated;
  grant usage, select on all sequences in schema public to authenticated;
  revoke execute on function public.complete_demo_checkout(text, integer) from public, anon;
  revoke execute on function public.complete_demo_checkout(text, integer) from authenticated;

  insert into storage.buckets (id, name, public) values
    ('avatars', 'avatars', false),
    ('source-documents', 'source-documents', false),
    ('video-assets', 'video-assets', false)
  on conflict (id) do nothing;

  create policy "users read own storage" on storage.objects for select to authenticated using ((storage.foldername(name))[1] = (select auth.uid())::text);
  create policy "users upload own storage" on storage.objects for insert to authenticated with check (bucket_id in ('avatars','source-documents','video-assets') and (storage.foldername(name))[1] = (select auth.uid())::text);
  create policy "users update own storage" on storage.objects for update to authenticated using ((storage.foldername(name))[1] = (select auth.uid())::text) with check ((storage.foldername(name))[1] = (select auth.uid())::text);
  create policy "users delete own storage" on storage.objects for delete to authenticated using ((storage.foldername(name))[1] = (select auth.uid())::text);
