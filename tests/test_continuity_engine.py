"""General visual-continuity regression checks (no topic-specific fixtures)."""

import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from continuity_engine import (  # noqa: E402
    ContinuityCoordinator,
    continuity_planner_instruction,
    reconcile_visual_plan,
)


def _plan(element_id: str, x: float, text: str = "Stable object"):
    return {
        "canvas": {"width": 1280, "height": 720},
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {
                "type": "rect", "id": element_id, "x": x, "y": 200,
                "width": 220, "height": 120, "fill": "white",
                "stroke": "black", "stroke_width": 3,
            },
            {
                "type": "text", "id": f"{element_id}_label", "x": x + 110,
                "y": 265, "text": text, "font_size": 28,
                "fill": "black", "align": "center",
            },
        ],
    }


def _scene(scene_id: int, mode: str, group: str = "sequence", keep=None):
    return {
        "id": scene_id,
        "visual_state": {"mode": mode, "group": group, "keep": keep or []},
    }


def test_recurring_elements_keep_previous_geometry() -> None:
    previous = _plan("shared_object_box", 100)
    current = _plan("shared_object_box", 800, text="Updated object")
    reconciled = reconcile_visual_plan(
        _scene(2, "continue", keep=["shared object"]), current, 1, previous
    )
    assert reconciled["elements"][0]["x"] == 100
    assert reconciled["elements"][0]["persistent"] is True
    assert reconciled["elements"][1]["text"] == "Updated object"
    assert reconciled["elements"][1]["x"] == 210
    assert reconciled["_continuity"]["reused_element_count"] == 2
    assert reconciled["_continuity"]["applied"] is True


def test_recurring_subject_preserves_identity_colour_across_representation_change() -> None:
    previous = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [{"type": "circle", "id": "traveler", "x": 100, "y": 100,
                      "radius": 20, "fill": "#1d4ed8", "stroke": "#172033"}],
    }
    current = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [{"type": "icon", "id": "traveler", "query": "particle",
                      "x": 700, "y": 300, "width": 50, "height": 50,
                      "fill": "#f59e0b", "stroke": "#111111"}],
    }
    reconciled = reconcile_visual_plan(
        _scene(2, "continue", keep=["traveler"]), current, 1, previous
    )
    assert reconciled["elements"][0]["fill"] == "#1d4ed8"
    assert reconciled["_continuity"]["stable_identity_style_count"] == 1


def test_missing_semantic_object_is_not_injected_into_target_scene() -> None:
    previous = _plan("shared_object_box", 100)
    current = _plan("new_result_box", 700, text="New result")
    reconciled = reconcile_visual_plan(
        _scene(2, "continue", keep=["shared object"]), current, 1, previous
    )
    assert reconciled["_continuity"]["carried_element_count"] == 0
    assert reconciled["_continuity"]["applied"] is False
    assert reconciled["_continuity"]["reason"] == "continuity_rejected_no_complete_keep_objects"
    assert [element["id"] for element in reconciled["elements"]] == ["new_result_box", "new_result_box_label"]


def test_changed_representation_does_not_duplicate_kept_object() -> None:
    previous = _plan("defender_body", 100, text="Defender")
    current = {
        "canvas": {"width": 1280, "height": 720},
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "icon", "id": "defender_comparison", "query": "football player", "x": 700, "y": 300, "width": 100, "height": 100},
            {"type": "text", "id": "label_defender", "text": "Defender", "x": 700, "y": 380, "font_size": 24},
        ],
    }
    reconciled = reconcile_visual_plan(
        _scene(2, "continue", keep=["defender"]), current, 1, previous
    )
    assert reconciled["_continuity"]["carried_element_count"] == 0
    assert reconciled["_continuity"]["duplicate_carries_suppressed"]


