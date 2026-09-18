import os
import json
from concurrent.futures import ThreadPoolExecutor

import duration_calibrator
import visual_engine
import quality_report
from quality_report import write_canvas_evaluation
from job_runtime import JobRuntimeConfig, activate_runtime, current_runtime


def _runtime(job_id: str, quality: str, style: str, captions: bool) -> JobRuntimeConfig:
    return JobRuntimeConfig.from_payload(
        job_id,
        {
            "quality_mode": quality,
            "style": style,
            "captions": captions,
            "word_timestamps": quality != "fast",
            "diagrams": quality != "fast",
            "camera": quality != "fast",
            "seed": 101 if quality == "fast" else 202,
        },
        default_planner_model=visual_engine.PLANNER_MODEL,
    )


def test_fast_job_does_not_persist_planner_model(monkeypatch):
    monkeypatch.setenv("GROQ_PLANNER_MODEL", visual_engine.PLANNER_MODEL)
    original = os.environ["GROQ_PLANNER_MODEL"]
    fast = _runtime("fast-job", "fast", "ocean", False)
    balanced = _runtime("balanced-job", "balanced", "forest", True)

    with activate_runtime(fast):
        assert visual_engine._resolve_planner_config().model == fast.planner_model
    with activate_runtime(balanced):
        assert visual_engine._resolve_planner_config().model == visual_engine.PLANNER_MODEL

    assert os.environ["GROQ_PLANNER_MODEL"] == original


def test_parallel_jobs_keep_every_request_setting_isolated():
    fast = _runtime("fast-job", "fast", "ocean", False)
    rich = _runtime("rich-job", "rich", "sunset", True)

    def observe(config):
        with activate_runtime(config):
            active = current_runtime()
            return (
                active.diagnostics()["fingerprint"],
                active.quality_mode,
                active.style,
                active.captions,
                active.word_timestamps,
                active.diagrams,
                active.camera_enabled,
                active.camera_seed,
                visual_engine._resolve_planner_config().model,
                visual_engine._resolve_skin()[0],
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        fast_seen, rich_seen = list(pool.map(observe, (fast, rich)))

    assert fast_seen != rich_seen
    assert fast_seen[1:4] == ("fast", "ocean", False)
    assert rich_seen[1:4] == ("rich", "sunset", True)
    assert current_runtime() is None


def test_clean_diagrams_preserves_visual_preset_and_beginner_audience():
    runtime = JobRuntimeConfig.from_payload(
        "clean-beginner",
        {
            "style": "clean-diagrams",
            "audience": "beginner",
            "diagrams": True,
        },
        default_planner_model=visual_engine.PLANNER_MODEL,
    )

    with activate_runtime(runtime):
        assert current_runtime().style == "clean-diagrams"
        assert current_runtime().audience == "beginner"
        # The preset changes composition guidance while retaining the existing
        # high-contrast default colour skin.
        assert visual_engine._resolve_skin()[0] == "default"


def test_clean_beginner_gate_rejects_unexplained_symbols_and_arrows():
    runtime = JobRuntimeConfig.from_payload(
        "symbol-chain",
        {
            "style": "clean-diagrams",
            "audience": "beginner",
        },
        default_planner_model=visual_engine.PLANNER_MODEL,
    )
    plan = {
        "canvas": {"width": 1280, "height": 720},
        "elements": [
            {"id": "symbol_a", "type": "icon", "query": "atom"},
            {"id": "symbol_b", "type": "icon", "query": "battery"},
            {"id": "symbol_c", "type": "circle", "radius": 40},
            {"id": "arrow_one", "type": "arrow"},
            {"id": "arrow_two", "type": "arrow"},
            {"id": "topic", "type": "text", "text": "Energy"},
        ],
    }

    with activate_runtime(runtime):
        gate = visual_engine.evaluate_pre_render_quality_gate(
            plan, {"semantic_contract": {}}
        )

    assert gate["status"] == "fail"
    assert any(
        issue.startswith("unexplained_symbol_sequence:")
        for issue in gate["blocking_issues"]
    )
    assert any(
        issue.startswith("connector_only_explanation:")
        for issue in gate["blocking_issues"]
    )


def test_clean_beginner_gate_accepts_labelled_explanation():
    runtime = JobRuntimeConfig.from_payload(
        "labelled-scene",
        {
            "style": "clean-diagrams",
            "audience": "beginner",
        },
        default_planner_model=visual_engine.PLANNER_MODEL,
    )
    plan = {
        "canvas": {"width": 1280, "height": 720},
        "elements": [
            {"id": "source", "type": "rect", "width": 180, "height": 100},
            {"id": "change", "type": "circle", "radius": 50},
            {"id": "result", "type": "rect", "width": 180, "height": 100},
            {"id": "arrow_one", "type": "arrow"},
            {"id": "arrow_two", "type": "arrow"},
            {"id": "title", "type": "text", "text": "How energy changes"},
            {"id": "source_label", "type": "text", "text": "Stored energy"},
            {"id": "result_label", "type": "text", "text": "Motion energy"},
        ],
    }

    with activate_runtime(runtime):
        gate = visual_engine.evaluate_pre_render_quality_gate(
            plan, {"semantic_contract": {}}
        )

    assert not any(
        issue.startswith(("unexplained_symbol_sequence:", "connector_only_explanation:"))
        for issue in gate["blocking_issues"]
    )


def test_calibrator_accepts_only_raw_unmodified_plausible_voice(monkeypatch):
    monkeypatch.setattr(duration_calibrator, "ENABLED", True)
    monkeypatch.setattr(duration_calibrator, "_samples", [])
    monkeypatch.setattr(duration_calibrator, "_rejected_samples", 0)

    assert duration_calibrator.record_sample(
        120, 50.0, raw_tts=True, tempo_factor=1.0
    ) is True
    assert duration_calibrator.record_sample(
        120, 25.0, raw_tts=True, tempo_factor=2.0
    ) is False
    assert duration_calibrator.record_sample(
        120, 50.0, raw_tts=False, tempo_factor=1.0
    ) is False
    assert duration_calibrator.samples() == [(120, 50.0)]
    summary = duration_calibrator.calibration_summary()
    assert summary["sample_policy"] == "raw_tts_only"
    assert summary["rejected_samples_this_process"] == 2


def test_evaluation_json_proves_issues_six_seven_and_eight(monkeypatch, tmp_path):
    runtime = _runtime("job-proof", "balanced", "forest", True)
    runtime_meta = runtime.diagnostics()
    job_dir = tmp_path / "work" / "job-proof"
    scene_dir = job_dir / "scenes"
    output_dir = tmp_path / "outputs" / "job-proof"
    scene_dir.mkdir(parents=True)
    (job_dir / "runtime_diagnostics.json").write_text(
        json.dumps(runtime_meta), encoding="utf-8"
    )
    plan = {
        "canvas": {"width": 1280, "height": 720},
        "elements": [{"type": "text", "text": "General", "x": 100, "y": 100, "font_size": 30}],
        "_runtime": {
            "fingerprint": runtime_meta["fingerprint"],
            "isolation_scope": "immutable_job_context",
        },
    }
    (scene_dir / "scene_001.visual_plan.json").write_text(
        json.dumps(plan), encoding="utf-8"
    )
    final = output_dir / "final.mp4"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"diagnostic fixture")
    monkeypatch.setattr(
        quality_report,
        "_dimensions",
        lambda _path: {"width": 1280, "height": 720, "aspect_ratio": round(1280 / 720, 6)},
    )
    monkeypatch.setattr(quality_report, "_media_duration", lambda _path: 60.0)

    path = write_canvas_evaluation(
        job_id="job-proof",
        mode="streaming",
        job_dir=job_dir,
        output_dir=output_dir,
        final_video=final,
        request_data={"target_duration_seconds": 60},
    )
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["summary"]["problem_6_pass"] is True
    assert report["summary"]["problem_7_pass"] is True
    assert report["summary"]["problem_8_pass"] is True
    assert report["duration_diagnostics"]["tempo_adjustment_applied"] is False


