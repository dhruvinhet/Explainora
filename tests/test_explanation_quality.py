from explanation_quality import (
    assess_visual_anchor_coverage,
    duration_teaching_contract,
    normalize_explanation_plan,
)
from narration_compiler import compile_duration_first_plan
from quality_report import write_canvas_evaluation
import json
from pathlib import Path


def _plan():
    return {
        "learning_goal": "Explain a difficult idea clearly.",
        "scope_summary": "A coherent answer.",
        "audience_profile": {
            "inferred_context": "New operations manager moving into a technical team.",
            "prior_knowledge": "Understands workflows but not implementation jargon.",
            "learning_goal": "Make an informed operational decision.",
            "language_and_tone": "Professional, concrete, and low-jargon.",
            "terminology_policy": "Define implementation terms at first use.",
            "assumptions": ["Knows ordinary business workflows."],
            "terms_to_define": ["queue"],
        },
        "explanation_strategy": {
            "central_question": "Why does this mechanism matter?",
            "core_takeaway": "The mechanism controls when work moves.",
            "mental_model": "A controlled handoff between stages.",
            "narrative_thread": "Follow one request through the workflow.",
            "primary_example": "A delayed customer request.",
            "progression": ["problem", "mechanism", "decision"],
            "excluded_for_capacity": [],
        },
        "scenes": [
            {
                "id": 1,
                "teaching_beat": "Show the starting problem",
                "teaching_role": "motivation and mental_model",
                "learner_takeaway": "The learner sees why a controlled handoff is needed.",
                "builds_on": "",
                "semantic_contract": {
                    "claim": "Uncontrolled handoffs lose work.",
                    "visual_evidence": "Show one request disappearing between two stages.",
                    "before_state": "One request is ready.",
                    "after_state": "The receiving stage never gets it.",
                    "required_entities": ["request", "receiving stage"],
                    "state_facts": ["request id = 42"],
                    "exact_anchors": ["Request 42"],
                },
                "narration_beats": [
                    {
                        "beat_id": "s1",
                        "required": True,
                        "priority": 5,
                        "narration": "Request 42 is ready, but an uncontrolled handoff can lose it between stages.",
                        "visual_action": "Show the request leaving one stage and not reaching the next.",
                        "exact_anchors": ["Request 42"],
                    }
                ],
            }
        ],
    }


def test_duration_contract_scales_without_a_sixty_second_template():
    short = duration_teaching_contract(27, 3, 65)
    long = duration_teaching_contract(407, 29, 980)
    assert short["target_seconds"] == 27
    assert long["target_seconds"] == 407
    assert short["planned_scene_count"] == 3
    assert long["planned_scene_count"] == 29
    assert len(long["depth_opportunities"]) > len(short["depth_opportunities"])
    assert all("60" not in rule for rule in long["rules"])


def test_free_form_audience_is_preserved_not_mapped_to_fixed_labels():
    audience = "museum volunteers who know local history but dislike software jargon"
    plan = normalize_explanation_plan(
        _plan(), audience=audience, target_seconds=73, target_words=175
    )
    assert plan["audience_profile"]["user_description"] == audience
    assert plan["explanation_diagnostics"]["audience_is_free_form"] is True
    assert plan["explanation_diagnostics"]["target_seconds"] == 73
    assert plan["explanation_diagnostics"]["extra_llm_calls"] == 0


def test_exact_anchors_stay_structured_and_out_of_visible_prompt():
    plan = normalize_explanation_plan(
        _plan(), audience="mixed-experience team", target_seconds=20, target_words=14
    )
    compiled = compile_duration_first_plan(plan, 20)
    visual = compiled["scenes"][0]["visual_prompt"]
    assert "copy these literally" not in visual
    assert "Exact visual anchors" not in visual
    assert compiled["scenes"][0]["semantic_contract"]["exact_anchors"] == ["Request 42"]
    assert compiled["scenes"][0]["semantic_contract"]["claim"]


