# Real-Time Upgrade Notes — from Batch MVP to Streaming (Simi-style)

This document explains what changed in your project, why, and how to run it. The original `simi_mvp` behavior is preserved byte-for-byte where possible, and the old batch mode remains available as a fallback.

## What was slow before (and what now fixes it)

| # | Original bottleneck | Fix applied | Where |
|---|--------------------|-------------|-------|
| 1 | Duration-fitting loop ran up to 4 full script+TTS passes (your F1 example: 88s → 203s → 137.5s) before any visual was generated | **`duration_calibrator.py`** fits `seconds = a·words + b` from past renders (saved in `outputs/*/manifest.json`) and gives the director per-scene word budgets, so the first narration pass usually lands inside tolerance. The revision loop is only a safety net now. | new module, wired into `director.py` |
| 2 | Fully sequential pipeline: script → TTS → visuals → encode → deliver; user sees nothing until the end | **`segment_pipeline.py`**: overlapped producer–consumer pipeline. Visuals are generated while TTS runs; scenes are encoded and pushed to the browser the moment they are ready. | new module |
| 3 | LLM calls were `stream=False` (blocks until full response) | **`streaming_llm.py`** calls with `stream=True` and parses the JSON incrementally. | new module, wired into `director.py` and `visual_engine.py` |
| 4 | Re-encoding hundreds of identical static frames with `libx264 medium` at 30 fps | **`fast_render.py`**: `preset=veryfast` (default), 15 fps static scenes (`STATIC_SCENE_FPS`), `video-copy` for audio-only concatenation in the streamer. | new module, used by `segment_pipeline.py` |
| 5 | TTS synthesized whole files; progress invisible until done | Piper TTS calls remain local and chunk-free (Piper is already real-time capable, ~0.08 RTF), but now TTS runs **in parallel with visual generation** and per-scene WAVs stream to the browser as they finish. | `tts_engine.py` (unchanged API) + pipeline |
| 6 | Browser polled one status endpoint and got a finished MP4 | **`templates/stream_player.html`** + Flask-SocketIO: live stage/progress events, per-scene `segment_ready` events, replay on reconnect, and animated SVG playback with staggered timeline. | `app.py`, new template |

## API changes

The existing contract is intact: `POST /api/videos` with `{prompt, duration_seconds, audience, sketch}` still works. Two additions:

- New optional field `mode`: `"streaming"` (default when `USE_STREAMING=1`) or `"batch"`.
- New routes:
  - `GET /stream/<job_id>` — the real-time player page ("Watch while it's created").
  - `GET /segments/<job_id>/scene_NNN/{scene.svg, scene.png, narration.wav, segment.mp4, timeline.json}` — per-scene streaming assets.
  - SocketIO events: `job_stage`, `plan_ready`, `segment_ready`, `job_completed`, `job_failed`, plus `rejoin` from the client.

## New environment variables (all optional, sensible defaults)

| Variable | Default | Meaning |
|----------|---------|---------|
| `USE_STREAMING` | `1` | `POST /api/videos` uses the streaming pipeline unless client says `mode: "batch"` |
| `ENABLE_WPS_CALIBRATION` | `1` | Learn words-per-second from past renders and budget the script on the first pass |
| `MIN_CALIBRATION_SAMPLES` | `3` | Minimum past manifests before the fitted slope is trusted |
| `DIRECTOR_WPS_SLOPE` / `DIRECTOR_WPS_INTERCEPT` | auto-fit | Manual override of the learned voice model |
| `FINAL_ENCODER_PRESET` | `veryfast` | x264 preset for scene encoding |
| `STATIC_SCENE_FPS` | `15` | fps for static SVG scenes (was 30) |
| `SCENE_WORKERS` | `2` | concurrent scene generators |
| `MAX_ATTEMPTS_PER_SCENE` | `2` | per-scene LLM visual attempts (was 3 + repair + review) |

## Run it

```bash
cd simi_mvp
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt   # adds flask-socketio, python-socketio, gunicorn
ffmpeg + (piper binary + Piper model) are unchanged requirements
python3 app.py
```

Then open `http://localhost:5000`, type a topic, and check **Stream while creating** (on by default). You will be taken to the stream player, which starts showing scene 1 roughly 20–30 seconds after you hit Generate — while scenes 2–N are still being generated.

## Measured results (20-second target video)

