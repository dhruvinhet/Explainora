# simi_realtime — Implementation Spec (internal notes for agent)

Working copy: /home/ubuntu/simi_realtime (clean, no venv/node_modules/.git/work, outputs dir removed)
Original: /home/ubuntu/mvp/MVP/simi_mvp
Env: /home/ubuntu/simi_realtime/.env (user keys), system has ffmpeg/ffprobe. Packages installed: flask 3.1, openai 2.52, flask-socketio, gunicorn.

## Existing contracts (do NOT break; add new files, make old app.py still runnable as "batch" mode)

### director.py
- `create_video_plan(prompt, target_seconds, audience)` returns plan dict with: title, learning_goal, scope_summary, must_cover[], out_of_scope[], target_duration_seconds, scenes[] where scene = {id, teaching_beat, narration, visual_prompt}.
- `narration_word_count(plan)`, `desired_scene_count(t)`, `target_word_budget(t)`, `revise_plan_for_duration(plan, actual, target)`.
- DIRECTOR_WPM env default 145; SCENE_SECONDS_TARGET=14; MIN/MAX_SCENES=3/30.
- `_call_json(system, payload, max_tokens=8000)` — non-streaming.

### llm_provider.py
- `get_llm_config(role)` (director/planner) -> LLMConfig(provider, model, api_key, base_url, default_headers)
- `create_llm_client(config)` -> OpenAI client. `fit_completion_token_budget(config, messages, requested)`. `estimate_message_tokens`.
- Providers: nvidia (https://integrate.api.nvidia.com/v1), groq (https://api.groq.com/openai/v1), openrouter (https://openrouter.ai/api/v1, headers HTTP-Referer/X-OpenRouter-Title).

### tts_engine.py
- `resolve_backend()` -> "piper"|"espeak". `synthesize(text, output_wav)` Path. `audio_duration_seconds(path)` float. `adjust_tempo(src, dst, tempo)`.
- Piper cmd: PIPER_BIN --model PIPER_MODEL --output_file OUT (stdin = speech_text+\n, env PYTHONIOENCODING=utf-8:strict PYTHONUTF8=1).
- eSpeak: ESPEAK_BIN -f txtfile -w out.wav -s TTS_SPEED_WPM -v voice. WAV 48k mono.

### visual_engine.py
- `create_visual_plan(text)` -> normalized plan {canvas{width,height}, layout{scale,translate_x,translate_y}, elements[]}. MAX 3 attempts (plan + repair + retry compact), each up to ~12k tokens; PLAN_REVIEW_MODE=auto adds review call.
- `render_visual_plan(plan, sketch=False)` -> (svg_string, used_icons[{"query","icon"}]). Canvas 1024x768, elements: text/rect/circle/ellipse/line/arrow/path/polygon/polyline/icon. Icon query resolved via Iconify (search + fetch, cached in icon_cache/).

### video_engine.py
- `svg_to_png(svg_text, png_path)`, `render_scene_video(png, wav, mp4)` (libx264 preset medium, CRF VIDEO_CRF=20, VIDEO_FPS=30, 1280x720 pad white, AAC 192k), `concat_scene_videos(list, out)` (concat demux, -c copy, +faststart).
- ENV: VIDEO_WIDTH/HEIGHT/FPS/CRF/BROWSER_BIN.

### app.py
- Flask on :5000. POST /api/videos {prompt, duration_seconds(20-1800), audience, sketch} -> 202 {job_id, status_url}. GET /api/jobs/{id} status dict: status, stage, progress, detail, output_url, manifest_url, plan_summary, final_duration_seconds.
- Stages: queued->planning->tts/tts_duration_check/script_revision->visuals->encoding->finalizing->completed|failed.
- Thread per job, in-memory store _jobs. MAX_WORKERS=4 audio, MAX_VISUAL_WORKERS=2.

## Changes to implement

1. NEW `duration_calibrator.py`: fits `seconds = a*words + b` from historical renders (outputs/*/manifest.json scene data). `estimate_seconds(words)` with fallback a=DIRECTOR_WPM/60, b=0. `record_sample(job_dir, words, seconds)`. Auto-loads samples at startup, appends each new render.
2. NEW `streaming_llm.py`: `stream_json(system, payload, max_tokens)` using client.chat.completions.create(stream=True) + parse scene objects as they accumulate (track bracket depth; yield complete scene objects + full text). Reusable by director and visual planner.
3. NEW `segment_pipeline.py`: producer-consumer: Director plan -> SceneTask queue. Consumers: TTS (synthesize) + visual generation run per scene in parallel; scene ready => emit segment event. Buffer size 1-2. Overlap: visuals start while TTS still measuring (skip multi-pass duration fitting; use calibrated estimate + single revision only if needed + atempo as last resort).
4. WebSocket events via flask-socketio in app.py (new route /api/videos/stream; keep old polling endpoints): events: job_stage, plan, scene_progress{scene_id, tts_done, visual_done, duration}, segment_ready{scene_id, duration, svg_url? no — browser requests /segments/{job}/{scene}/svg} , playback_start, job_completed{output_url, manifest_url, plan_summary}, job_failed.
5. NEW `templates/stream_player.html` + client JS: starts playing scene 1 as soon as segment 1 ready; animated SVG timeline (elements fade/draw in with staggered delays via CSS + JS requestAnimationFrame driven by audio time); next scene preloaded. Also final MP4 link when complete (keeps old encoding path with preset=veryfast, fps 15 for static).
6. Cheap encoding: VIDEO_FPS default 15 for batch static scenes (env), preset veryfast (env VIDEO_PRESET=veryfast), keep concat faststart.
7. New env vars: STREAM_BUFFER_SIZE=1, USE_STREAMING_LLM=true, ENABLE_WPS_CALIBRATION=true, USE_ANIMATED_SCENES=true, FINAL_ENCODER_PRESET=veryfast, STATIC_SCENE_FPS=15.
8. index.html: add "Stream mode" option; mode=stream uses streaming pipeline + player; mode=batch uses old pipeline.

## Segment asset layout
WORK_DIR/{job_id}/segments/scene_{N:03d}/: narration.wav (Piper), scene.svg, scene.png, timeline.json, segment.mp4 (batch only).

## Progress state (as of phase 6)
Done: duration_calibrator.py, streaming_llm.py, fast_render.py, segment_pipeline.py created.
Remaining:
1. Update app.py: add Flask-SocketIO, /api/videos endpoint choose mode=batch(default) vs mode=streaming; register /segments/<job>/<path> route; /api/jobs still works (streaming pipeline updates _jobs dict too); keep legacy endpoints intact. Add segment event storage per job for reconnect.
2. Create templates/stream_player.html: socket.io client, queue playback of segments (scene order), Web Audio or <video> element per segment, animated SVG via timeline.json (fade/draw), progress bar, plan panel. Include socket.io JS from CDN.
3. Update index.html minimally: add mode select (batch/stream), stream mode opens/uses stream_player (or embed). Keep old UI intact.
4. Update requirements.txt: add flask-socketio.
5. Update .env.example: new vars (ENABLE_WPS_CALIBRATION, MAX_ATTEMPTS_PER_SCENE, STATIC_SCENE_FPS=15, FINAL_ENCODER_PRESET=veryfast, USE_STREAMING, etc).
6. README.md update section describing both modes + speed improvements.
7. Test: python3 -c import checks, syntax, maybe start server and run /api/videos streaming with eSpeak (no Piper installed — test must use espeak; PIPER_MODEL in .env points to bundled en_US-lessac-medium.onnx in project dir — piper bin NOT installed, so backend resolves to espeak in sandbox). Test with a short 20s prompt.
8. Package: zip /home/ubuntu/simi_realtime without venv/git outputs -> /home/ubuntu/simi_mvp_realtime_v2.zip + migration notes + final message.
Important: tts_engine imports FFMPEG_BIN etc — fast_render imports them from tts_engine (FFMPEG_BIN defined there). Verify import: tts_engine has `FFMPEG_BIN = os.environ.get("FFMPEG_BIN","ffmpeg")` at top — check before testing (it does, lines ~1-30).
Server test: cd /home/ubuntu/simi_realtime; python3 -c "import app" should not crash; run `python3 app.py` in bg, curl POST /api/videos {mode:streaming, prompt:'Explain what is a semaphore', duration_seconds:20, audience:beginner}, then curl /api/jobs/<id> to watch stages, then verify segments dir + final.mp4.
Delivery doc references: laminalabs.ai (pricing/benchmarks), ycombinator.com/launches/REC-simi..., mer.vin article, laminalabs.ai/docs/getting-started.
