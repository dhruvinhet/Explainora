from pathlib import Path
from xml.etree import ElementTree

import animated_scene
import incremental_video_assembler as assembler_module
import segment_pipeline
import visual_engine
from incremental_video_assembler import IncrementalVideoAssembler
from job_runtime import JobRuntimeConfig


def _segment(tmp_path: Path, scene_id: int) -> Path:
    path = tmp_path / "segments" / f"scene_{scene_id:03d}" / "segment.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake mp4")
    return path


def test_diagram_helper_is_not_shadowed_by_function_local_import():
    assert callable(segment_pipeline.has_diagram)
    assert "has_diagram" not in segment_pipeline._build_segment.__code__.co_varnames


def test_clean_diagrams_keeps_rich_planner_composition():
    plan = {
        "elements": [{"type": "rect"}, {"type": "text", "text": "Label"}],
        "diagram": {
            "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "edges": [{"from": "a", "to": "b"}],
        },
    }
    clean = JobRuntimeConfig.from_payload(
        "clean",
        {"style": "clean-diagrams", "audience": "beginner", "diagrams": True},
        default_planner_model=visual_engine.PLANNER_MODEL,
    )
    blueprint = JobRuntimeConfig.from_payload(
        "blueprint",
        {"style": "ocean", "audience": "developer", "diagrams": True},
        default_planner_model=visual_engine.PLANNER_MODEL,
    )

    assert segment_pipeline._should_use_deterministic_diagram(clean, plan) is False
    assert segment_pipeline._should_use_deterministic_diagram(blueprint, plan) is True


def test_animation_indexer_counts_icon_wrapper_only_once():
    svg = (
        '<svg><g transform="translate(0 0) scale(1)">'
        '<line x1="0" y1="0" x2="10" y2="10"/>'
        '<g transform="translate(2 2)" opacity="1"><path d="M0 0L1 1Z"/></g>'
        '<text x="5" y="5">Label</text>'
        '</g></svg>'
    )

    matches = list(animated_scene._iter_element_matches(svg))

    assert [match.group(1) for _, match in matches] == ["line", "g", "text"]
    assert [index for index, _ in matches] == [0, 1, 2]


def test_icon_animation_preserves_self_closing_svg_shape():
    svg = (
        '<svg><g transform="translate(0 0) scale(1)">'
        '<g transform="translate(2 2)" opacity="1"><path d="M0 0L1 1Z"/></g>'
        '</g></svg>'
    )
    icon_match = list(animated_scene._iter_element_matches(svg))[0][1]

    patched = animated_scene._patch_icon_inner(svg, icon_match, 'opacity="0.5"')

    ElementTree.fromstring(patched)
    assert '<path d="M0 0L1 1Z" opacity="0.5"/>' in patched


def test_animation_indexer_skips_renderer_canvas_background():
    svg = (
        '<svg width="1280" height="720">'
        '<rect width="1280" height="720" fill="white"/>'
        '<g transform="translate(0 0) scale(1)">'
        '<circle cx="10" cy="10" r="5"/><text x="20" y="20">First fact</text>'
        '</g></svg>'
    )
    matches = list(animated_scene._iter_element_matches(svg))
    assert [match.group(1) for _, match in matches] == ["circle", "text"]
    patched = animated_scene._patch_element_in_svg(svg, 0, 'opacity="0"')
    assert '<rect width="1280" height="720" fill="white"/>' in patched
    assert '<circle cx="10" cy="10" r="5" opacity="0"' in patched


def test_dash_animation_replaces_existing_hyphenated_svg_attributes():
    svg = (
        '<svg><g transform="translate(0 0) scale(1)">'
        '<path d="M0 0L10 10" stroke="black" stroke-dasharray="8 6"/>'
        '</g></svg>'
    )
    patched = animated_scene._patch_dash_in_svg(
        svg, 0, 'stroke-dasharray="100" stroke-dashoffset="25"'
    )
    ElementTree.fromstring(patched)
    assert patched.count('stroke-dasharray=') == 1
    assert 'stroke-dasharray="100"' in patched
    assert 'stroke-dashoffset="25"' in patched


def test_animation_schedule_covers_full_audio_when_frame_budget_is_capped(monkeypatch):
    monkeypatch.setenv("ANIM_MAX_FRAMES", "20")
    frames = animated_scene.build_frame_schedule(
        [{"e": 0, "t": 2.0, "d": 0.5}], total_seconds=40.0, fps=8
    )
    assert len(frames) == 20
    assert abs(sum(frame.hold_seconds for frame in frames) - 40.0) < 1e-6
    assert frames[-1].time >= 39.0


def test_beat_timing_uses_the_end_of_each_window_for_late_emphasis():
    animations = [
        {"e": 0, "t": 0.1, "d": 0.4, "a": "fade", "_beat_id": "b1",
         "_beat_start_fraction": 0.0, "_beat_end_fraction": 0.5},
        {"e": 0, "t": 0.1, "d": 0.5, "a": "pulse", "_beat_id": "b1",
         "_beat_start_fraction": 0.0, "_beat_end_fraction": 0.5,
         "_late_emphasis": True},
    ]
    aligned = animated_scene.narrate_align_animations(animations, 20.0)
    assert max(item["t"] for item in aligned) >= 9.0


