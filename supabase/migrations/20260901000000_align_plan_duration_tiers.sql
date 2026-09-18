-- Align the public 2/4/6-minute composer with server-enforced plan limits.
-- Apply after 20260831000000_production_operations.sql.

begin;

update public.plan_catalog
set
  max_duration_seconds = case id
    when 'free' then 120
    when 'pro' then 240
    when 'pro_ultra' then 360
  end,
  updated_at = now()
where id in ('free', 'pro', 'pro_ultra');

commit;
