-- Run after `supabase db reset` with the Supabase CLI.
begin;
select plan(5);
select has_table('public', 'profiles', 'profiles exists');
select has_table('public', 'video_jobs', 'video_jobs exists');
select has_table('public', 'videos', 'videos exists');
select has_table('public', 'payment_transactions', 'payments exists');
select policies_are('public', 'profiles', array['users read own profile','users update own profile'], 'profile RLS policies exist');
select * from finish();

-- Manual deployment checks: expect 5/light blue, 10/royal blue, 20/purple;
-- authenticated_can_reserve=false and service_role_can_reserve=true.
select id, video_limit, theme_color
from public.plan_catalog
order by monthly_price_cents;

select
  has_function_privilege(
    'authenticated',
    'public.create_video_job_with_quota(uuid,text,uuid,text,text,text,integer,text,text,jsonb)',
    'execute'
  ) as authenticated_can_reserve,
  has_function_privilege(
    'service_role',
    'public.create_video_job_with_quota(uuid,text,uuid,text,text,text,integer,text,text,jsonb)',
    'execute'
  ) as service_role_can_reserve;
rollback;
