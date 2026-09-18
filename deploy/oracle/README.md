# Oracle Cloud production setup

This is the single-production-environment deployment selected for PencilVid:

```text
Cloudflare -> Caddy on Oracle Ampere A1 -> web container
                                          worker container
                                                |
                                  existing Supabase Auth/Postgres/Storage
```

The Oracle disk is scratch space and a short-lived render cache. Supabase
private Storage remains authoritative for uploaded sources and generated
videos. The Supabase `video_jobs` table is the durable queue; a VM restart
does not discard queued work.

## 1. Create the VM

Create an Ubuntu ARM64 instance in the OCI home region using
`VM.Standard.A1.Flex`, 2 OCPUs, 12 GB RAM, and a 100–150 GB boot volume. Reserve
its public IP. In the OCI subnet security list allow:

- TCP 22 from your own administration IP only;
- TCP 80 and 443 from the internet initially;
- no public access to port 5000.

Copy `bootstrap-ubuntu.sh` to the VM and run it once:

```bash
sudo bash bootstrap-ubuntu.sh
```

Sign out and back in, then verify `docker version` and `docker compose version`.
The bootstrap creates `/opt/pencilvid`, enables UFW, and adds a 4 GB swap file
to reduce out-of-memory failures during FFmpeg peaks.

## 2. Configure the runtime

Copy `production.env.example` to
`/opt/pencilvid/env/production.env`, populate it on the server, and run:

```bash
chmod 600 /opt/pencilvid/env/production.env
```

Do not paste `SUPABASE_SECRET_KEY`, provider API keys, database passwords, or
the media signing key into GitHub variables or commit them. Before the first
automated deployment, authenticate the VM to GHCR using a GitHub token with
only `read:packages` permission:

```bash
echo "$GHCR_READ_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
unset GHCR_READ_TOKEN
```

## 3. Configure Cloudflare

Create an `A` record for the application hostname pointing at the reserved OCI
IP. Keep it DNS-only until Caddy has obtained its first certificate, verify
HTTPS, then enable the orange-cloud proxy and set SSL/TLS mode to **Full
(strict)**.

Do not create a "Cache Everything" rule for `/api/*`, authenticated HTML, or
signed URLs. Static JS/CSS can use normal Cloudflare caching. Generated videos
currently come from Supabase Storage, not the Oracle origin, so a Cloudflare
rule on the application hostname cannot cache those off-site objects.

## 4. Configure the GitHub `production` environment

Secrets:

| Name | Value |
|---|---|
| `SUPABASE_ACCESS_TOKEN` | Supabase personal access token used by the CLI |
| `SUPABASE_DB_PASSWORD` | Password for the existing project database |
| `OCI_SSH_PRIVATE_KEY` | Dedicated deployment private key |
| `OCI_KNOWN_HOSTS` | Verified `known_hosts` line for the reserved VM IP |

Variables:

| Name | Value |
|---|---|
| `SUPABASE_PROJECT_REF` | Existing project reference |
| `VITE_SUPABASE_URL` | Existing public Supabase URL |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | Existing browser-safe publishable key |
| `OCI_HOST` | Reserved OCI public IP or administration hostname |
| `OCI_USER` | Usually `ubuntu` |
| `APP_DOMAIN` | Public application hostname, without `https://` |
| `VITE_PUBLIC_DEMO_URL` | Optional demo URL |
| `VITE_PUBLIC_DEMO_POSTER_URL` | Optional poster URL |
| `VITE_PUBLIC_DEMO_TITLE` | Accessible title for the public demo |

Protect the environment with a required reviewer. The release workflow builds
both AMD64 and ARM64 images, turns on maintenance for an existing deployment,
applies forward migrations to the one Supabase project, and deploys the exact
commit-SHA image. It only disables maintenance after `/readyz` succeeds.

## 5. Operate maintenance mode

On the VM:

```bash
cd /opt/pencilvid
./maintenance.sh on
./maintenance.sh off
```

While enabled, `/healthz` remains HTTP 200, `/readyz` becomes HTTP 503, API
traffic receives a JSON 503 response, and browser traffic sees the maintenance
page. If a release fails readiness, the deployment stays in maintenance mode
for investigation.

## 6. First release checklist

1. Baseline the three migrations already applied through the SQL Editor.
2. Confirm the production-operations migration is the only pending migration.
3. Add the GitHub environment secrets and variables above.
4. Run CI and require every job to pass.
5. Run the manual `Release` workflow from `main`.
6. Confirm `/readyz` reports `"tts":{"detail":"piper","ok":true}`.
7. Verify sign-in, quota reservation, one render with the expected Lessac
   voice, playback, account, and logout.
8. Configure an external uptime check for `/readyz` and a weekly off-server
   database plus Storage export before accepting real user data.