def test_support_surface_is_not_carried_without_becoming_an_orphan() -> None:
    previous = {
        "canvas": {"width": 1280, "height": 720},
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "rect", "id": "shared_metric_bg", "x": 400, "y": 250,
             "width": 260, "height": 120},
            {"type": "text", "id": "shared_metric", "text": "Shared metric",
             "x": 530, "y": 320, "font_size": 28},
        ],
    }
    current = {
        "canvas": {"width": 1280, "height": 720},
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "text", "id": "shared_metric", "text": "Shared metric",
             "x": 900, "y": 100, "font_size": 28},
        ],
    }
    reconciled = reconcile_visual_plan(
        _scene(2, "continue", keep=["shared metric"]), current, 1, previous
    )
    assert not any(element.get("id") == "shared_metric_bg" for element in reconciled["elements"])
    assert reconciled["_continuity"]["applied"] is False
    assert reconciled["_continuity"]["reason"] == "continuity_rejected_no_complete_keep_objects"


def test_equivalent_environment_base_suppresses_old_outline() -> None:
    previous = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "rect", "id": "environment_outline", "layer": 2,
             "x": 100, "y": 80, "width": 1080, "height": 560, "fill": "white"},
        ],
    }
    current = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "rect", "id": "environment_base", "layer": 0,
             "x": 100, "y": 80, "width": 1080, "height": 560, "fill": "#e8f1ff"},
            {"type": "icon", "id": "subject", "layer": 2, "query": "subject",
             "x": 640, "y": 360, "width": 80, "height": 80},
        ],
    }
    reconciled = reconcile_visual_plan(
        _scene(2, "continue", keep=["environment outline"]), current, 1, previous
    )
    assert not any(element.get("id") == "environment_outline" for element in reconciled["elements"])
    assert reconciled["_continuity"]["applied"] is False


def test_missing_support_panel_is_not_carried_or_reordered() -> None:
    previous = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "rect", "id": "legend_panel", "layer": 1,
             "x": 50, "y": 50, "width": 200, "height": 100},
        ],
    }
    current = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "circle", "id": "new_subject", "layer": 2,
             "x": 600, "y": 300, "radius": 60},
            {"type": "text", "id": "new_label", "layer": 3,
             "x": 600, "y": 400, "text": "New", "font_size": 24},
        ],
        "animations": [{"e": 0, "a": "pop", "t": 0.1, "d": 0.4}],
    }
    reconciled = reconcile_visual_plan(
        _scene(2, "continue", keep=["legend panel"]), current, 1, previous
    )
    assert [element["id"] for element in reconciled["elements"]] == ["new_subject", "new_label"]
    assert reconciled["animations"][0]["e"] == 0
    assert reconciled["_continuity"]["carried_layer_reorders"] == 0


def test_new_visual_state_does_not_force_reuse() -> None:
    previous = _plan("shared_object_box", 100)
    current = _plan("shared_object_box", 800)
    reconciled = reconcile_visual_plan(_scene(2, "new"), current, 1, previous)
    assert reconciled["elements"][0]["x"] == 800
    assert reconciled["_continuity"]["applied"] is False


def test_parallel_plans_are_committed_in_scene_order() -> None:
    coordinator = ContinuityCoordinator()
    results = {}

    def submit_second():
        results[2] = coordinator.submit(
            _scene(2, "continue", keep=["shared object"]),
            _plan("shared_object_box", 800, text="Updated object"),
        )

    thread = threading.Thread(target=submit_second)
    thread.start()
    time.sleep(0.05)
    results[1] = coordinator.submit(_scene(1, "new"), _plan("shared_object_box", 100))
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert results[2]["_continuity"]["base_scene_id"] == 1
    assert results[2]["elements"][0]["x"] == 100


def test_independent_scene_does_not_wait_for_missing_earlier_scene() -> None:
    coordinator = ContinuityCoordinator()
    started = time.monotonic()
    result = coordinator.submit(
        _scene(3, "new", group="independent_three"),
        _plan("third_object", 700),
    )
    assert time.monotonic() - started < 0.25
    assert result["_continuity"]["applied"] is False
    assert result["_continuity"]["base_scene_id"] is None


