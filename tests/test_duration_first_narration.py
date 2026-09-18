import wave
from pathlib import Path

import duration_calibrator
import director
from narration_compiler import (
    NarrationDurationError,
    allocate_final_scene_durations,
    assert_duration_preflight,
    compile_duration_first_plan,
)
from tts_engine import pad_wav_to_duration


def test_scene_expansion_batches_are_balanced_without_singleton_tail():
    assert director._balanced_batch_ranges(6, 5) == [(0, 3), (3, 6)]
    assert director._balanced_batch_ranges(15, 5) == [(0, 5), (5, 10), (10, 15)]
    assert director._balanced_batch_ranges(16, 5) == [
        (0, 4), (4, 8), (8, 12), (12, 16)
    ]


def _write_wav(path: Path, seconds: float, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * round(seconds * rate))


def test_voice_profile_isolated_by_voice_and_speed(monkeypatch):
    monkeypatch.setenv("TTS_BACKEND", "piper")
    monkeypatch.setenv("PIPER_MODEL", "voice.onnx")
    monkeypatch.setenv("TTS_VOICE", "en-us")
    monkeypatch.setenv("TTS_SPEED_WPM", "145")
    first = duration_calibrator.voice_profile()["id"]
    monkeypatch.setenv("TTS_SPEED_WPM", "165")
    second = duration_calibrator.voice_profile()["id"]
    assert first != second


def test_voice_profile_isolated_by_piper_length_scale(monkeypatch):
    monkeypatch.setenv("TTS_BACKEND", "piper")
    monkeypatch.setenv("PIPER_MODEL", "voice.onnx")
    monkeypatch.setenv("PIPER_LENGTH_SCALE", "1.00")
    first = duration_calibrator.voice_profile()["id"]
    monkeypatch.setenv("PIPER_LENGTH_SCALE", "1.08")
    second = duration_calibrator.voice_profile()["id"]
    assert first != second


def test_piper_cold_start_budget_matches_real_voice_rate(monkeypatch):
    monkeypatch.setenv("TTS_BACKEND", "piper")
    monkeypatch.setenv("PIPER_MODEL", "voice.onnx")
    monkeypatch.setattr(duration_calibrator, "_detailed_samples", [])
    monkeypatch.setattr(duration_calibrator, "SLOPE_OVERRIDE", None)
    assert duration_calibrator.estimate_words(115.0) >= 330


def test_word_budget_uses_robust_voice_rate_not_unstable_ols(monkeypatch):
    profile = duration_calibrator.voice_profile()
    rows = []
    for words, seconds in [(12, 4.8), (15, 6.0), (20, 8.0), (22, 8.8), (11, 8.0)]:
        rows.append({
            "profile_id": profile["id"], "raw_tts": True,
            "features": {"words": words}, "measured_seconds": seconds,
        })
    monkeypatch.setattr(duration_calibrator, "_detailed_samples", rows)
    monkeypatch.setattr(duration_calibrator, "MIN_SAMPLES", 3)
    # Four normal samples are 0.4 s/word; the slow short-scene outlier must not
    # distort a two-minute narration budget.
    assert duration_calibrator.estimate_words(115.2) == 288


def test_compiler_keeps_required_beats_and_visuals_bound_to_selection(monkeypatch):
    monkeypatch.setattr(duration_calibrator, "_detailed_samples", [])
    plan = {"scenes": [{
        "id": 1, "narration": "legacy", "visual_prompt": "legacy visual",
        "narration_beats": [
            {"beat_id": "core", "required": True, "priority": 5,
             "narration": "Core explanation.", "visual_action": "Show the core relationship."},
            {"beat_id": "extra", "required": False, "priority": 1,
             "narration": "A long optional example with several additional details and comparisons.",
             "visual_action": "Show the optional comparison."},
        ],
    }]}
    compiled = compile_duration_first_plan(plan, 20)
    scene = compiled["scenes"][0]
    assert "core" in scene["selected_beat_ids"]
    for beat in scene["narration_beats"]:
        selected = beat["beat_id"] in scene["selected_beat_ids"]
        assert (beat["narration"] in scene["narration"]) is selected
        assert (beat["visual_action"] in scene["visual_prompt"]) is selected
    assert scene["semantic_contract"]["beat_visual_requirements"] == [
        {"beat_id": beat["beat_id"], "visual_action": beat["visual_action"]}
        for beat in scene["narration_beats"]
        if beat["beat_id"] in scene["selected_beat_ids"]
    ]


