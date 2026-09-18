"""Issue 4 generalized graphic-art-direction regression checks."""

import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
os.environ["VIDEO_WIDTH"] = "1280"
os.environ["VIDEO_HEIGHT"] = "720"
os.environ["GRAPHICS_STYLE_ENABLED"] = "true"
os.environ["GRAPHICS_ICON_FAMILY"] = "tabler"

from graphics_style import apply_graphic_style  # noqa: E402
import visual_engine  # noqa: E402
from quality_report import write_canvas_evaluation  # noqa: E402


def _plan():
    return {
        "canvas": {"width": 1280, "height": 720},
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "rect", "id": "outer_frame", "layer": 0, "x": 30.0, "y": 30.0,
             "width": 1220.0, "height": 660.0, "rx": 8.0, "fill": "#ffffff",
             "stroke": "#222222", "stroke_width": 3.0, "opacity": 1.0, "rotation": 0.0, "dash": ""},
            {"type": "rect", "id": "key_result", "layer": 2, "x": 410.0, "y": 220.0,
             "width": 460.0, "height": 220.0, "rx": 8.0, "fill": "#e3f2fd",
             "stroke": "#222222", "stroke_width": 2.0, "opacity": 1.0, "rotation": 0.0, "dash": ""},
            {"type": "arrow", "id": "transition", "layer": 1, "x1": 250.0, "y1": 330.0,
             "x2": 390.0, "y2": 330.0, "fill": "none", "stroke": "#222222",
             "stroke_width": 2.0, "opacity": 1.0, "rotation": 0.0, "dash": ""},
            {"type": "text", "id": "title", "layer": 3, "x": 640.0, "y": 150.0,
             "text": "General visual", "font_size": 36.0, "font_weight": "normal",
             "align": "center", "fill": "#111111", "stroke": "none",
             "stroke_width": 0.0, "opacity": 1.0, "rotation": 0.0, "dash": ""},
            {"type": "icon", "id": "object", "layer": 2, "x": 640.0, "y": 330.0,
             "width": 100.0, "height": 100.0, "query": "data server", "fill": "#e3f2fd",
             "stroke": "#222222", "stroke_width": 2.0, "opacity": 1.0, "rotation": 0.0, "dash": ""},
        ],
        "source": "freeform_elements",
    }


def test_style_adapter_preserves_geometry_and_semantic_colours() -> None:
    raw = _plan()
    raw["elements"][1]["fill"] = "#DC2626"  # a deliberate semantic colour
    styled = apply_graphic_style(raw)
    assert styled["elements"][1]["fill"] == "#DC2626"
    for before, after in zip(raw["elements"], styled["elements"]):
        for key in ("x", "y", "x1", "y1", "x2", "y2", "width", "height"):
            if key in before:
                assert after[key] == before[key]
    meta = styled["_graphic_style"]
    assert meta["layout_preserved"] is True
    assert meta["extra_llm_calls"] == 0
    assert meta["environment_borders_removed"] == 1
    assert meta["depth_effects_applied"] >= 1
    assert meta["icon_family"] == "tabler"


def test_renderer_uses_matching_arrowhead_and_non_dot_icon_fallback() -> None:
    styled = apply_graphic_style(_plan())
    real_search = visual_engine.search_iconify
    visual_engine.search_iconify = lambda query, preferred_family=None: None
    try:
        svg, icons = visual_engine.render_visual_plan(styled)
    finally:
        visual_engine.search_iconify = real_search
    assert icons == []
    assert "arrowhead_" in svg and 'fill="#172033"' in svg
    assert "data server" in svg and "<polygon" not in svg
    assert styled["_graphic_style"]["icon_monogram_fallbacks"] == 1
    assert styled["_graphic_style"]["generic_dot_fallbacks"] == 0


