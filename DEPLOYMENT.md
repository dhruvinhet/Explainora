# PencilVid production deployment

This is the Stage 2 operations runbook. It keeps the web service, render
worker, database migrations, and environment secrets independently deployable.

## Architecture

```text
Browser -> HTTPS host -> web container -> Supabase Postgres/Storage
                                      |
                                      v
                                durable job queue
                                      |
                                      v
                              worker container(s)
                                      |
                                      v
                              Supabase private Storage
```

The web container never renders in production. It validates the authenticated
request and reserves quota transactionally, then returns a queued job. Workers
claim jobs with `FOR UPDATE SKIP LOCKED`; all final assets are uploaded to
private Supabase Storage. Local `work/` and `outputs/` are scratch space only.

## 1. Local production image

The production Compose file pins `FFMPEG_BIN=ffmpeg`, `FFPROBE_BIN=ffprobe`,
and pins `TTS_BACKEND=piper` inside Linux. The immutable image installs the
native Piper runtime and downloads the
`en_US-lessac-medium` ONNX voice from its pinned voice release with SHA-256
verification. Compose also pins the container paths for the executable and
model, preventing Windows development paths in `.env` from overriding them.
Piper is fail-closed in production: if its executable or ONNX model is
missing, readiness fails instead of silently generating with a different
voice. An emergency switch to eSpeak therefore requires an explicit reviewed
Compose change.

Start Docker Desktop, copy `.env.example` to `.env`, and set real development
credentials. Do not commit `.env`.

```powershell
docker compose -f compose.production.yaml config
docker compose -f compose.production.yaml build
docker compose -f compose.production.yaml up -d
docker compose -f compose.production.yaml ps
Invoke-RestMethod http://127.0.0.1:5000/healthz
Invoke-RestMethod http://127.0.0.1:5000/readyz
```

`/healthz` is a liveness probe and deliberately does not call dependencies.
`/readyz` verifies the frontend build, writable scratch directories, FFmpeg,
FFprobe, TTS, SVG rendering, Postgres access, and the private video bucket.

To stop the stack:

```powershell
docker compose -f compose.production.yaml down
```

## 2. Supabase environment

The initial deployment deliberately uses the existing hosted Supabase project
for production and the Supabase CLI stack for local migration tests. There is
no hosted staging database at the current scale. Releases therefore enable the
application maintenance gate before applying forward-only migrations and turn
it off only after the immutable web image passes readiness.

This is an accepted early-stage availability tradeoff, not full environment
isolation. Create a separate staging project before the team grows, before a
destructive data migration, or before production data makes manual recovery
unacceptable. Never copy production users or source documents into staging.

The existing project was initially migrated in the SQL Editor. Before the
first CLI-managed release, compare the actual schema, then mark only the SQL
files that are already present as applied:

```powershell
supabase link --project-ref YOUR_PROJECT_REF
supabase migration list
supabase migration repair --status applied 20260828000000
supabase migration repair --status applied 20260829000000
supabase migration repair --status applied 20260830000000
supabase db push --dry-run
```

The dry run should show only `20260831000000_production_operations.sql`. Only
repair migration history after verifying the three earlier migrations really
are installed. A future fresh staging project needs no repair; `supabase db
push` applies every migration.

After baselining, all schema changes must be new committed migrations. Never
edit the remote production schema directly in Table Editor or SQL Editor.

For account recovery, Authentication -> URL Configuration must allow
`https://explainora.dev/auth/recovery` (and the localhost equivalent during
development). Configure a custom SMTP sender before public rollout; the
Supabase default sender is not a production delivery service. Migration
`20260902000000_account_lifecycle.sql` adds the service-role-only pseudonymous
security audit. Purge audit entries older than 365 days on a scheduled basis.

## 3. GitHub environment and secrets

In Repository Settings -> Environments, create `production`. Protect it with a
required reviewer and restrict it to `main`. Also protect `main` and require
every `CI` job before merge.

Add these secrets to each environment:

| Name | Purpose |
|---|---|
| `SUPABASE_ACCESS_TOKEN` | Supabase CLI personal access token |
| `SUPABASE_DB_PASSWORD` | Database password for that environment |
| `OCI_SSH_PRIVATE_KEY` | Dedicated Oracle VM deployment private key |
| `OCI_KNOWN_HOSTS` | Verified SSH host-key line for the Oracle VM |

Add these non-secret environment variables:

| Name | Purpose |
|---|---|
| `SUPABASE_PROJECT_REF` | Project id from the Supabase dashboard URL |
| `VITE_SUPABASE_URL` | Public project API URL embedded in the frontend |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | Public browser key embedded in the frontend |
| `VITE_PUBLIC_DEMO_URL` | Optional public 30-second demo clip |
| `VITE_PUBLIC_DEMO_POSTER_URL` | Optional public demo poster |
| `OCI_HOST` | Reserved Oracle VM public IP or admin hostname |
| `OCI_USER` | Oracle Ubuntu SSH user, normally `ubuntu` |
| `APP_DOMAIN` | Public hostname without a URL scheme |