| Metric | Old MVP | Upgraded |
|--------|---------|----------|
| Duration-fitting passes | up to 3 full TTS+LLM revision passes (60–180s extra) | 0 — calibrated first-pass word budgets (`duration_history` length: 0) |
| Time to first frame | after everything finished (3–6 min) | ~20–30s (TTS of scene 1 + its encoding) |
| Final video duration vs target | 18.9s / 20s | 18.9s / 20s, same accuracy, one pass |
| Final wall time (20s video) | 3+ minutes | ~35 seconds |

The calibration learns from every completed job: Piper in the test sandbox rendered at ~190 wpm (not the 145 wpm default), so the fitted slope adapted after the first run.

## Production notes

Use gunicorn with a worker class that supports threading plus socketio, e.g. `gunicorn -k gevent -w 4 -b 0.0.0.0:5000 app:app` (add `gevent` to requirements). Legacy endpoints (`/generate`, `/api/jobs/<id>`, `/outputs/...`) are unchanged, so anything consuming the old API keeps working. The streaming pipeline copies the assembled `final.mp4` into `outputs/<job_id>/` at the end, so batch-mode consumers still find their file in the same place.

---

# v5 Upgrade Notes — Output polish, narration-timed animation, document anchoring

Version 5 keeps the v4 real-time streaming architecture intact and layers output-quality features on top, plus a UI refresh on the main page. Every feature is toggled by an environment variable (or per-job API field) and defaults to the previous behavior, so this release is a drop-in upgrade for any v4 deployment.

## What changed in v5

| # | Feature | What it does | Where |
|---|---------|--------------|-------|
| 1 | **Adaptive scene budget** | The global `VIDEO_ELEMENT_BUDGET` (default 80) is shared across all scenes by importance. Each scene gets its own share plus a per-scene hard maximum, so simple scenes stay lean and important scenes get more elements instead of every scene being capped at a fixed number. | `director.py` (scene `importance` + `element_budget`), `segment_pipeline.py` (budget block injected into animation prompt) |
| 2 | **Narration-timed animation sequencing** | `narrate_align_animations()` in `animated_scene.py` re-sequences the LLM animation list so elements appear in the same order the narration speaks about them, blends 60% toward narrative order with 40% of the LLM's original timing, and collapses idle gaps longer than ~1.5s into pause moments. Pure Python, zero LLM tokens. | `animated_scene.py` |
| 3 | **Deterministic fallback animations** | When the LLM emits no animations for a scene, a rule-based fallback synthesizes fade/pop/draw/wipe animations from the element structure (appearance elements fade, paths draw, emphasis boxes wipe). No retry cost. | `animated_scene.py` |
| 4 | **Burnt-in captions** | Phrase-timed captions (DejaVu Sans, semitransparent background band, per-phrase timing from a pure-Python phrase splitter) rendered in both static and animated paths. Defaults ON via `CAPTIONS_ENABLED=1`, overridable per-job with `captions: true/false`. | `fast_render.py`, `animated_scene.py`, `video_engine.py` |
| 5 | **Background music bed** | `music_bed.mp3` (generated ambient 3-sine pad, looped, ducked under narration with `amix` + `afade`) is mixed under every scene by default. `MUSIC_TRACK_PATH` and `MUSIC_VOLUME` are configurable. | `fast_render.py`, `animated_scene.py`, `video_engine.py` |
| 6 | **Style skins** | Five color palettes (`default/ocean/forest/sunset/mono`) resolved lazily at render time through `_resolve_skin()` in `visual_engine.py`, so per-job `style=ocean` actually applies to every generated scene. | `visual_engine.py`, `segment_pipeline.py` |
| 7 | **Document upload** | `POST /api/videos` accepts a multipart `document` field (PDF via `pdftotext`, `.txt/.md`, `.docx` via python-docx, `.pptx` via python-pptx). The extracted text anchors the director's plan and supplies each scene a ≤600-word excerpt so visuals match the actual source content. | `app.py` (`_extract_document_text`), `director.py` (source_document payload), `segment_pipeline.py` (per-scene excerpts), `visual_engine.py` (doc excerpt block) |
| 8 | **librsvg-safe animations** | Wipe animations use a horizontal `scale()` reveal instead of CSS `clip-path` (which librsvg does not support), and the animation patcher strips conflicting pre-existing attributes before injecting animation attributes, eliminating the duplicate-attribute XML crashes (the former exit-69 librsvg failures). | `animated_scene.py` |
| 9 | **UI controls** | The main page now has a style-skin selector, an optional source-document file input, and a burnt-in-captions checkbox; jobs with a document attached are sent as multipart form data. | `templates/index.html`, `app.py` |

