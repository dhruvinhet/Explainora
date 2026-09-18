"""Quality-first hybrid planning and conservative repair checks."""

import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from freeform_repair import repair_freeform_plan, repair_post_continuity_plan  # noqa: E402


def _plan():
    return {
        "canvas": {"width": 1280, "height": 720},
        "elements": [
            {"type": "circle", "id": "source", "x": 180, "y": 300, "radius": 70},
            {"type": "rect", "id": "target", "x": 850, "y": 240, "width": 220, "height": 120},
            {"type": "arrow", "id": "motion", "from_id": "source", "to_id": "target", "x1": 0, "y1": 0, "x2": 1, "y2": 1},
            {"type": "text", "id": "caption", "x": 640, "y": 620, "text": "Readable caption", "font_size": 9},
        ],
    }


def test_freeform_composition_is_preserved_and_objective_defects_are_repaired() -> None:
    raw = _plan()
    raw["elements"].append(dict(raw["elements"][-1]))
    repaired = repair_freeform_plan(raw)
    diagnostics = repaired["_freeform_repair"]
    assert repaired["source"] == "freeform_elements"
    assert diagnostics["composition_preserved"] is True
    assert diagnostics["duplicate_elements_removed"] == 1
    assert diagnostics["font_sizes_raised"] == 1
    assert diagnostics["connector_endpoints_repaired"] == 1
    arrow = next(element for element in repaired["elements"] if element["id"] == "motion")
    assert arrow["x1"] > 180
    assert arrow["x2"] < 960


def test_different_layered_artwork_is_not_deduplicated() -> None:
    raw = _plan()
    overlay = dict(raw["elements"][0])
    overlay["id"] = "source_highlight"
    overlay["opacity"] = 0.3
    raw["elements"].append(overlay)
    repaired = repair_freeform_plan(raw)
    assert len(repaired["elements"]) == len(raw["elements"])


def test_background_layer_and_low_contrast_text_are_repaired() -> None:
    raw = {
        "elements": [
            {"type": "rect", "id": "pitch", "x": 0, "y": 0, "width": 1280, "height": 720, "layer": 2, "fill": "#ffffff"},
            {"type": "text", "id": "title", "x": 640, "y": 80, "text": "Visible title", "font_size": 24, "fill": "#ffff00", "layer": 3},
        ]
    }
    repaired = repair_freeform_plan(raw)
    pitch, title = repaired["elements"]
    assert pitch["layer"] == 0
    assert title["fill"] == "#111827"
    assert repaired["_freeform_repair"]["background_layers_repaired"] == 1
    assert repaired["_freeform_repair"]["text_contrast_repaired"] == 1


def test_hybrid_mode_is_wired_as_default_quality_path() -> None:
    visual_source = (ROOT / "visual_engine.py").read_text(encoding="utf-8")
    stream_source = (ROOT / "segment_pipeline.py").read_text(encoding="utf-8")
    report_source = (ROOT / "quality_report.py").read_text(encoding="utf-8")
    # CI and production images intentionally exclude the secret-bearing .env.
    env_source = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert 'VISUAL_PLANNING_MODE", "hybrid"' in visual_source
    assert "RICH_VISUAL_PLANNER_PROMPT" in visual_source
    assert "repair_freeform_plan(raw_plan)" in visual_source
    assert '"VISUAL_PLANNING_MODE", "hybrid"' in stream_source
    assert "VISUAL_PLANNING_MODE=hybrid" in env_source
    assert '"repaired_freeform_scene_ids"' in report_source
    assert '"sparse_foreground_scene_ids"' in report_source
    assert '"continuity_base_unavailable_scene_ids"' in report_source
    director_source = (ROOT / "director.py").read_text(encoding="utf-8")
    assert "group not in known_visual_groups" in director_source
    assert "FOREGROUND_AUTOFRAME_ENABLED" in visual_source