def test_instruction_is_compact_and_generic() -> None:
    instruction = continuity_planner_instruction(
        _scene(2, "continue", group="generic_sequence", keep=["primary object"])
    )
    assert "generic_sequence" in instruction
    assert "primary object" in instruction
    assert len(instruction.split()) < 90
    assert continuity_planner_instruction(_scene(1, "new")) == ""


def test_pipeline_integrations_and_diagnostics_are_present() -> None:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    stream_source = (ROOT / "segment_pipeline.py").read_text(encoding="utf-8")
    report_source = (ROOT / "quality_report.py").read_text(encoding="utf-8")
    assert "ContinuityCoordinator()" in app_source
    assert "continuity.submit(scene, visual_plan)" in app_source
    assert "ContinuityCoordinator()" in stream_source
    assert "continuity=continuity" in stream_source
    assert '"problem_2_status"' in report_source
    assert '"reused_element_total"' in report_source


def test_label_prefixes_and_generic_acronyms_match() -> None:
    previous = _plan("right_atrium", 100, text="Right Atrium")
    current = _plan("ra", 800, text="RA")
    reconciled = reconcile_visual_plan(
        _scene(2, "continue", keep=["RA"]), current, 1, previous
    )
    assert reconciled["elements"][0]["x"] == 100

    previous_label = _plan("ra_label", 100, text="RA")
    current_label = _plan("label_ra", 800, text="RA")
    reconciled_label = reconcile_visual_plan(
        _scene(2, "continue", keep=["RA"]), current_label, 1, previous_label
    )
    assert reconciled_label["elements"][0]["x"] == 100


def test_unknown_continuation_group_does_not_merge_adjacent_scene() -> None:
    coordinator = ContinuityCoordinator()
    first = coordinator.submit(_scene(1, "new", group="established"), _plan("shared_object", 100))
    assert first["_continuity"]["applied"] is False
    second = coordinator.submit(
        _scene(2, "continue", group="mistaken_new_group", keep=["shared object"]),
        _plan("shared_object", 800),
    )
    assert second["_continuity"]["applied"] is False
    assert second["_continuity"]["base_scene_id"] is None
    assert second["_continuity"]["reason"] == "continuity_base_unavailable"
    assert second["elements"][0]["x"] == 800


def test_matching_label_without_visual_subject_rejects_continuity() -> None:
    previous = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "text", "id": "stage_label", "text": "Shared stage",
             "x": 100, "y": 100, "font_size": 24},
        ],
    }
    current = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "text", "id": "stage_label", "text": "Shared stage",
             "x": 900, "y": 100, "font_size": 24},
            {"type": "rect", "id": "new_subject", "x": 700, "y": 250,
             "width": 300, "height": 180},
        ],
    }
    result = reconcile_visual_plan(
        _scene(2, "continue", keep=["shared stage"]), current, 1, previous
    )
    assert result["_continuity"]["applied"] is False
    assert result["_continuity"]["reason"] == "continuity_rejected_no_complete_keep_objects"
    assert result["elements"][0]["x"] == 900


def test_generic_layout_roles_never_authorize_visible_reuse() -> None:
    previous = _plan("input_box", 100, text="Input")
    current = _plan("input_panel", 800, text="Input")
    result = reconcile_visual_plan(
        _scene(2, "continue", keep=["Input"]), current, 1, previous
    )
    assert result["_continuity"]["applied"] is False
    assert result["_continuity"]["continuity_gate"]["objects"][0]["generic_role"] is True
    assert result["elements"][0]["x"] == 800


if __name__ == "__main__":
    test_recurring_elements_keep_previous_geometry()
    test_missing_semantic_object_is_not_injected_into_target_scene()
    test_new_visual_state_does_not_force_reuse()
    test_parallel_plans_are_committed_in_scene_order()
    test_instruction_is_compact_and_generic()
    test_pipeline_integrations_and_diagnostics_are_present()
    print("Deterministic scene continuity: PASS")
