# End-to-end test results (Aug 14, 2026)

## Streaming job 1198ae368475 (20s target, binary search)
- Pipeline: planning -> completed. Final duration 18.87s (target 20s) — first pass hit within tolerance, NO revision loop executed (old MVP would have run up to 3 full TTS+LLM revision passes).
- Calibration learned from this run: slope 0.3165 s/word, intercept 0.488s (Piper is much faster than the 145 wpm default — actual ~190 wpm). Predictions 6.8/8.8/7.6s vs actual 5.4/7.0/6.5s — reasonable first-pass accuracy.
- Segments dir per scene: narration.wav, scene.svg, scene.png, segment.mp4, timeline.json — all serving 200. Final mp4 copied to outputs/, manifest.json written.
- Browser verified: streaming player rendered scene 3 SVG correctly, captions panel working, stage/progress live, scene counter shows predicted vs actual.
- Sandbox setup done (not part of delivered zip): piper binary in /usr/local/bin, espeak-ng-data symlinked, server on :5000.

## Remaining checks
- Batch mode still works (regression): POST mode=batch.
- Stream player animation (fade/draw classes) applied — CSS transitions present; draw-in needs getTotalLength support (lines/polylines). Verified SVG renders; animation classes added via timeline.
- TODO: final package = /home/ubuntu/simi_realtime (exclude outputs/, work/, icon_cache is fine to keep) -> zip + migration doc.

Second browser check (18s later): player still on scene 3 (final scene, audio just finished playing), all captions visible, counter accurate. Since job completed in ~25s total (planning ~15s + scenes ~8s), by the time the browser opened, all 3 scenes were ready and it played through the queue sequentially — exactly the designed behavior. Animation classes (svg-el/draw/visible) applied via timeline.json; draw-in + fade confirmed in first screenshot's page markdown/DOM.

The key speed evidence: 20s target video completed in ~25s wall time with ZERO duration revision passes (old MVP needed 60-180s extra for up to 3 revision loops on the same job).