def test_post_continuity_repair_separates_overlapping_general_labels() -> None:
    plan = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "text", "id": "first_label", "x": 500, "y": 350,
             "text": "First descriptive label", "font_size": 24, "align": "center"},
            {"type": "text", "id": "second_label", "x": 570, "y": 350,
             "text": "Second descriptive label", "font_size": 24, "align": "center"},
            {"type": "text", "id": "third_label", "x": 640, "y": 350,
             "text": "Third descriptive label", "font_size": 24, "align": "center"},
        ],
    }
    repaired = repair_post_continuity_plan(plan)
    meta = repaired["_freeform_repair"]
    assert meta["text_overlaps_repaired"] >= 2
    assert meta["unresolved_text_overlap_count"] == 0


def test_post_continuity_repair_removes_unrelated_carried_overlay() -> None:
    plan = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "rect", "id": "old_metric_bg", "x": 400, "y": 250,
             "width": 260, "height": 140, "persistent": True},
            {"type": "rect", "id": "new_table", "x": 350, "y": 220,
             "width": 400, "height": 240},
            {"type": "text", "id": "new_table_label", "x": 500, "y": 300,
             "text": "New table", "font_size": 24},
        ],
    }
    repaired = repair_post_continuity_plan(plan)
    assert not any(element.get("id") == "old_metric_bg" for element in repaired["elements"])
    assert repaired["_freeform_repair"]["orphan_support_overlays_removed"] == 1


def test_post_continuity_repair_removes_duplicate_environment_outline() -> None:
    plan = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "rect", "id": "arena_base", "layer": 0, "x": 80, "y": 80,
             "width": 1120, "height": 560, "fill": "#e8f1ff"},
            {"type": "icon", "id": "main_subject", "layer": 2, "x": 640, "y": 360,
             "width": 100, "height": 100, "query": "subject"},
            {"type": "rect", "id": "arena_outline", "layer": 2, "x": 100, "y": 100,
             "width": 1080, "height": 520, "fill": "white", "persistent": True},
        ],
    }
    repaired = repair_post_continuity_plan(plan)
    assert not any(element.get("id") == "arena_outline" for element in repaired["elements"])
    assert repaired["_freeform_repair"]["duplicate_environment_supports_removed"] == 1
    assert repaired["_freeform_repair"]["foreground_occlusion_count"] == 0


def test_foreground_occlusion_over_icon_is_relayered_safely() -> None:
    plan = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "icon", "id": "earlier_subject", "layer": 2, "x": 640, "y": 360,
             "width": 120, "height": 120, "query": "subject"},
            {"type": "rect", "id": "opaque_overlay", "layer": 2, "x": 300, "y": 180,
             "width": 680, "height": 360, "fill": "white", "opacity": 1},
        ],
    }
    repaired = repair_post_continuity_plan(plan)
    assert repaired["_freeform_repair"]["foreground_occlusion_count"] == 0
    assert repaired["_freeform_repair"]["occluding_layers_lowered"] == 1


def test_post_continuity_repair_removes_visible_layout_scaffolding() -> None:
    plan = {
        "elements": [
            {"id": "real", "type": "text", "text": "Availability", "x": 100, "y": 100},
            {"id": "slot", "type": "text", "text": "Bottom Left", "x": 100, "y": 600},
            {"id": "shape_name", "type": "text", "text": "Triangle", "x": 500, "y": 350},
        ]
    }
    repaired = repair_post_continuity_plan(plan, spoken_context="Availability keeps services usable.")
    assert [element["text"] for element in repaired["elements"]] == ["Availability"]
    assert repaired["_freeform_repair"]["visible_layout_placeholders_removed"] == 2


def test_post_repair_removes_visible_constraint_metadata() -> None:
    plan = {
        "elements": [
            {"id": "real_label", "type": "text", "text": "Actual result", "x": 500, "y": 200},
            {"id": "anchor_text_1", "type": "text", "text": "A | B", "x": 500, "y": 600},
            {"id": "instruction", "type": "text", "text": "Exact visual anchors", "x": 500, "y": 650},
        ]
    }
    repaired = repair_post_continuity_plan(plan, spoken_context="The actual result appears.")
    assert [element["text"] for element in repaired["elements"]] == ["Actual result"]
    assert repaired["_freeform_repair"]["visible_meta_text_removed"] == 2