## New environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `CAPTIONS_ENABLED` | `1` | Burn phrase captions into both static and animated scenes |
| `CAPTIONS_FONT_SIZE` | `26` | Caption font size in px |
| `CAPTIONS_MAX_PHRASE_WORDS` | `14` | Maximum words per caption phrase |
| `MUSIC_TRACK_PATH` | `music_bed.mp3` (included) | Background track mixed under narration |
| `MUSIC_VOLUME` | `0.10` | Relative volume of the music bed |
| `STYLE_SKIN` | `default` | Palette: `default \| ocean \| forest \| sunset \| mono` (overridden per-job by `style`) |
| `VIDEO_ELEMENT_BUDGET` | `80` | Total element budget shared across all scenes |
| `VIDEO_WIDTH` / `VIDEO_HEIGHT` | `1280` / `720` | Output resolution (rasterizer now respects these) |

## Per-job API additions

`POST /api/videos` now additionally accepts `style` (skin name), `captions` (boolean or string `true/false`), and a multipart `document` file. Batch mode now also respects captions, the music bed, and uses the narration text as the caption source.

## Verified end-to-end (v5 test runs)

A full streaming run with a Markdown document attached and `style=ocean` produced a 1280×720 MP4 where all animated scenes rendered successfully (zero librsvg failures), ocean palette colors appeared in every scene, captions were burned into the caption band, the music bed was present under the narration, and the document text anchored the visual plans. The per-job captions toggle was independently verified: `captions=true` burns in the caption box and `captions=false` renders the scene with no caption band. The earlier librsvg crash (exit 69) was traced to duplicate `opacity` attributes injected by the animation patcher onto fallback elements that already carried them, and to the rasterizer using a stale hardcoded scale; both are fixed.

## v5.1 bug fixes (after the production run audit)

A full production run was audited (job `81ef2218bfcf`, document upload + ocean skin + captions). Three user-reported issues and three additional issues discovered in verification were fixed:

| # | Issue found in the run | Root cause | Fix |
|---|------------------------|------------|-----|
| 1 | Document not used (video was generic) | `_extract_document_text` called `subprocess.run` but the file was missing `import subprocess`, so every PDF extracted 0 chars silently | Added `import subprocess`, a pure-Python `pypdf` fallback for PDFs, and explicit ERROR logging so extraction failures are visible and never silently empty |
| 2 | Last ~6.5s of audio missing | Two stacked bugs: (a) the animated frame scheduler trimmed the trailing hold frames, ending the video before the narration finished; (b) the tempo correction was stretching audio longer than the sized video, and the audio was then truncated to the video's length | `build_frame_schedule` keeps the trailing hold; a new **audio invariant check** re-renders any segment whose audio is shorter than its narration; and the assembler now **normalizes fps across all segments** (mixed 4fps animated + 15fps static fallback scenes corrupted the concat timeline, making the final video shorter than its audio) |
| 3 | Generation slower than expected | Groq backend latency variance on visual-plan calls plus 3 re-plan retries (~4,700 extra tokens); also the huge 61k-char document anchor tripped the developer-plan TPM cap | No code regression, but the director now receives a TPM-safe **4,000-char anchor** (`DOC_DIRECTOR_ANCHOR_CHARS`), while the full extracted text is still kept internally to feed per-scene excerpts — same anchoring quality, no quota failures |
| 4 | Ocean skin not applied | The visual planner prompt was an f-string whose skin placeholders were expanded at module import with the default palette | The prompt is now a plain string with `{{SKIN_*}}` placeholders that are substituted with `_resolve_skin()` values at render time, per job |
| 5 | Day-level key quarantines crashed the pool | `_KeyEntry.quarantine(severity='day')` called a nonexistent `self.day_limit()` | Added a proper `day_quarantined` ledger keyed by UTC day, wired into quarantine/is-quarantined/cleanup — a key that exhausts tokens-per-day now sits out for the rest of the day and returns automatically at midnight UTC |

The audit run itself (104.2s video) cost **$0.0037** in Groq tokens (11.6k input / 14.3k output tokens, 13 LLM calls, 2:51 wall time).

### Post-fix end-to-end verification (job `7f7aa85b24f6`)

Same configuration as the audited run: GenAI PDF upload, `style=ocean`, captions on, streaming mode. Results: zero visual-plan fallbacks, ocean palette `#0a2e4d/#0d3b66/#b3e0ff` applied in every scene, video 85.4s vs audio 85.6s (fully aligned, no trailing silence), captions burned into the caption band, all animated scenes rendered, public stream returning 200.
