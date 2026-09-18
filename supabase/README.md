# Supabase setup for Explainora

The repository is ready to run without Supabase credentials (local demo mode),
and automatically switches to Supabase Auth and persistence when credentials
are present.

## 1. Create and migrate a project

Create a Supabase project, then run every file in `migrations/` in filename
order using the Supabase SQL Editor:

1. `20260828000000_initial_schema.sql`
2. `20260829000000_atomic_quotas_and_blue_plans.sql`
3. `20260830000000_stage1_security_architecture.sql`
4. `20260831000000_production_operations.sql`
5. `20260901000000_align_plan_duration_tiers.sql`
6. `20260902000000_account_lifecycle.sql`

The migrations create Auth-linked profiles, preferences, plans, subscriptions,
payment history, atomic quotas, source documents, generation jobs/events,
videos, scenes, asset metadata, usage history, private Storage buckets,
indexes, triggers, and row-level security policies.

The second and third migrations are required even if the first migration was previously
installed. It atomically reserves one allowance slot together with each job,
prevents concurrent requests from crossing the plan limit, avoids double
counting completed videos, and refunds failed or cancelled renders.

Supabase Auth owns email/password identities. Passwords are hashed and managed
inside `auth.users`; Explainora never stores plaintext passwords or copies password
hashes into `public` tables.

## 2. Add browser credentials

Copy `frontend/.env.example` to `frontend/.env.local` and replace both values:

```env
VITE_SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_YOUR_KEY
```

The publishable key is intentionally used in the browser. RLS protects each
user's rows.

## 3. Add backend credentials

Add these values to the repository-root `.env` (never to a frontend file):

```env
SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
SUPABASE_SECRET_KEY=sb_secret_YOUR_SERVER_KEY
```

The secret key bypasses RLS and must remain server-only. Never prefix it with
`VITE_`, commit it, paste it into client code, or include it in a URL.

## 4. Configure Auth

In Supabase Dashboard → Authentication → URL Configuration, set the local site
URL to `http://localhost:5173` and add these redirect URLs:

- `http://localhost:5173/auth/recovery`
- `https://explainora.dev/auth/recovery`

Choose whether email confirmation is required. The UI handles both immediate
sessions and confirmation-required signups.

Configure a custom SMTP provider before public rollout. Supabase's default mail
sender is best-effort and is too tightly rate-limited for production password
recovery. Keep leaked-password protection enabled. Password changes and account
deletion also verify the current password in the backend before proceeding.

The `account_security_audit` table stores keyed pseudonymous subject hashes,
never raw user IDs, emails, IPs, tokens, or passwords. Restrict it to the
service role and delete records older than 365 days with a scheduled database
job or an equivalent operational retention task.

## 5. Billing transition

The Stage 1 migration removes `complete_demo_checkout`; browsers cannot assign
paid plans. The checkout endpoint fails safely until a real provider adapter
and signed, idempotent webhook are implemented. Never update `current_plan_id`
from frontend code or from an unverified checkout-success redirect.

## 6. Durable render workers

Deploy the Flask service with `JOB_EXECUTION_MODE=queue`, then run `worker.py`
as a separate process using the same backend secrets and rendering tools.
Workers atomically claim queued jobs, heartbeat while rendering, and allow
stale interrupted work to be recovered without duplicate processing.

Worker presence is recorded in the service-role-only `render_workers` table.
Use `last_heartbeat_at` to alert when no worker has reported within 90 seconds.

## Migration ownership after Stage 2

The SQL Editor was appropriate for initial setup, but it does not populate the
Supabase CLI migration history. Before the first automated release, baseline
the already-applied versions once (see `DEPLOYMENT.md`). After that point, do
not make remote schema edits in the Dashboard. Create and commit a timestamped
file in `supabase/migrations/`; the approval-gated release workflow applies it.

## Data ownership

All private tables use `auth.uid()` RLS policies. Storage objects must be placed
under a user-prefixed path such as
`USER_UUID/videos/JOB_ID/final.mp4`. The backend also checks bearer tokens before
creating, listing, or opening database-backed jobs and videos.
