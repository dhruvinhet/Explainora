# Work state — v3 fixes (Aug 15, 2026)

## User's feedback on v2 (to fix now)
1. Streaming player felt like slides, not video — no play/pause/seek. User wants old MP4-player-like UI (their screenshot shows a normal video element with controls, 2:10/2:10 timestamp).
2. Audio collision: when 2 scenes finish together, both narrations speak at once. Must serialize playback.
3. Multi-key Groq pool: user will give 7 more keys (total 8). Assign different scenes to different API keys. Scenarios: more scenes than keys (keys reused for freed/finished slots), key quota hit → failover algorithm to redistribute work. Asked to implement algorithm first, then ask user for the 7 keys, then test.

## Project location
/home/ubuntu/simi_realtime (working copy of user's MVP; zip delivered previously: /home/ubuntu/simi_mvp_realtime_v2.zip)
Public test URL: https://5000-id44t0pddrhv6ykzodltu-cd485766.sg1.manus.computer (port 5000; expose again if server restarted)

## Env/setup (sandbox)
- piper installed in /usr/local/bin, libs in /usr/local/lib, espeak-ng-data symlinked at /usr/share/espeak-ng-data
- .env: LLM_PROVIDER=groq, GROQ keys, DIRECTOR_MODEL=deepseek-ai/deepseek-v4-flash-0731, PLANNER_MODEL=same, TTS_BACKEND=piper, PIPER_MODEL=/home/ubuntu/simi_realtime/en_US-lessac-medium.onnx, PLAN_REVIEW_MODE=off, USE_STREAMING=1, ENABLE_WPS_CALIBRATION=1
- Server: cd /home/ubuntu/simi_realtime && python3 app.py (port 5000). kill via: ps aux | grep '[a]pp.py' | awk '{print $2}' | xargs kill

## Done in v3 so far
- incremental_video_assembler.py — IncrementalVideoAssembler: append-only concat rebuild of work/{job}/stream.mp4 with faststart; scene_offsets.json w/ cumulative starts.
- segment_pipeline.py wired: assembler per job, add_scene() after each render_scene_video_fast; result patched with stream_url=/segments/{job}/stream.mp4; stream_info.json written to job_dir; final_mp4 = assembler.finish() + copy to final.mp4.
- app.py: segment_file route now serves /segments/{job}/stream.mp4 and scene_offsets.json (top-level, in job_dir) plus scene_NNN assets; _run_streaming_video_job copies stream.mp4 to outputs/, adds stream_url to manifest + job store.
- templates/stream_player.html REWRITTEN: single real <video controls> element fed by /segments/{job}/stream.mp4; reloads source when buffered range ends; polls scene_offsets.json every 2s for growth; bufferNote shown on waiting. Play/pause/seek/volume/fullscreen free.

## Remaining TODO
- Implement api_key_pool.py: 8-key pool, per-key TPM usage tracking (Groq TPM limit per key; default free tier 6000 TPM / key per minute-ish — actually Groq free tier is 6000 TPM per minute per key; need GROQ_TPM_LIMIT env). Algorithm: round-robin with estimated-usage ledger + backoff on 429; fallback to next healthy key; keys recover after cooldown. Scenes > 8: pool reuses freed keys (worker picks least-loaded non-quarantined key).
- Wire pool into llm_provider.py (streaming_llm.py + visual_engine.py + director.py use get_llm_client / llm_provider).
- Also per-scene TTS is local (piper) — no API key needed; collisions fixed by single <video> playback (serialized by construction).
- Ask user for 7 API keys (message), then test, package (zip), deliver.

## Test history
- v2 streaming 20s job: ~45s total, 3 scenes, no revision loop, calibration learned ~3.16s/word (Piper fast)
- Batch mode regression passed.
- Server currently KILLED; restart before user testing.

## V3 progress log (Aug 15 03:50)
- incremental_video_assembler.py DONE (unit-tested): stream.mp4 + scene_offsets.json, skip empty, total_seconds prop added.
- stream_player.html REWRITTEN: real <video controls> playing /segments/{job}/stream.mp4, reload on buffer-end, poll offsets 2s.
- segment_pipeline.py wired: assembler per job, add_scene after render, result patched stream_url/stream_total_seconds, final_mp4=assembler.finish().
- app.py: segment_file serves stream.mp4 + scene_offsets.json at /segments/{job}/*; copies stream.mp4 to outputs/; /api/keys route added.
- api_key_pool.py DONE (unit-tested): 8-key pool, least-load selection, minute ledger, minute quarantine, day-limit (TPD) support added via day_limited dict. NOTE: after first edit, quarantine() now accepts optional severity kwarg: quarantine(entry, severity="day") should quarantine for the day. Implemented: quarantine(entry, severity=None|"day").
- streaming_llm.py: _checkout_client wired; 429 failover w/ RateLimitError detection.
- llm_provider.py: pooled_create() helper; director.py + visual_engine.py call it with max_key_failovers=3.
- .env.example updated with GROQ_API_KEYS docs.
- Tests: _test_key_pool.py PASSED, _test_assembler.py PASSED.

## BUGS FOUND IN E2E (being fixed now)
1. user's Groq key: qwen/qwen3.6-27b hit TPD (tokens-per-DAY 200000) — the earlier v2 tests consumed the daily quota in the sandbox! Multi-key fix won't help unless user gives more keys. Job failed; stream.mp4 missing -> FileNotFoundError in shutil.copy2. FIX: (a) pooled_create/quarantine with severity="day" when error mentions "tokens per day"; (b) guard copy in app.py when final_mp4 missing (fail job gracefully).
2. First e2e failure trace: "No files to concat" at finish() -> FIXED (skip empty scenes).
3. NameError assembler not defined — could not reproduce after restart; likely stale process using old code.

## Current sandbox Groq status: qwen model TPD exhausted ~52min (retry-after shown). Director (deepseek/gpt-oss) may also be near daily limit from earlier tests. Multi-key testing needs user's 7 extra keys.

## Test commands
- server: cd /home/ubuntu/simi_realtime && python3 app.py (port 5000, kill via grep app.py pids)
- submit: curl -X POST http://localhost:5000/api/videos -H 'Content-Type: application/json' -d '{"prompt":"...","duration_seconds":20,"mode":"streaming"}'
- log: /tmp/server.log ; expose: port 5000 -> https://5000-id44t0pddrhv6ykzodltu-cd485766.sg1.manus.computer

## TODO remaining
1. Finish api_key_pool quarantine severity="day" + quarantine on day-limit errors (streaming_llm + pooled_create).
2. Guard app.py copy (final_mp4 exists check already exists; add for stream).
3. Re-run e2e (may still fail due to sandbox key daily limit — acceptable; key pool logic already unit-tested).
4. Ask user for 7 more keys (gsk_...) + model they want to use (e.g., openai/gpt-oss-120b has 8000 TPM).
5. Test with 8 keys, package zip (rm _test_*.py from zip), deliver.

## CRITICAL BUG (still unfixed as of 04:10): assembler NameError in segment_pipeline._build_segment
- Reproduced via /home/ubuntu/simi_realtime/_repro_pipeline.py (harness; deletes work/testjob2)
- "Scene 2 live-assembly failed (continuing): name 'assembler' is not defined"
- assembler defined at segment_pipeline.py line 264 (local in run_streaming_pipeline); used at line 168 inside _build_segment (module-level function, closure).
- Closure semantics verified fine in isolation; but failing in module. SUSPECT: run_streaming_pipeline has ANOTHER function with same name (duplicate definition?) earlier in file — Python uses LAST definition; if first def doesn't define assembler and gets imported somewhere... OR _build_segment is defined after run_streaming_pipeline and the name 'assembler' exists as a cell only in the LAST definition — but we saw only one 'def run_streaming_pipeline'.
- ALSO suspicious: error says Scene 2, and earlier server logs showed same for ed64/abcf/bd03/ad8e jobs. scene_002 fails; maybe worker thread timing (threads start BEFORE assembler assignment? NO — pool.map is after line 264).
- NEXT DEBUG STEP: use inspect.getclosurevars(_build_segment) to print free vars, or simpler: change _build_segment to receive assembler as explicit parameter instead of closure capture. That fixes it regardless of root cause.
- ALSO FIX NEEDED: shutil.copy2(final_mp4, legacy_final) at line 368 must be guarded with if final_mp4.exists() (final_mp4 is assembler.stream_path — may not exist if all add_scene failed).
- Absolute-path fix for ffmpeg concat DONE in incremental_video_assembler.py (Path(p).absolute()) — confirmed working manually from /tmp.
- 8 keys configured in .env (gsk_tEFG primary + 6 + gsk_CysS = 8). All validated OK.
- qwen/qwen3.6-27b (planner model) hit TPD (200k/day) in earlier tests; day-limit quarantine implemented.

## STATUS 04:05 — ALL FIXED AND WORKING
- Root cause of NameError FOUND & FIXED: _build_segment (module-level, defined BEFORE run_streaming_pipeline) had NO closure over assembler (co_freevars empty). Passed assembler explicitly as param. Also guarded final_mp4 existence before copy2/atempo.
- Job 7ed79dcdc628 (30s neural network, streaming) COMPLETED: final 30.62s, 3 scenes, WPS 4.5 calibrated, predictions 10.9/8.8/11.3s.
- Stream player verified in browser: real video element with play/pause/seek/volume/fullscreen, scene 1 showing, progress events.
- 8 keys healthy in pool, no quarantines.
- Remaining: test a long job (>8 scenes, e.g. 120s) to prove multi-key parallelism + replay across boundaries, then re-expose public URL, package, deliver.
- Public URL: https://5000-id44t0pddrhv6ykzodltu-cd485766.sg1.manus.computer (server running: nohup python3 app.py > /tmp/server.log, work dir /home/ubuntu/simi_realtime)
- User's 8 keys: gsk_tEFG (primary, in .env GROQ_API_KEY) + 7 in GROQ_API_KEYS incl gsk_CysS at end.

## FINAL TEST EVIDENCE (04:06)
The 120-second multi-scene job (8c2fc7b042eb, topic: how a computer works) completed with 9 scenes, final duration 102.21s, calibration at WPS 3.03 from 12 samples. The public URL serves the stream player with real HTML5 video controls (play/seek/volume/fullscreen) — verified visually with a CPU fetch-decode-execute diagram screenshot. The 30s job (7ed79dcdc628) also completed at 30.62s with calibrated predictions. Key pool shows 8 keys, several with 7700 tokens/min usage, no quarantines. All remaining: package zip (exclude work/outputs/node_modules/__pycache__/.venv) and deliver.

## LIVE-PLAYBACK BUG FIX (phase 17, in progress)
User report: player shows "Scene 1 / 5 · 52.0s rendered" but video stays BLACK even after job completes; downloaded MP4 plays fine. Diagnosis: ffmpeg -y overwrites stream.mp4 in place on every new scene, so the served file is truncated mid-browser-read → corrupted decode state → black video. FIXED: incremental_video_assembler._rebuild now writes to stream.tmp.mp4 and does atomic tmp_path.replace(self._stream_path). Also fixed earlier: NameError (assembler closure) + guarded final copy.
REMAINING WORK: (1) player JS setSource reloads with '?v=Date.now()' — after atomic swap the browser may keep old (corrupted) cached decode; the reload logic exists (checkNewScene reloads only near end). Consider forcing full reload on job_completed or on first checkNewScene when video not loaded yet; simpler: setSource() already called initially and after ended; ensure initial setSource fires BEFORE offsets arrive? setSource() runs immediately at load (line 134) — fine if file exists. (2) Test: run server, start job, open player MID-generation, verify video plays progressively; also open AFTER completion and verify playback. (3) User's browser = their machine; verify same behavior locally. (4) Repackage zip (exclude .env, work, outputs, __pycache__) → /home/ubuntu/simi_mvp_realtime_v3.zip. (5) Re-expose public URL (port 5000, expose tool) and share link with final result.
Server restart needed after edits: kill app.py, cd /home/ubuntu/simi_realtime, nohup python3 app.py > /tmp/server.log 2>&1 &. Test job via POST http://localhost:5000/api/videos {"prompt":"...","duration_seconds":30,"mode":"streaming"}.

## FIX VERIFIED (04:14)
Job 40440c64d6de (refrigerator, 40s) — live during generation: player showed Scene 1/2 · 25.1s rendered, playback playing at 7.55s/40.1s, readyState=4, no errors. After completion: reloaded page, Scene 1/3 · 40.1s, playback playing 1.91s/40.1s, no errors. Both cases verified via console exec.
Remaining: re-expose public URL (port 5000), share with user, then package zip as /home/ubuntu/simi_mvp_realtime_v3.zip (exclude .env work outputs __pycache__) and deliver.
Fixes applied: (a) assembler._rebuild writes stream.tmp.mp4 then atomic replace; (b) player waits for stream.mp4 existence (HEAD poll) before first load; (c) reloads full file whenever offsets grow; (d) fresh setSource on job_completed; (e) error-retry handler (6 attempts).

## PHASE 20-24: ANIMATION + LOGGING + SMOOTH PLAYBACK (current work)
User's 3 new requirements:
1. SMOOTH transitions: video goes black/stops when a new scene is appended. Cause: full src reload on every scene growth. Fix direction: avoid src reset while playing; keep same URL, reload only near end (player already has `video.currentTime >= duration - 1.2` check in older code — check current stream_player.html logic; also consider requestUpdate() on SourceBuffer via MediaSource API, or at minimum re-set src only when playhead beyond buffer). Simplest robust: use MediaSource Extensions (append each new segment.mp4 as a new init+media segment) OR reload only when near end + show loading spinner. Decide after reading current JS.
2. SEMANTIC OBJECT-LEVEL ANIMATION: user wants diagrams/arrows/transforms animated, narration-aligned, NOT LLM-token-heavy. Design: compact typed animation DSL JSON (target id + anim type + normalized time 0-1 + dur), emitted by LLM under strict JSON schema (openai response_format / structured) with few-shot examples in system prompt. Animation types: draw (stroke-dashoffset), fade, move, scale, pulse/highlight, count, pop. Render: CSS animations injected into SVG+CSS then rasterize via chromium screenshot at t? NO — final output is MP4. Best: animate in browser headless with CSS @keyframes over time? Current pipeline: visual_engine creates SVG → chromium screenshot (or python svg rendering) → PNG → ffmpeg PNG+wav → segment.mp4 (static image loop). Options: (a) keep static PNGs but add zoompan/subtle motion via ffmpeg (cheap, low tokens); (b) render ANIMATED scenes: generate keyframes? Simplest high-impact: produce CSS-animated HTML (SVG + keyframes), screenshot at multiple phases? Too complex for capstone deadline? Decision: implement hybrid — LLM emits animation DSL; we render an ANIMATED GIF/MP4 per scene by driving chromium headless with CSS animation timeline via CDP 'Emulation' + screenshots at intervals, then encode. Actually simplest: use the existing chromium screenshot at 3-4 normalized time points and ffmpeg morphs? NO. CHOSEN: keep PNG pipeline, but add 'animate' overlay: ffmpeg overlay with zoompan + fade between PNGs? 
   ALTERNATIVE CHOSEN (better quality): generate per-scene CSS animation HTML page (SVG + injected keyframes), use chromium headless (already used in visual_engine? check which renderer) with CDP or 'chromium --screenshot' can't capture animation; use node/puppeteer-style? chromium alone no puppeteer. Check visual_engine renderer. If visual_engine uses weasyprint or cairosvg (static), then for animated scenes use chromium --headless --screenshot with JS animation paused at target times via --virtual-time-budget! Chromium supports --virtual-time-budget (ms) which freezes JS/CSS animation at that point. PERFECT: for each anim time point t, screenshot with vtb=t*1000 → series of PNGs → encode at 24fps with each frame held per its sub-duration. Tokens: animation DSL output ~200-400 tokens extra per scene (typed JSON only, no prose).
3. RUN LOGGING: logs/runs/<job_id>.jsonl — one record per API call: ts, job_id, stage (director|planner|visual|tts|pool), key_prefix, model, prompt_tokens, completion_tokens, latency_ms, status, retry/failover events; summary.json: totals, cost estimate ($/1M token pricing table), per-stage timing, duration accuracy. Endpoint /api/logs/<job_id>. Wrap call sites in streaming_llm.py (record from response.usage + elapsed), pooled_create (llm_provider.py), tts_engine (record latency, wpm), segment_pipeline (record per-scene wall time). Zero new deps.
Implementation order: logging first (21) since it's low-risk, then animation (20), then playback (22), then E2E test (23), deliver (24).
Server state: port 5000, server running from /home/ubuntu/simi_realtime (nohup python3 app.py > /tmp/server.log &), exposed at https://5000-id44t0pddrhv6ykzodltu-cd485766.sg1.manus.computer. 8 Groq keys in .env (gsk_tEFG primary + 7 in GROQ_API_KEYS incl gsk_CysS). Piper installed at /usr/local/bin/piper with libs. ffmpeg+espeak-ng installed.
Key files: app.py (routes, socketio), segment_pipeline.py (pipeline, _build_segment w/ assembler kwarg), incremental_video_assembler.py (atomic tmp+replace, concat absolute paths), streaming_llm.py (pool checkout + 429 failover day-limit), llm_provider.py (pooled_create), api_key_pool.py, duration_calibrator.py, visual_engine.py (SVG DSL renderer), director.py, fast_render.py, tts_engine.py, templates/stream_player.html.
Packaging: zip from /home/ubuntu/simi_realtime excluding .env work outputs __pycache__ .git; delivered as /home/ubuntu/simi_mvp_realtime_v3.zip last time.

## ANIMATION DESIGN (phase 20) — DECIDED
Pipeline facts: segment_pipeline._build_segment → create_visual_plan → render_visual_plan (raw SVG) → svg_to_png (ffmpeg librsvg, fallback chromium screenshot `_svg_to_png_with_browser`) → render_scene_video_fast (ffmpeg -loop 1 PNG + wav → libx264). Timeline metadata _make_animation_timeline already exists (layer-staggered fade/draw) used only by stream player CSS.
DESIGN: LLM emits per-scene ANIMATION LIST (typed JSON) appended to the existing visual plan prompt ("append an `animations` array, compact typed list, each {t:idx,a:"draw|fade|move|scale|pulse|count",d:sec} or similar ~6 fields max). Cost control: same single LLM call that produces elements; response_format=json_object; instruction says animations list max N entries, terse values, no prose; reasoning disabled.
RENDER: animate IN SERVER-SIDE VIDEO, not browser. Method: instead of -loop 1 single PNG, produce a SHORT ANIMATED GIF-free MP4 per scene by:
  Option A: ffmpeg zoompan/fade overlays on PNG (cheap, works with static png) — gives motion but NOT object-level semantics.
  Option B: animated keyframe PNGs: use chromium headless with CSS animation + --virtual-time-budget? chromium binary lacks full puppeteer JS API via plain --screenshot (no JS control). video_engine already has _svg_to_png_with_browser which calls chromium with JS: check it — it may inject CSS animations via a small JS script and screenshot! If _svg_to_png_with_browser accepts an optional animation script/timestamp, we can screenshot N phases.
  Option C (CHOSEN): hybrid — keep static PNG primary; for scenes WITH animations, build an animated variant using ffmpeg filtergraph driven by keyframe PNGs generated from the SVG WITH injected CSS keyframes rendered via weasyprint? weasyprint doesn't support CSS animation. Use chromium: inject SVG + @keyframes with animation paused at each phase (set style animation: ... paused; transform manually via JS-free CSS custom timing is hard). Simplest robust: per animation phase t, add to the SVG elements inline `opacity`/`stroke-dashoffset` computed values directly in Python (CSS-free), re-render SVG N times with python string interpolation of attribute values (draw: dashoffset from L to 0; fade: opacity 0→1; move: translate; scale; pulse: stroke/outline; count: text swap). N = 12-24 frames/scene at 2fps = 12 frames for 6s scene. Then encode with -framerate 2 loop over frames? Need varying hold per frame (motion blur not needed): concat filter with setpts. Simple: frames of equal duration = ffmpeg -framerate 12 -loop... Actually encode each frame PNG held via concat or use 'fps' with input as image sequence (-framerate 12 -pattern_type glob). For narration sync, frames of equal length is close enough; better: time each animation to its scheduled seconds → compute frame times → use `filter_complex` with individual PNG inputs + setpts + concat. Implementation: frames list [(png, dur), ...]; if all durs equal, single -framerate glob. Else filter_complex concat with setpts offsets.
FRAMES/SCENE BUDGET: 12-16 frames max (2-4 s/scene at 4fps). 4fps for 6s = 24 frames → 24 SVG rasterizations. Chromium rasterize of small SVG is ~0.3s → 24 × 0.3 = 7s extra/scene. Acceptable? Slightly. Use 3-4fps. Alternatively rasterize via ffmpeg librsvg (very fast) with injected inline styles — librsvg doesn't support CSS animation but supports inline style attr= so python-templated SVG works with librsvg! Even faster. USE ffmpeg librsvg for templated SVG (inline attributes), chromium fallback only for icons/fonts? librsvg handles text/fonts OK; icons are already inlined in SVG via render_icon (embedded <g>). So templated SVG via librsvg path.
PLAYER: stream player already shows CSS-animated timelines; no change needed except smooth transition fix (phase 22).

## STATUS @ phase 20 (animation module created)
animated_scene.py CREATED and compiles. Verified ffmpeg approach in /tmp/animtest:
- concat demuxer on PNG list + -framerate option = FAILS ("Option framerate not found")
- WORKING: loop each PNG -> -t frame_dt -> fps=4,ultrafast sub-seg -> concat -c copy -> attach anullsrc audio w/ -map copy+aac = exact duration, fps*total frames. This is what render_animated_segment now does.
animated_scene.py exports: ANIM_SYSTEM_PROMPT (append to VISUAL_PLANNER_PROMPT), parse_animations_from_plan(plan, narration_seconds), build_frame_schedule, render_animated_segment(svg, elements, animations, wav_path, output_mp4, fps=4). Element-index patching done by counting '<rect|circle|ellipse|line|path|polygon|polyline|text' opens.
NEXT STEPS:
(1) Wire into segment_pipeline._build_segment: append ANIM_SYSTEM_PROMPT to visual prompt (only if ANIM_ENABLED); after create_visual_plan, call parse_animations_from_plan(plan, duration); if animations and ANIM_ENABLED, call render_animated_segment instead of svg_to_png+render_scene_video_fast. Env knobs: ANIM_ENABLED=1, MAX_ANIMATIONS_PER_SCENE=16, ANIM_SCENE_FPS=4, ANIM_MAX_FRAMES=96. Add .env.example lines.
(2) PHASE 21 logging: run_log.py — JSONL append per call (stage, model, key_prefix, prompt_tokens, completion_tokens, latency_ms, status, retries); wrap streaming_llm (record response.usage), pooled_create (llm_provider), tts_engine, segment_pipeline stages; summary.json totals + cost estimate ($/1M: gpt-oss 0.2/0.2, qwen3.6 0.29/0.39, llama-70b 0.59/0.79, deepseek-r1 0.75/2.4, llama-3.3 0.59/0.79 — verify current models: openai/gpt-oss-120b, qwen/qwen3.6-27b); GET /api/logs/<job_id>; logs/runs/<job_id>.jsonl.
(3) PHASE 22 smooth playback: in stream_player.html, instead of setSource() on every growth, only reload when video.currentTime >= duration - 1.2 (already in older logic — verify current file) PLUS use MSE append or simpler: keep same src, rely on browser's buffered range extension; the black frame comes from full reload. Best robust: reload only when near end OR video has not started (currentTime < 1).
(4) E2E test: server running port 5000 (nohup python3 app.py > /tmp/server.log &), exposed https://5000-id44t0pddrhv6ykzodltu-cd485766.sg1.manus.computer; 8 Groq keys in .env; piper at /usr/local/bin/piper. Test 40s job, check manifest has animations, verify log file + /api/logs, verify playback smoothness.
(5) Deliver: zip /home/ubuntu/simi_mvp_realtime_v4.zip (exclude .env work outputs __pycache__ .git) + message.
