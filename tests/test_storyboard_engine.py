"""Generic semantic-storyboard composition regression checks."""

import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from storyboard_engine import build_storyboard_scene, sanitize_storyboard  # noqa: E402


def _storyboard(layout="flow"):
    return {
        "title": "A general transformation",
        "layout": layout,
        "items": [
            {"id": "input_state", "label": "Input", "kind": "object", "representation": "icon", "icon": "document"},
            {"id": "process", "label": "Transform", "detail": "Apply the rule", "kind": "concept", "representation": "circle", "emphasis": True},
            {"id": "output_state", "label": "Output", "kind": "value", "representation": "pill"},
        ],
        "relations": [
            {"from": "input_state", "to": "process"},
            {"from": "process", "to": "output_state", "label": "produces"},
        ],
    }


def test_compiler_owns_all_geometry() -> None:
    semantic = _storyboard()
    assert not any(key in item for item in semantic["items"] for key in ("x", "y", "width", "height"))
    plan = build_storyboard_scene(semantic)
    assert plan["source"] == "semantic_storyboard"
    assert plan["_storyboard"]["coordinate_fields_from_llm"] == 0
    assert plan["_storyboard"]["item_count"] == 3
    assert all(element.get("id") for element in plan["elements"])
    assert any(element["type"] == "arrow" for element in plan["elements"])
    assert plan["_storyboard"]["compiler"] == "deterministic_v2"
    assert plan["_storyboard"]["card_only"] is False
    assert plan["_storyboard"]["node_overlap_count"] == 0
    assert {"icon", "circle", "pill"} <= set(plan["_storyboard"]["representation_counts"])


def test_semantic_ids_are_stable_and_duplicate_items_are_removed() -> None:
    raw = _storyboard()
    raw["items"].append({"id": "input-state", "label": "Duplicate"})
    clean = sanitize_storyboard(raw)
    ids = [item["id"] for item in clean["items"]]
    assert ids.count("input_state") == 1
    plan = build_storyboard_scene(clean)
    assert any(element["id"] == "input_state" for element in plan["elements"])
    assert any(element["id"] == "input_state_label" for element in plan["elements"])


def test_every_layout_compiles_inside_a_normalizable_plan() -> None:
    for layout in ("flow", "cycle", "comparison", "timeline", "hierarchy", "grid", "focus"):
        raw = _storyboard(layout)
        if layout == "comparison":
            raw["items"][0]["group"] = "before"
            raw["items"][1]["group"] = "after"
            raw["items"][2]["group"] = "after"
        plan = build_storyboard_scene(raw)
        assert plan["canvas"] == {"width": 1280, "height": 720}
        assert plan["source"] == "semantic_storyboard"
        assert plan["_storyboard"]["layout"] == layout
        assert plan["_storyboard"]["node_overlap_count"] == 0
        assert plan["elements"]


def test_pipeline_and_evaluation_integration() -> None:
    visual_source = (ROOT / "visual_engine.py").read_text(encoding="utf-8")
    report_source = (ROOT / "quality_report.py").read_text(encoding="utf-8")
    assert "build_storyboard_scene" in visual_source
    assert "missing semantic storyboard" in visual_source
    assert '"problem_3_status"' in report_source
    assert '"coordinate_free_storyboard_scene_ids"' in report_source
    assert '"fail_uniform_card_only_video"' in report_source
    assert '"storyboard_representation_totals"' in report_source


def test_connectors_are_not_reused_as_persistent_objects() -> None:
    from continuity_engine import reconcile_visual_plan

    previous = build_storyboard_scene(_storyboard("flow"))
    current = build_storyboard_scene(_storyboard("focus"))
    scene = {
        "id": 2,
        "visual_state": {"mode": "continue", "group": "general", "keep": ["input state"]},
    }
    reconciled = reconcile_visual_plan(scene, current, 1, previous)
    assert not any(
        element.get("persistent") and str(element.get("id", "")).startswith("relation_")
        for element in reconciled["elements"]
    )