def test_post_repair_reattaches_named_label_to_its_component() -> None:
    plan = {
        "elements": [
            {"id": "result_box", "type": "rect", "x": 700, "y": 250,
             "width": 220, "height": 120, "layer": 2},
            {"id": "result_label", "type": "text", "text": "Result", "x": 120,
             "y": 80, "font_size": 24, "layer": 3},
        ]
    }
    repaired = repair_post_continuity_plan(plan)
    label = next(element for element in repaired["elements"] if element["id"] == "result_label")
    assert 700 <= label["x"] <= 920
    assert 250 <= label["y"] <= 390
    assert repaired["_freeform_repair"]["detached_labels_repaired"] == 1


def test_post_repair_places_opaque_path_behind_covered_icon() -> None:
    plan = {
        "elements": [
            {"id": "speaker", "type": "icon", "query": "person", "x": 600, "y": 360,
             "width": 160, "height": 160, "layer": 2},
            {"id": "speech_bubble", "type": "path", "d": "M 500 250 L 700 250 L 700 450 L 500 450 Z",
             "fill": "white", "opacity": 1, "layer": 2},
        ]
    }
    repaired = repair_post_continuity_plan(plan)
    by_id = {element["id"]: element for element in repaired["elements"]}
    assert by_id["speech_bubble"]["layer"] < by_id["speaker"]["layer"]
    assert repaired["_freeform_repair"]["occluding_layers_lowered"] == 1
    assert repaired["_freeform_repair"]["foreground_occlusion_count"] == 0


def test_named_multiline_text_is_constrained_to_its_container() -> None:
    plan = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"id": "code_box", "type": "rect", "x": 100, "y": 100,
             "width": 500, "height": 300, "layer": 1},
            {"id": "code_text", "type": "text", "x": 120, "y": 130,
             "font_size": 16, "layer": 2,
             "text": "first long line with several tokens\nsecond line\nthird line"},
        ],
    }
    repaired = repair_post_continuity_plan(plan)
    text = next(element for element in repaired["elements"] if element["id"] == "code_text")
    assert 8 <= text["__max_chars"] <= 120
    assert text["__max_lines"] >= 3
    assert repaired["_freeform_repair"]["container_text_constraints_applied"] == 1


def test_label_directly_below_named_icon_is_not_reported_detached() -> None:
    plan = {
        "elements": [
            {"id": "meal_icon", "type": "icon", "query": "meal", "x": 500,
             "y": 300, "width": 90, "height": 90},
            {"id": "meal_label", "type": "text", "text": "Meal", "x": 500,
             "y": 410, "font_size": 20, "align": "center"},
        ]
    }
    repaired = repair_post_continuity_plan(plan)
    assert repaired["_freeform_repair"]["unresolved_detached_label_count"] == 0
    assert repaired["_freeform_repair"]["detached_labels_repaired"] == 0


def test_label_above_named_component_is_not_reported_detached() -> None:
    plan = {
        "elements": [
            {"id": "window_box", "type": "rect", "x": 400, "y": 250,
             "width": 300, "height": 100},
            {"id": "window_label", "type": "text", "text": "Current Window",
             "x": 550, "y": 220, "font_size": 20, "align": "center"},
        ]
    }
    repaired = repair_post_continuity_plan(plan)
    assert repaired["_freeform_repair"]["unresolved_detached_label_count"] == 0


def test_text_icon_and_text_arrow_collisions_are_repaired_or_reported() -> None:
    plan = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"id": "heading", "type": "text", "text": "Important heading",
             "x": 600, "y": 150, "font_size": 30, "align": "center"},
            {"id": "status_icon", "type": "icon", "query": "check",
             "x": 600, "y": 145, "width": 90, "height": 90},
            {"id": "flow_arrow", "type": "arrow", "x1": 450, "y1": 150,
             "x2": 750, "y2": 150, "stroke_width": 4},
        ],
    }
    repaired = repair_post_continuity_plan(plan)
    diagnostics = repaired["_freeform_repair"]
    assert diagnostics["cross_type_collisions_repaired"] >= 1 or diagnostics["unresolved_cross_type_collision_count"] >= 1
    assert "unresolved_cross_type_collision_ids" in diagnostics
