# Duration-aware Explain → SVG Scenes → Local TTS → MP4

**v5 (this release)** — adds narration-timed object animations, adaptive per-scene element budgets, burnt-in captions, background music bed, 5 style skins, and document upload (PDF/Word/PPT/MD/TXT), on top of the v4 real-time streaming player and 8-key Groq pool. See `UPGRADE_NOTES.md` for the full v4/v5 changelog.

## Pipeline

1. User sends a topic/question + desired duration.
2. Director LLM creates a strict `must_cover` / `out_of_scope` scope lock.
3. Director writes the complete scene-by-scene narration and visual prompts.
4. Local TTS generates narration and the app measures the real audio duration.
5. If it is too short/long, the LLM revises depth **inside the same topic**. It does not cut the ending or drift into adjacent topics.
6. A small final FFmpeg `atempo` correction lands near the exact requested duration without cutting audio.
7. Once narration is approved, SVG scenes are generated in parallel using the existing visual engine.
8. SVG → PNG → scene MP4, then FFmpeg concatenates all scenes into `final.mp4`.

## Files

- `app.py` — Flask API, background job runner, orchestration, progress polling.
- `llm_provider.py` — shared NVIDIA, Groq, and OpenRouter configuration/client factory.
- `director.py` — duration-aware scope/script planner + duration revision.
- `visual_engine.py` — your existing generic visual planner/normalizer/SVG renderer, extracted from `test.py`.
- `tts_engine.py` — fully local TTS abstraction: Piper preferred, eSpeak fallback.
- `video_engine.py` — SVG rasterization and FFmpeg scene/final MP4 rendering.
- `templates/index.html` — UI for prompt, duration, audience, progress, and final MP4.

## Requirements

Python 3.10+ is recommended.

System executables:

```bash
ffmpeg -version
ffprobe -version
```

For local narration, use either:

### Option A: Piper (recommended)

Install Piper and download a local voice model. Then set:

```bash
TTS_BACKEND=piper
PIPER_BIN=piper
PIPER_MODEL=/absolute/path/to/voice.onnx
```

### Option B: eSpeak (works immediately on many Linux systems)

```bash
TTS_BACKEND=espeak
TTS_VOICE=en-us
TTS_SPEED_WPM=145
```

eSpeak is useful for engineering/testing; Piper sounds much better for the actual product.

## Setup

```bash
cd simi_mvp
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
```

Choose `LLM_PROVIDER=nvidia`, `groq`, `openrouter`, or `cerebras` in `.env`, then add the matching API key.

### LLM providers

NVIDIA (keeps the existing setup):

```bash
LLM_PROVIDER=nvidia
NVIDIA_API_KEY=your_key
NVIDIA_DIRECTOR_MODEL=deepseek-ai/deepseek-v4-flash-0731
NVIDIA_PLANNER_MODEL=deepseek-ai/deepseek-v4-flash-0731
```

Groq (recommended split for the limits shown):

```bash
LLM_PROVIDER=groq
GROQ_API_KEY=your_key
GROQ_DIRECTOR_MODEL=openai/gpt-oss-120b
GROQ_PLANNER_MODEL=openai/gpt-oss-120b
GROQ_TPM_SAFETY_TOKENS=300
```

Groq completion budgets are automatically reduced so the estimated prompt plus requested output stays below the model's free-tier TPM limit. Set `GROQ_TPM_LIMIT` only if your account has a different or upgraded limit.

The scheduler also enforces the published free-plan request/day limits for the
configured GPT-OSS and Qwen models (30 RPM, 1,000 RPD, 8,000 TPM and 200,000
TPD per organization). Override `GROQ_RPM_LIMIT`, `GROQ_RPD_LIMIT`,
`GROQ_TPM_LIMIT`, or `GROQ_TPD_LIMIT` when the Groq Limits page for an
organization shows a different tier. Multiple keys increase throughput only
when they belong to independent organizations/projects with independent
quotas; keys in the same organization share its ceiling.

OpenRouter:

```bash
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=your_key
OPENROUTER_DIRECTOR_MODEL=openai/gpt-oss-120b
OPENROUTER_PLANNER_MODEL=qwen/qwen3.6-27b
OPENROUTER_HTTP_REFERER=http://localhost:5000
OPENROUTER_APP_TITLE=Explainora
```

Cerebras (OpenAI-compatible API):

```bash
LLM_PROVIDER=cerebras
CEREBRAS_API_KEY=your_key
CEREBRAS_BASE_URL=https://api.cerebras.ai/v1
CEREBRAS_DIRECTOR_MODEL=gpt-oss-120b
CEREBRAS_PLANNER_MODEL=gpt-oss-120b
CEREBRAS_FAST_PLANNER_MODEL=gpt-oss-120b
CEREBRAS_TIMEOUT_SECONDS=30
CEREBRAS_RPM_LIMIT=5
CEREBRAS_RPH_LIMIT=150
CEREBRAS_RPD_LIMIT=2400
```

Add extra Cerebras keys as a comma-separated pool. Request admission uses
rolling minute/hour/day counters per key, rotates before a configured limit is
exceeded, and immediately quarantines and replaces a key that returns HTTP 429:

