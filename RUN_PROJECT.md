# Running PencilVid / Explainora

This guide starts the current React frontend, Flask video-generation backend,
and Supabase authentication/database integration.

## 1. Prerequisites

Install:

- Python 3.10 or newer
- Node.js 18 or newer with npm
- FFmpeg and FFprobe, available on `PATH`
- Piper TTS with a voice model, or eSpeak
- A supported LLM provider API key (Groq, NVIDIA, OpenRouter, or Cerebras)
- A Supabase project

Verify the main executables:

```powershell
python --version
node --version
npm --version
ffmpeg -version
ffprobe -version
```

All commands below assume the terminal is open in the repository root—the
directory containing `app.py`, `requirements.txt`, and `frontend/`.

## 2. Install backend dependencies

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks virtual-environment activation, use the environment's
Python executable directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### macOS or Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 3. Install frontend dependencies

```powershell
cd frontend
npm install
cd ..
```

## 4. Configure Supabase

Create a Supabase project. In **Supabase Dashboard → SQL Editor**, run these
files in order:

1. `supabase/migrations/20260828000000_initial_schema.sql`
2. `supabase/migrations/20260829000000_atomic_quotas_and_blue_plans.sql`
3. `supabase/migrations/20260830000000_stage1_security_architecture.sql`

If the first two migrations are already installed, run only the third file.
The third migration removes browser-controlled plan upgrades and adds the
durable queue, plan duration/concurrency controls, and rate-limit buckets.

In **Authentication → URL Configuration**:

- Set the local site URL to `http://127.0.0.1:5173`.
- Add `http://localhost:5173` as an additional redirect URL if you use it.
- Add the production URL when the site is deployed.

Use one hostname consistently. Browser sessions for `127.0.0.1` and
`localhost` are separate.

## 5. Configure backend environment variables

Copy `.env.example` to `.env`:

```powershell
Copy-Item .env.example .env
```

macOS/Linux equivalent:

```bash
cp .env.example .env
```

Edit `.env` and configure at least:

```env
LLM_PROVIDER=groq
GROQ_API_KEY=your_provider_key

SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_SECRET_KEY=your_backend_secret_key

FFMPEG_BIN=ffmpeg
FFPROBE_BIN=ffprobe
TTS_BACKEND=auto
PORT=5000
APP_ENV=development
JOB_EXECUTION_MODE=inline
```

For Piper narration, also configure:

```env
TTS_BACKEND=piper
PIPER_BIN=piper
PIPER_MODEL=C:/absolute/path/to/en_US-lessac-medium.onnx
```

Alternatively, install eSpeak and use `TTS_BACKEND=espeak`.

Never place `SUPABASE_SECRET_KEY` in the frontend, prefix it with `VITE_`, or
commit `.env`.

## 6. Configure frontend environment variables

Copy the frontend example:

```powershell
Copy-Item frontend\.env.example frontend\.env.local
```

macOS/Linux equivalent:

```bash
cp frontend/.env.example frontend/.env.local
```

Edit `frontend/.env.local`:

```env
VITE_SUPABASE_URL=https://your-project-ref.supabase.co
VITE_SUPABASE_PUBLISHABLE_KEY=your_publishable_key
```

The frontend and backend Supabase URLs must identify the same project.
`frontend/.env.local` must contain the publishable key, never the secret key.

## 7. Start the project

Open two terminals in the repository root.

### Terminal 1 — Flask backend

Windows:

```powershell
.\.venv\Scripts\python.exe app.py
```

macOS/Linux:

```bash
source .venv/bin/activate
python app.py
```

The backend should report:

```text
Running on http://127.0.0.1:5000
```

### Terminal 2 — React frontend

```powershell
cd frontend
npm run dev
```

Open:

```text
http://127.0.0.1:5173
```

Do not use the Flask port as the normal browser entry point during frontend
development. Vite proxies `/api`, media, output, streaming, and Socket.IO
requests to Flask on port 5000.

### Queue mode (staging and production)

Set `JOB_EXECUTION_MODE=queue` and run a separate worker process:

```powershell
.\.venv\Scripts\python.exe worker.py
```

Production also requires `APP_ENV=production`, exact `ALLOWED_ORIGINS`, and a
random `MEDIA_SIGNING_KEY` of at least 32 characters. Never run production in
inline mode: an application restart would terminate active renders.

## 8. First-use check

1. Open `http://127.0.0.1:5173`.
2. Create an account or sign in.
3. Confirm the email if Supabase email confirmation is enabled.
4. Open the studio and generate a short test video.
5. Watch progress move through planning, narration, visuals, encoding, and
   finalization.
6. The completed MP4 is uploaded to the private Supabase `video-assets` bucket.
   Local output is temporary working data; Supabase Storage is authoritative.

An unauthenticated request to `http://127.0.0.1:5000/api/videos` should return
`401 Authentication required`; that is expected and confirms API protection.

## 9. Plan limits

The database enforces these limits per reset period:

| Plan | Video allowance | Theme |
|---|---:|---|
| Free | 5 | Light blue |
| Pro | 10 | Royal blue |
| Pro Ultra | 20 | Purple |

Quota reservation and job creation occur in one database transaction. Failed
or cancelled renders refund their reserved slot.

## 10. Tests and production build

Backend checks:

```powershell
python -m pip install pytest
.\.venv\Scripts\python.exe -m pytest tests\test_supabase_store.py tests\test_job_runtime_and_duration.py -q
```

Frontend checks:

```powershell
cd frontend
npm run lint
npm run build
```

## 11. Troubleshooting

### `Invalid or expired session`

- Hard-refresh the frontend and sign in again.
- Confirm both Supabase URLs point to the same project.
- Use either `127.0.0.1` or `localhost` consistently.
- Restart both servers after changing an environment file.
- Confirm the backend can access the internet and Supabase Auth.

### `Video quota service is temporarily unavailable`

- Run `20260829000000_atomic_quotas_and_blue_plans.sql` in the Supabase SQL
  Editor.
- Restart Flask after the migration completes.
- Confirm `SUPABASE_SECRET_KEY` is the backend secret key.

### FFmpeg or FFprobe not found

Install FFmpeg and ensure its `bin` directory is on `PATH`, or set absolute
paths in `FFMPEG_BIN` and `FFPROBE_BIN`.

### No TTS backend is available

Install/configure Piper or eSpeak. With `TTS_BACKEND=auto`, the backend uses
Piper when both its executable and model exist, otherwise it checks eSpeak.

### Port already in use

Stop the previous development process, or change `PORT` for Flask and update
the targets in `frontend/vite.config.ts` to match.

## 12. Generated files

- Final videos and manifests: `outputs/<job_id>/`
- Intermediate scenes/audio/video segments: `work/<job_id>/`
- Uploaded source documents: `work/uploads/`
- Run logs: `logs/runs/<job_id>.jsonl`

`.env`, `frontend/.env.local`, generated media, and local credentials should
remain outside Git.