def test_visual_anchor_coverage_detects_changed_literal_values():
    scene = _plan()["scenes"][0]
    passed = assess_visual_anchor_coverage(
        {"elements": [{"type": "text", "text": "Request 42"}]}, scene
    )
    failed = assess_visual_anchor_coverage(
        {"elements": [{"type": "text", "text": "Request 18"}]}, scene
    )
    hidden_id_only = assess_visual_anchor_coverage(
        {"elements": [{"type": "rect", "id": "request_42"}]}, scene
    )
    assert passed["anchor_coverage_pass"] is True
    assert failed["anchor_coverage_pass"] is False
    assert failed["missing_anchors"] == ["Request 42"]
    assert hidden_id_only["anchor_coverage_pass"] is False


def test_required_entities_and_state_facts_detect_missing_or_changed_evidence():
    scene = _plan()["scenes"][0]
    passed = assess_visual_anchor_coverage({
        "elements": [
            {"type": "text", "id": "request_42", "text": "Request ID = 42"},
            {"type": "rect", "id": "receiving_stage"},
        ]
    }, scene)
    failed = assess_visual_anchor_coverage({
        "elements": [
            {"type": "text", "id": "request_18", "text": "Request ID = 18"},
            {"type": "rect", "id": "receiving_stage"},
        ]
    }, scene)
    assert passed["evidence_coverage_pass"] is True
    assert failed["evidence_coverage_pass"] is False
    assert failed["missing_state_facts"] == ["request id = 42"]


def test_state_relationship_delimiter_is_not_required_as_visible_text():
    scene = {
        "id": 1,
        "semantic_contract": {
            "required_entities": ["package"],
            "state_facts": ["Package status after delivery = delivered"],
            "exact_anchors": ["in‑transit"],
        },
    }
    visual = {"elements": [
        {"type": "rect", "id": "package", "semantic_refs": ["E1"]},
        {"type": "text", "id": "status", "text": "Delivered", "semantic_refs": ["F1"]},
        {"type": "text", "id": "prior", "text": "in-transit"},
    ]}

    result = assess_visual_anchor_coverage(visual, scene)
    assert result["missing_state_facts"] == []
    assert result["missing_anchors"] == []


def test_entity_name_in_text_is_not_visual_entity_evidence():
    scene = {
        "id": 1,
        "semantic_contract": {
            "required_entities": ["global current diagram"],
            "state_facts": [],
            "exact_anchors": [],
        },
    }
    visual = {"elements": [
        {"type": "text", "id": "placeholder", "text": "Global Current Diagram", "semantic_refs": ["E1"]},
    ]}
    result = assess_visual_anchor_coverage(visual, scene)
    assert result["missing_required_entities"] == ["global current diagram"]


def test_textual_entity_reference_is_valid_on_visible_text():
    scene = {
        "id": 1,
        "semantic_contract": {
            "required_entities": ["question text", "centripetal label", "Earth"],
            "state_facts": [],
            "exact_anchors": [],
        },
    }
    visual = {"elements": [
        {"type": "text", "id": "question", "text": "Why?", "semantic_refs": ["E1"]},
        {"type": "text", "id": "force_label", "text": "Centripetal", "semantic_refs": ["E2"]},
        {"type": "circle", "id": "earth", "semantic_refs": ["E3"]},
    ]}
    result = assess_visual_anchor_coverage(visual, scene)
    assert result["missing_required_entities"] == []
    assert result["text_semantic_references_present"] == ["E1", "E2"]


def test_textual_entity_reference_is_valid_on_visible_text():
    scene = {
        "id": 1,
        "semantic_contract": {
            "required_entities": ["question text", "centripetal label", "Earth"],
            "state_facts": [],
            "exact_anchors": [],
        },
    }
    visual = {"elements": [
        {"type": "text", "id": "question", "text": "Why?", "semantic_refs": ["E1"]},
        {"type": "text", "id": "force_label", "text": "Centripetal", "semantic_refs": ["E2"]},
        {"type": "circle", "id": "earth", "semantic_refs": ["E3"]},
    ]}
    result = assess_visual_anchor_coverage(visual, scene)
    assert result["missing_required_entities"] == []
    assert result["text_semantic_references_present"] == ["E1", "E2"]