def test_assembler_buffers_parallel_results_until_scene_order(monkeypatch, tmp_path):
    paths = {scene_id: _segment(tmp_path, scene_id) for scene_id in (1, 2, 3)}
    durations = {str(path): float(scene_id) for scene_id, path in paths.items()}
    monkeypatch.setattr(
        assembler_module,
        "audio_duration_seconds",
        lambda path: durations[str(path)],
    )

    assembler = IncrementalVideoAssembler(tmp_path)
    rebuilds = []
    monkeypatch.setattr(
        assembler,
        "_rebuild",
        lambda scenes: rebuilds.append([p.parent.name for p in scenes]),
    )

    assert assembler.add_scene(paths[3], scene_id=3) == 0
    assert rebuilds == []

    assert assembler.add_scene(paths[1], scene_id=1) == 1
    assert rebuilds[-1] == ["scene_001"]

    assert assembler.add_scene(paths[2], scene_id=2) == 6
    assert rebuilds[-1] == ["scene_001", "scene_002", "scene_003"]
    manifest = assembler.manifest()
    assert [scene["n"] for scene in manifest["scenes"]] == [1, 2, 3]
    assert [scene["start_seconds"] for scene in manifest["scenes"]] == [0, 1, 3]


def test_assembler_uses_explicit_ffprobe_path(monkeypatch, tmp_path):
    called = []
    class Result:
        stdout = "4/1\n"
    monkeypatch.setattr(assembler_module, "FFPROBE_BIN", "C:/tools/ffmpeg/bin/ffprobe.exe")
    monkeypatch.setattr(
        assembler_module.subprocess,
        "run",
        lambda command, **_kwargs: called.append(command) or Result(),
    )
    assert IncrementalVideoAssembler._video_fps(tmp_path / "segment.mp4") == 4
    assert called[0][0] == "C:/tools/ffmpeg/bin/ffprobe.exe"


def test_fps_normalization_uses_unique_output_per_scene(monkeypatch, tmp_path):
    paths = [_segment(tmp_path, scene_id) for scene_id in (1, 2)]
    outputs = []
    def fake_run(command, **_kwargs):
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"normalized")
        outputs.append(output)
    monkeypatch.setattr(assembler_module.subprocess, "run", fake_run)
    normalized = IncrementalVideoAssembler(tmp_path)._normalize_fps(paths, 4.0)
    assert normalized[0] != normalized[1]
    assert [path.name for path in normalized] == [
        "scene_001_segment.mp4", "scene_002_segment.mp4",
    ]


def test_emergency_scene_renderer_finishes_with_local_assets(monkeypatch, tmp_path):
    segment_dir = tmp_path / "segments" / "scene_001"
    segment_dir.mkdir(parents=True)
    (segment_dir / "narration_raw.wav").write_bytes(b"audio")
    (segment_dir / "narration.wav").write_bytes(b"audio")
    monkeypatch.setattr(segment_pipeline, "audio_duration_seconds", lambda _path: 8.0)
    monkeypatch.setattr(
        segment_pipeline,
        "svg_to_png",
        lambda _svg, path: Path(path).write_bytes(b"png"),
    )
    monkeypatch.setattr(
        segment_pipeline,
        "render_scene_video_fast",
        lambda _png, _wav, path, **_kwargs: Path(path).write_bytes(b"mp4"),
    )
    class Assembler:
        def add_scene(self, path, scene_id):
            assert Path(path).is_file() and scene_id == 1
            return 8.0
    result = segment_pipeline._build_emergency_segment(
        "abc123abc123",
        {
            "id": 1,
            "title": "Safe scene",
            "narration": "A clear local fallback keeps the lesson moving.",
            "semantic_contract": {"required_entities": ["input", "result"]},
        },
        tmp_path / "segments",
        Assembler(),
        None,
        TypeError("diagnostic bug"),
    )
    assert result["anim_mode"] == "local_emergency_static"
    assert result["stream_total_seconds"] == 8.0
    assert (segment_dir / "segment.mp4").is_file()


def test_live_player_uses_complete_mp4_fallback():
    template = (Path(__file__).parents[1] / "templates" / "stream_player.html").read_text(
        encoding="utf-8"
    )
    assert "function canUseMSE()" in template
    assert "return false;" in template
    assert "handleJobCompleted" in template
    assert "const finalUrl = new URL(final.href, window.location.href)" in template
    assert "video.src = `${finalUrl.pathname}?v=${Date.now()}`" in template


def test_visual_requests_have_a_startup_safe_timeout():
    source = (Path(__file__).parents[1] / "visual_engine.py").read_text(encoding="utf-8")
    assert "VISUAL_REQUEST_TIMEOUT_SECONDS" in source
    assert '"12"' in source