def test_icon_query_reduction_is_generic_and_bounded() -> None:
    candidates = visual_engine.icon_query_candidates(
        "machine operator silhouette outline"
    )
    assert "machine operator" in candidates
    assert "machine" in candidates
    assert len(candidates) <= 10


def test_icon_query_candidates_bridge_closed_compound_concepts() -> None:
    candidates = visual_engine.icon_query_candidates("small raindrop illustration")
    assert "drop" in candidates
    assert "water" in candidates
    assert len(candidates) <= 10


def test_ambiguous_everyday_icon_queries_prefer_semantic_aliases() -> None:
    success = visual_engine.icon_query_candidates("success symbol")
    assert success.index("check-circle") < success.index("success")
    assert visual_engine._semantically_matching_icon(["mdi:dishwasher"], "dish", "mdi") is None


def test_icon_reduction_does_not_drop_concrete_object_for_weak_descriptor() -> None:
    candidates = visual_engine.icon_query_candidates("gold coin dollar sign")
    assert "gold" not in candidates
    assert "coin" in candidates
    assert visual_engine._semantically_matching_icon(["mdi:gold"], "coin dollar", "mdi") is None


def test_multiline_text_uses_rendered_lines_for_layout_bounds() -> None:
    plan = visual_engine.normalize_visual_plan({
        "elements": [
            {"type": "rect", "id": "code_box", "x": 60, "y": 110,
             "width": 560, "height": 380},
            {"type": "text", "id": "code_text", "x": 90, "y": 150,
             "font_size": 16,
             "text": "line one with code\nline two with code\nline three with code\nline four"},
        ]
    })
    assert plan["layout"]["scale"] >= 0.9
    assert plan["_legibility"]["minimum_effective_font_size"] >= 14
    svg, _ = visual_engine.render_visual_plan(plan)
    assert svg.count("<tspan") >= 4


def test_filled_composite_parts_do_not_gain_local_outlines_or_shadows() -> None:
    normalized = visual_engine.normalize_visual_plan({"elements": [
        {"id": "whole", "type": "ellipse", "x": 500, "y": 300,
         "rx": 180, "ry": 100, "fill": "#ffffff", "semantic_refs": ["E1"]},
        {"id": "whole_part", "type": "circle", "x": 460, "y": 270,
         "radius": 60, "fill": "#ffffff", "semantic_refs": ["E1"]},
    ]})
    styled = apply_graphic_style(normalized)
    part = next(item for item in styled["elements"] if item["id"] == "whole_part")
    assert part["stroke"] == "none"
    assert "__effect" not in part
    assert part["__composite_part"] is True


def test_renderer_wraps_complete_question_without_ellipsis() -> None:
    text = "Can a single water droplet travel from the ocean to rain while remaining the same molecule?"
    plan = visual_engine.normalize_visual_plan({"elements": [
        {"id": "question", "type": "text", "x": 640, "y": 200,
         "align": "center", "font_size": 32, "text": text},
    ]})
    svg, _ = visual_engine.render_visual_plan(plan)
    assert "molecule?" in svg
    assert "…" not in svg


def test_evaluation_reports_problem_four_pass() -> None:
    with tempfile.TemporaryDirectory(prefix="simi_issue4_") as tmp:
        root = Path(tmp)
        scenes = root / "work" / "job" / "scenes"
        output = root / "outputs" / "job"
        scenes.mkdir(parents=True)
        styled = apply_graphic_style(_plan())
        styled["_graphic_style"]["icons_resolved"] = 1
        (scenes / "scene_001.visual_plan.json").write_text(json.dumps(styled), encoding="utf-8")
        path = write_canvas_evaluation(
            job_id="job", mode="batch", job_dir=root / "work" / "job",
            output_dir=output, final_video=None, request_data={"prompt": "generic"},
        )
        report = json.loads(path.read_text(encoding="utf-8"))
        assert report["report_version"] == "2.1"
        assert report["summary"]["problem_4_pass"] is True
        assert report["summary"]["icon_families_used"] == ["tabler"]