def test_scene_targets_sum_exact_requested_duration(monkeypatch):
    monkeypatch.setattr(duration_calibrator, "_detailed_samples", [])
    plan = {"scenes": [
        {"id": i, "narration": f"Required explanation {i}.", "visual_prompt": f"Show concept {i}."}
        for i in range(1, 5)
    ]}
    compiled = compile_duration_first_plan(plan, 60)
    assert abs(sum(scene["target_scene_seconds"] for scene in compiled["scenes"]) - 60) < 0.001
    assert compiled["duration_compiler"]["extra_llm_calls"] == 0


def test_padding_preserves_raw_wav_and_hits_target(tmp_path):
    raw = tmp_path / "raw.wav"
    final = tmp_path / "final.wav"
    _write_wav(raw, 1.0)
    raw_bytes = raw.read_bytes()
    result = pad_wav_to_duration(raw, final, 1.75)
    assert raw.read_bytes() == raw_bytes
    assert abs(result["raw_duration_seconds"] - 1.0) < 0.001
    assert abs(result["final_duration_seconds"] - 1.75) < 0.001
    assert abs(result["boundary_pause_seconds"] - 0.75) < 0.001


def test_global_padding_does_not_accumulate_scene_prediction_errors():
    allocated = allocate_final_scene_durations([12, 8, 9], 30, [10, 10, 10])
    assert abs(sum(allocated) - 30) < 1e-9
    assert all(final >= raw for final, raw in zip(allocated, [12, 8, 9]))
    overshoot = allocate_final_scene_durations([12, 11, 9], 30, [10, 10, 10])
    assert overshoot == [12, 11, 9]


def test_large_missing_duration_is_not_hidden_with_silence(monkeypatch):
    monkeypatch.setenv("MAX_BOUNDARY_PAUSE_RATIO", "0.05")
    monkeypatch.setenv("MAX_SCENE_BOUNDARY_PAUSE_SECONDS", "0.9")
    allocated = allocate_final_scene_durations([5.0] * 9, 120, [120 / 9] * 9)
    assert sum(allocated) <= 51.0001
    assert max(final - 5.0 for final in allocated) <= 0.9001


def test_preflight_rejects_half_length_narration(monkeypatch):
    monkeypatch.setattr(duration_calibrator, "_detailed_samples", [])
    monkeypatch.setenv("NARRATION_MIN_WORD_RATIO", "0.88")
    plan = {"scenes": [{
        "id": 1,
        "narration": "This explanation is intentionally much too short.",
        "visual_prompt": "Show a concise explanation.",
    }]}
    compiled = compile_duration_first_plan(plan, 120)
    assert compiled["duration_preflight"]["status"] == "fail"
    try:
        assert_duration_preflight(compiled)
    except NarrationDurationError as exc:
        assert exc.diagnostics["reason"] == "narration_too_short"
    else:
        raise AssertionError("short narration was not rejected")


def test_batch_duration_path_has_no_revision_loop():
    source = Path("app.py").read_text(encoding="utf-8")
    body = source[source.index("def _fit_narration_duration"):source.index("def _generate_visual_assets")]
    assert "revise_plan_for_duration" not in body
    assert "MAX_SCRIPT_REVISIONS" not in body
    assert "audio_raw" in body and "audio_final" in body