def test_state_reference_cannot_hide_a_changed_numeric_literal():
    scene = {
        "id": 1,
        "semantic_contract": {
            "required_entities": [],
            "state_facts": ["request id = 42"],
            "exact_anchors": [],
        },
    }
    visual = {"elements": [
        {"type": "text", "id": "request", "text": "Request 18", "semantic_refs": ["F1"]},
    ]}
    assert assess_visual_anchor_coverage(visual, scene)["missing_state_facts"] == [
        "request id = 42"
    ]


def test_contract_roles_do_not_treat_camera_actions_or_object_names_as_literal_text():
    plan = _plan()
    scene = plan["scenes"][0]
    scene["semantic_contract"].update({
        "required_entities": ["request", "camera zoom"],
        "exact_anchors": ["request"],
    })
    scene["narration_beats"][0]["visual_action"] = "Show the request, then camera zoom toward it."
    normalized = normalize_explanation_plan(
        plan, audience="operator", target_seconds=20, target_words=14
    )
    contract = normalized["scenes"][0]["semantic_contract"]
    assert "camera zoom" not in contract["required_entities"]
    assert contract["presentation_actions"] == ["camera zoom"]
    assert contract["exact_anchors"] == []


def test_missing_reference_is_not_a_retry_when_visible_evidence_is_unambiguous():
    scene = {
        "id": 3,
        "semantic_contract": {
            "required_entities": ["temperature gradient", "air layers"],
            "state_facts": ["temperature decreases with altitude"],
            "exact_anchors": [],
        },
    }
    visual = {"elements": [
        {"id": "temp_gradient_arrow", "type": "arrow", "semantic_refs": ["E1"]},
        {"id": "warm_layer", "type": "rect"},
        {"id": "cool_layer", "type": "rect"},
        {"id": "warm_label", "type": "text", "text": "Warm Air"},
        {"id": "cool_label", "type": "text", "text": "Cool Air"},
        {"id": "temp_label", "type": "text", "text": "Temp decreases"},
    ]}
    report = assess_visual_anchor_coverage(visual, scene)
    assert report["missing_required_entities"] == []
    assert report["missing_state_facts"] == []
    assert report["state_facts"][0]["evidence_mode"] == "hybrid_lexical_evidence"


def test_visual_constraints_use_hidden_structured_channel():
    root = Path(__file__).parents[1]
    visual_source = (root / "visual_engine.py").read_text(encoding="utf-8")
    compiler_source = (root / "narration_compiler.py").read_text(encoding="utf-8")
    app_source = (root / "app.py").read_text(encoding="utf-8")
    stream_source = (root / "segment_pipeline.py").read_text(encoding="utf-8")
    assert "INTERNAL SEMANTIC CONSTRAINTS" in visual_source
    assert "NEVER DRAW THIS BLOCK" in visual_source
    assert "Exact visual anchors—" not in compiler_source
    assert 'semantic_constraints=scene.get("semantic_contract")' in app_source
    assert 'semantic_constraints=scene.get("semantic_contract")' in stream_source


def test_downloadable_evaluation_contains_explanation_contract(tmp_path, monkeypatch):
    plan = normalize_explanation_plan(
        _plan(), audience="night-shift supervisors", target_seconds=73, target_words=175
    )
    monkeypatch.setattr("quality_report._media_duration", lambda _path: None)
    path = write_canvas_evaluation(
        job_id="explanation-test",
        mode="batch",
        job_dir=tmp_path / "work",
        output_dir=tmp_path / "output",
        final_video=None,
        request_data={"target_duration_seconds": 73},
        plan_data=plan,
    )
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["summary"]["explanation_contract_present"] is True
    assert report["explanation"]["audience_profile"]["user_description"] == "night-shift supervisors"
    assert report["explanation"]["scenes"][0]["semantic_contract"]["claim"]