def test_evaluation_rejects_all_icon_fallback_false_pass() -> None:
    with tempfile.TemporaryDirectory(prefix="simi_issue4_fail_") as tmp:
        root = Path(tmp)
        scenes = root / "work" / "job" / "scenes"
        scenes.mkdir(parents=True)
        styled = apply_graphic_style(_plan())
        styled["_graphic_style"].update({
            "icon_query_count": 4, "icons_resolved": 0,
            "icon_monogram_fallbacks": 4,
        })
        (scenes / "scene_001.visual_plan.json").write_text(json.dumps(styled), encoding="utf-8")
        path = write_canvas_evaluation(
            job_id="job", mode="batch", job_dir=root / "work" / "job",
            output_dir=root / "outputs" / "job", final_video=None,
        )
        report = json.loads(path.read_text(encoding="utf-8"))
        assert report["summary"]["problem_4_pass"] is False
        assert report["summary"]["problem_4_status"] == "fail_icon_monogram_fallback_present"
        assert "fail_icon_resolution_below_quality_threshold" in report["summary"]["problem_4_failures"]


def test_evaluation_reports_all_graphics_failures() -> None:
    with tempfile.TemporaryDirectory(prefix="simi_issue4_multi_") as tmp:
        root = Path(tmp)
        scenes = root / "work" / "job" / "scenes"
        scenes.mkdir(parents=True)
        styled = apply_graphic_style(_plan())
        styled["layout"]["scale"] = 0.25
        styled["_graphic_style"].update({
            "icon_query_count": 2, "icons_resolved": 1, "icon_monogram_fallbacks": 1,
        })
        styled["_freeform_repair"] = {
            "applied": True, "composition_preserved": True,
            "unresolved_detached_label_count": 1,
        }
        (scenes / "scene_001.visual_plan.json").write_text(json.dumps(styled), encoding="utf-8")
        path = write_canvas_evaluation(
            job_id="job", mode="batch", job_dir=root / "work" / "job",
            output_dir=root / "outputs" / "job", final_video=None,
        )
        report = json.loads(path.read_text(encoding="utf-8"))
        failures = report["summary"]["problem_4_failures"]
        assert "fail_icon_monogram_fallback_present" in failures
        assert "fail_detached_semantic_labels" in failures
        assert "fail_rendered_text_below_legibility_threshold" in failures


def test_evaluation_rejects_missing_evidence_and_cross_type_collisions() -> None:
    with tempfile.TemporaryDirectory(prefix="simi_semantic_guard_") as tmp:
        root = Path(tmp)
        scenes = root / "work" / "job" / "scenes"
        scenes.mkdir(parents=True)
        styled = apply_graphic_style(_plan())
        styled["_freeform_repair"] = {
            "applied": True, "composition_preserved": True,
            "unresolved_cross_type_collision_count": 2,
        }
        styled["_explanation"] = {"evidence_coverage_pass": False}
        (scenes / "scene_001.visual_plan.json").write_text(json.dumps(styled), encoding="utf-8")
        path = write_canvas_evaluation(
            job_id="job", mode="batch", job_dir=root / "work" / "job",
            output_dir=root / "outputs" / "job", final_video=None,
        )
        report = json.loads(path.read_text(encoding="utf-8"))
        assert report["summary"]["problem_3_status"] == "fail_required_visual_evidence_missing"
        assert "fail_unresolved_cross_type_collisions" in report["summary"]["problem_4_failures"]
        assert report["summary"]["missing_semantic_evidence_scene_ids"] == [1]


def test_every_public_render_flow_applies_art_direction() -> None:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    stream_source = (ROOT / "segment_pipeline.py").read_text(encoding="utf-8")
    assert app_source.count("apply_graphic_style(") >= 3
    assert "visual_plan = apply_graphic_style(visual_plan)" in stream_source