The `Release` workflow is manual and approval-gated. It publishes AMD64 and
ARM64 images, enables maintenance, performs a database dry run, applies pending
migrations, and deploys the immutable commit-SHA image to Oracle. It never
places runtime secrets inside the image. See
[`deploy/oracle/README.md`](deploy/oracle/README.md) for initial VM and
Cloudflare setup.

## 4. Oracle runtime secrets

The selected Oracle Ampere A1 deployment runs one web service and one worker
from the same SHA-tagged image. Caddy is the only service exposing public ports;
port 5000 remains on a private Docker network. The worker is intentionally
single-job-at-a-time for the 2-OCPU Always Free allowance.

Required runtime secrets:

- `SUPABASE_URL`
- `SUPABASE_SECRET_KEY` (server secret/service role; never `VITE_`)
- `MEDIA_SIGNING_KEY` (unique random value, at least 32 characters)
- every configured LLM provider key used by the director/planner

Production preserves role-specific provider routing rather than forcing every
request through one service. The recommended configuration is:

```env
LLM_PROVIDER=groq
DIRECTOR_PROVIDER=cerebras
PLANNER_PROVIDER=groq
CEREBRAS_API_KEY=...
CEREBRAS_API_KEYS=...
GROQ_API_KEY=...
GROQ_API_KEYS=...
```

Provider-specific model variables such as `CEREBRAS_DIRECTOR_MODEL`,
`GROQ_PLANNER_MODEL`, and `GROQ_FAST_PLANNER_MODEL` remain authoritative. Keep
the populated file only at `/opt/pencilvid/env/production.env`; never commit it
or bake it into the container image.

Required runtime configuration:

```env
APP_ENV=production
JOB_EXECUTION_MODE=queue
USE_STREAMING=0
PORT=5000
ALLOWED_ORIGINS=https://YOUR_DOMAIN
LOG_FORMAT=json
```

Web command (already the Docker image default):

```text
gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 --timeout 120 app:app
```

Worker command:

```text
python worker.py
```

Set the platform liveness path to `/healthz` and readiness path to `/readyz`.
Allow workers a long termination grace period so active renders can finish.
Autoscale workers cautiously; the database enforces per-user concurrency, but
provider and host CPU limits still apply.

## 5. Monitoring and alerts

Container logs are JSON on stdout and every web response includes
`X-Request-ID`. Configure the host to retain logs and search by that id.

Production installs the exact versions in `requirements.lock`. Dependency
updates must regenerate that lock, pass CI, and pass the image smoke test before
release; do not let the hosting platform resolve unbounded versions at deploy.

Minimum alerts:

- `/readyz` non-200 for 2 consecutive minutes;
- no `render_workers.last_heartbeat_at` within 90 seconds while queued jobs exist;
- any job in `running` without `heartbeat_at` for 5 minutes;
- queue age over 10 minutes;
- render failure rate over 10% in 15 minutes;
- Supabase database/storage usage over 80%;
- LLM provider error or rate-limit spikes.

Do not expose raw prompts, uploaded document contents, access tokens, or
provider keys in logs. Rehearse alerts locally and perform a controlled
maintenance-window test before production launch.

## 6. Backups and restore drills

If the existing project is on Supabase Free, managed downloadable backups are
not guaranteed. Schedule a weekly logical database dump and a separate export
of the private Storage bucket to off-server storage before accepting real user
data. On Supabase Pro or above, enable managed daily backups at minimum; if
losing up to one day is unacceptable, enable Point-in-Time Recovery. Database
backups do not include Storage objects.

Quarterly restore drill:

1. record the latest restorable timestamp and choose a non-production target;
2. restore or duplicate into an isolated recovery project;
3. point a temporary worker/web deployment at the recovery project;
4. verify Auth profiles, plans, quota counters, jobs, videos, and signed assets;
5. record recovery point objective (RPO) and recovery time objective (RTO);
6. delete the isolated recovery environment after evidence is retained.

Never test a restore by overwriting the live production project.

## 7. Release and rollback

1. Merge only after the `CI` workflow passes.
2. Approve and run `Release` for the protected production environment.
3. The workflow enables maintenance, applies migrations, and deploys the exact
   GHCR SHA to Oracle.
4. Verify `/readyz`, sign-in, password recovery delivery/callback, password
   change plus forced sign-in, quota reservation, one render, playback, and
   logout. Test account deletion only with a dedicated disposable user and
   verify its private Storage prefixes were removed.
5. Before paid beta or general-public release, complete every release gate in
   `LEGAL_REVIEW.md`, including professional review and an end-to-end delivery
   test for `support@explainora.dev`.

Application rollback means redeploying the preceding immutable SHA. Database
rollbacks must be forward migrations; do not delete an applied migration or
run destructive down scripts against production.