def test_evaluation_rejects_exact_wall_clock_filled_with_silence(monkeypatch, tmp_path):
    runtime = _runtime("job-silence", "balanced", "forest", True)
    job_dir = tmp_path / "work" / "job-silence"
    output_dir = tmp_path / "outputs" / "job-silence"
    job_dir.mkdir(parents=True)
    (job_dir / "runtime_diagnostics.json").write_text(
        json.dumps(runtime.diagnostics()), encoding="utf-8"
    )
    (job_dir / "duration_diagnostics.json").write_text(json.dumps({
        "raw_speech_seconds": 55.45,
        "boundary_pause_seconds": 64.55,
        "duration_seconds": 120.0,
    }), encoding="utf-8")
    final = output_dir / "final.mp4"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"fixture")
    monkeypatch.setattr(quality_report, "_dimensions", lambda _path: {
        "width": 1280, "height": 720, "aspect_ratio": round(1280 / 720, 6)
    })
    monkeypatch.setattr(quality_report, "_media_duration", lambda _path: 120.0)
    path = write_canvas_evaluation(
        job_id="job-silence", mode="batch", job_dir=job_dir,
        output_dir=output_dir, final_video=final,
        request_data={"target_duration_seconds": 120},
    )
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["summary"]["duration_target_pass"] is True
    assert report["summary"]["duration_content_pass"] is False
    assert report["summary"]["problem_8_pass"] is False
    assert report["summary"]["problem_8_status"] == "fail_excessive_silence_or_insufficient_spoken_content"