```bash
CEREBRAS_API_KEYS=csk_extra_key_1,csk_extra_key_2,csk_extra_key_3
CEREBRAS_KEY_WAIT_MAX_SECONDS=70
```

Provider-specific model variables take precedence over the legacy `DIRECTOR_MODEL` and `PLANNER_MODEL` variables. You may also mix services with `DIRECTOR_PROVIDER` and `PLANNER_PROVIDER`.

For example, keep the director on Groq and use Cerebras only for visual planning:

```bash
LLM_PROVIDER=groq
PLANNER_PROVIDER=cerebras
```

Fast quality mode first checks `<PROVIDER>_FAST_PLANNER_MODEL` (for example,
`CEREBRAS_FAST_PLANNER_MODEL`), then the legacy `FAST_PLANNER_MODEL`, and finally
the provider's normal planner model. Restart the server after changing `.env`.

Then run:

```bash
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## v4 Changelog

- **Real-time streaming player**: `stream.mp4` grows atomically per finished scene; the browser plays a real HTML5 `<video>` with all standard controls. Smooth-transition rule only reloads the source near the end of the buffered content (or before first playback), so appending new scenes never causes a black flash mid-playback.
- **Object-level semantic animations**: the visual planner now MUST emit a compact `animations` array (typed DSL `{t, e, a, d}`) in the same single API call — no extra tokens per scene. The `animated_scene.py` engine renders per-frame SVG patches (fade, wipe, pop, scale, draw/ink) synchronized with the narration, then re-encodes only the animated frames.
- **Multi-key API pool (8 keys)**: `api_key_pool.py` load-balances Groq calls by least-loaded key with per-minute TPM tracking, minute-quarantine on 429, day-quarantine on tokens-per-day exhaustion, and automatic failover with fresh checkout on retry — more scenes than keys is handled by reusing freed keys.
- **Per-run detailed logging**: every LLM call (director, planner/visual, streaming) and TTS call is recorded to `logs/runs/<job_id>.jsonl` with stage, model, key prefix, input/output tokens, latency, retries, and status. `GET /api/logs/<job_id>` returns totals, per-model and per-key breakdowns, and an estimated USD cost; `GET /api/logs/<job_id>/calls` returns raw call records.
- **Downloadable model-boundary diagnostics**: every new LLM record also stores
  the complete messages/parameters sent to the provider and the returned model
  content (never API keys or authorization headers). The UI exposes
  `GET /api/logs/<job_id>/download` as one portable JSON bundle, including
  failed jobs, so planner output can be compared with normalized scene plans.
- **Speed**: WPS duration calibrator, streaming LLM calls, veryfast/ultrafast encoding at 15fps, overlapped per-scene pipeline, WPS-based scene scheduling, and optional gentle tempo correction.

### Piper voice model (not included in the zip)

Download once and point `PIPER_MODEL` at it:

```bash
wget -qO en_US-lessac-medium.onnx https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
```

## API

Create a video:

```bash
curl -X POST http://127.0.0.1:5000/api/videos \\
  -H "Content-Type: application/json" \\
  -d '{
    "prompt": "Explain binary search to a beginner with an example",
    "duration_seconds": 120,
    "audience": "beginner",
    "sketch": false
  }'
```

Response:

```json
{
  "job_id": "abc123...",
  "status_url": "/api/jobs/abc123..."
}
```

Poll:

```bash
curl http://127.0.0.1:5000/api/jobs/abc123...
```

When complete it returns `output_url` and `manifest_url`.

## Important MVP notes

- The Flask job store is in memory. Run a single process/worker for this MVP. For production, move jobs to Redis + Celery/RQ or a database-backed queue.
- Scenes are currently static SVG visuals held for the duration of narration. The next layer can animate SVG elements using a scene animation plan rather than replacing this architecture.
- Iconify is still network-backed because that is how the existing visual engine fetches icons. Cache is retained locally after fetch.
- `DIRECTOR_MODEL` can be stronger than `PLANNER_MODEL`. A stronger director is usually worthwhile because script completeness and scope control affect the whole video.
- DeepSeek V4 models run with reasoning disabled (`reasoning_effort=none` and `thinking=false`) for both directing and visual planning.
- Video output is 16:9. The existing 1024×768 SVG is scaled and white-padded to 1280×720, so the working SVG renderer itself does not need to be destabilized yet.

## Duration behavior

Example: `Explain binary search`.

- At 2 minutes: complete essentials, one clear worked example, complexity intuition, recap.
- At 5 minutes: same scope, but deeper intuition, more detailed example, mistakes/edge cases, stronger recap.
- It should not stop halfway at 2 minutes or begin teaching binary trees at 5 minutes.

The enforcement lives in `director.py` and `_fit_narration_duration()` in `app.py`.

Long-form requests also enforce a maximum static-scene hold (30 seconds by
default). If a director returns too few scenes, its existing paired narration
and visual beats are repartitioned locally before rendering. A five-minute
request therefore has at least ten watchable scenes without topic-specific
templates or another visual-planning call. Configure `MAX_SCENE_HOLD_SECONDS`
to change that policy.

Narration within `DURATION_TOLERANCE_SECONDS` of the requested duration is accepted without another script revision or final tempo correction. The default is 20 seconds, so a 2-minute request accepts a final duration from 1:40 through 2:20.
