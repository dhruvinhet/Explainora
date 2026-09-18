from director import (
    _apply_deterministic_factuality_fallback,
    _apply_visual_sequence_contract,
    _authorized_quantities,
    _quantified_tokens,
    _scene_factuality_violations,
)
from visual_engine import (
    beat_visual_timeline_instruction,
    evaluate_pre_render_quality_gate,
    normalize_visual_plan,
)
from explanation_quality import assess_visual_anchor_coverage
from animated_scene import narrate_align_animations, sanitize_animations
from segment_pipeline import _semantic_element_beat_map


def _beat(narration: str, visual_action: str):
    return {
        "beat_id": "b1",
        "required": True,
        "priority": 5,
        "narration": narration,
        "visual_action": visual_action,
        "exact_anchors": [],
    }


def test_closed_world_quantities_reject_model_created_statistic():
    allowed = _authorized_quantities("Explain the process", "")
    plan = {
        "scenes": [{
            "id": 1,
            "narration_beats": [_beat("About 30% returns.", "Show 30% returning")],
            "semantic_contract": {},
        }]
    }
    violations = _scene_factuality_violations(plan, allowed)
    assert violations
    assert any("30%" in row["unsupported"] for row in violations)


def test_closed_world_quantities_allow_user_supplied_value():
    allowed = _authorized_quantities("Explain why the input is 30%", "")
    plan = {
        "scenes": [{
            "id": 1,
            "narration_beats": [_beat("The input is 30%.", "Label the input 30%")],
            "semantic_contract": {"exact_anchors": ["30%"]},
        }]
    }
    assert _scene_factuality_violations(plan, allowed) == []


def test_constructed_unitless_example_values_are_not_treated_as_statistics():
    plan = {
        "scenes": [{
            "id": 1,
            "narration_beats": [_beat(
                "For this example, search the sorted list 5, 12, 23, 38.",
                "Show 5, 12, 23, and 38 in sorted cells",
            )],
            "semantic_contract": {
                "constructed_example": True,
                "example_context": "Hypothetical unitless test data for a worked demonstration.",
                "example_literals": ["5", "12", "23", "38"],
            },
        }]
    }
    assert _scene_factuality_violations(plan, []) == []


def test_constructed_example_cannot_authorize_an_unsourced_percentage():
    plan = {
        "scenes": [{
            "id": 1,
            "narration_beats": [_beat("The system succeeds 30% of the time.", "Show 30%")],
            "semantic_contract": {
                "constructed_example": True,
                "example_context": "A hypothetical example.",
                "example_literals": ["30%"],
            },
        }]
    }
    assert _scene_factuality_violations(plan, [])


def test_constructed_timer_measurements_are_valid_hypothetical_inputs():
    plan = {
        "scenes": [{
            "id": 1,
            "narration_beats": [_beat(
                "In this hypothetical timer, one task takes 10 s and another takes 5 s.",
                "Show sample timers labeled 10 s and 5 s.",
            )],
            "semantic_contract": {
                "constructed_example": True,
                "example_context": "Hypothetical timing used only to demonstrate a comparison.",
                "example_literals": ["10 s", "5 s"],
            },
        }]
    }
    assert _scene_factuality_violations(plan, []) == []


def test_contract_terminology_alias_avoids_false_missing_entity():
    scene = {
        "id": 1,
        "narration": "The Premium plan adds advanced tools.",
        "semantic_contract": {
            "claim": "The Premium plan adds advanced tools.",
            "visual_evidence": "Show the Premium plan feature list.",
            "required_entities": ["paid plan"],
        },
    }
    plan = {"elements": [{
        "type": "text", "id": "premium_plan_title", "text": "Premium Plan",
    }]}
    report = assess_visual_anchor_coverage(plan, scene)
    assert report["missing_required_entities"] == []
    assert report["required_entities"][0]["evidence_mode"] == "director_contract_alias"


def test_local_factuality_fallback_scrubs_spoken_and_visible_magnitudes():
    plan = {
        "scenes": [{
            "id": 6,
            "narration_beats": [_beat(
                "If the startup makes ten thousand API calls, usage stays affordable.",
                'Display "Requests: 10,000" and "Cost: modest amount".',
            )],
            "semantic_contract": {},
        }]
    }

    before = _scene_factuality_violations(plan, [])
    assert any("thousand" in row["unsupported"] for row in before)
    assert any("10,000" in row["unsupported"] for row in before)

    result = _apply_deterministic_factuality_fallback(plan, [])

    assert result["applied"] is True
    assert result.get("extra_llm_calls", 0) == 0
    assert result["remaining"] == []
    combined = plan["scenes"][0]["narration"] + plan["scenes"][0]["visual_prompt"]
    assert "thousand" not in combined
    assert "10,000" not in combined


def test_magnitude_fallback_consumes_of_without_breaking_grammar():
    from director import _replace_rejected_quantity
    rewritten = _replace_rejected_quantity(
        "The cluster contains billions of particles.", "billions"
    )
    assert "of of" not in rewritten
    assert rewritten == "The cluster contains a representative number of particles."
    assert _replace_rejected_quantity(
        "Billions of particles gather.", "Billions"
    ) == "A representative number of particles gather."


def test_quantity_parser_does_not_consume_first_letter_of_next_word_as_unit():
    assert _quantified_tokens("2 middle, 23 lies, 99 moves") == ["2", "23", "99"]


def test_unsourced_bare_numeral_in_spoken_factual_claim_is_rejected():
    plan = {
        "scenes": [{
            "id": 1,
            "narration_beats": [_beat("The device has 7 chambers.", "Show the device")],
            "semantic_contract": {},
        }]
    }
    violations = _scene_factuality_violations(plan, [])
    assert violations
    assert any("7" in row["unsupported"] for row in violations)


def test_bare_numeral_used_only_as_visual_structure_is_allowed():
    plan = {
        "scenes": [{
            "id": 1,
            "narration_beats": [_beat(
                "The decision separates the alternatives.",
                "Show 2 outgoing branches from the decision point.",
            )],
            "semantic_contract": {},
        }]
    }
    assert _scene_factuality_violations(plan, []) == []


def test_complete_beat_aligned_spoken_enumeration_is_structural():
    beats = [
        _beat("1. Inspect the first consideration.", "Show marker 1 beside it."),
        _beat("2. Inspect the next consideration.", "Show marker 2 beside it."),
        _beat("3. Inspect the final consideration.", "Show marker 3 beside it."),
    ]
    for index, beat in enumerate(beats, start=1):
        beat["exact_anchors"] = [str(index)]
    plan = {
        "scenes": [{
            "id": 9,
            "narration_beats": beats,
            "semantic_contract": {
                "claim": "Considerations 1, 2, and 3 organize the explanation.",
                "visual_evidence": "Visible markers 1, 2, and 3 preserve the order.",
                "exact_anchors": ["1", "2", "3"],
            },
        }]
    }
    assert _scene_factuality_violations(plan, []) == []


def test_beat_aligned_percentages_remain_source_locked():
    plan = {
        "scenes": [{
            "id": 1,
            "narration_beats": [
                _beat("1. The rate is 1%.", "Show 1%."),
                _beat("2. The rate becomes 2%.", "Show 2%."),
                _beat("3. The rate becomes 3%.", "Show 3%."),
            ],
            "semantic_contract": {},
        }]
    }
    violations = _scene_factuality_violations(plan, [])
    unsupported = {token for row in violations for token in row["unsupported"]}
    assert {"1%", "2%", "3%"}.issubset(unsupported)


def test_mismatched_spoken_numbers_are_not_misclassified_as_enumeration():
    plan = {
        "scenes": [{
            "id": 1,
            "narration_beats": [
                _beat("The first device has 7 chambers.", "Show the first device."),
                _beat("The next device has 8 chambers.", "Show the next device."),
                _beat("The final device has 9 chambers.", "Show the final device."),
            ],
            "semantic_contract": {},
        }]
    }
    violations = _scene_factuality_violations(plan, [])
    assert {token for row in violations for token in row["unsupported"]} == {"7", "8", "9"}


def test_partial_sequence_does_not_authorize_a_lone_spoken_number():
    plan = {
        "scenes": [{
            "id": 1,
            "narration_beats": [
                _beat("1. Consider the opening idea.", "Show the opening idea."),
                _beat("Now consider the continuation.", "Show the continuation."),
                _beat("Finally connect the ideas.", "Show the connection."),
            ],
            "semantic_contract": {},
        }]
    }
    violations = _scene_factuality_violations(plan, [])
    assert any("1" in row["unsupported"] for row in violations)


def test_visual_gate_allows_structural_index_but_rejects_empirical_quantity():
    scene = {"id": 1, "semantic_contract": {"authorized_quantities": []}}
    base = {
        "canvas": {"width": 1280, "height": 720},
        "elements": [
            {"id": "a", "type": "circle", "x": 260, "y": 300, "radius": 70},
            {"id": "b", "type": "circle", "x": 760, "y": 300, "radius": 70},
            {"id": "link", "type": "arrow", "x1": 340, "y1": 300, "x2": 680, "y2": 300},
            {"id": "index", "type": "text", "x": 500, "y": 180, "text": "2"},
        ],
    }
    structural_gate = evaluate_pre_render_quality_gate(base, scene)
    assert not any(
        issue.startswith("unsupported_visible_quantities:")
        for issue in structural_gate["issues"]
    )

    base["elements"][-1]["text"] = "2 million"
    empirical_gate = evaluate_pre_render_quality_gate(base, scene)
    assert any(
        issue.startswith("unsupported_visible_quantities:")
        for issue in empirical_gate["issues"]
    )


def test_connected_sequence_uses_recurring_semantic_subjects():
    plan = {
        "learning_goal": "Explain a repeating cycle",
        "explanation_strategy": {"mental_model": "a loop", "progression": ["change", "return"]},
        "scenes": [
            {"id": 1, "builds_on": "", "semantic_contract": {"required_entities": ["token", "source"]}, "visual_state": {"keep": ["token"]}},
            {"id": 2, "builds_on": "scene 1", "semantic_contract": {"required_entities": ["token", "middle"]}, "visual_state": {"keep": ["token"]}},
            {"id": 3, "builds_on": "scene 2", "semantic_contract": {"required_entities": ["token", "source"]}, "visual_state": {"keep": ["token"]}},
        ],
    }
    result = _apply_visual_sequence_contract(plan)
    assert result["visual_sequence_blueprint"]["mode"] == "connected"
    assert "token" in result["visual_sequence_blueprint"]["stable_subjects"]
    assert result["scenes"][1]["visual_state"]["group"] == "master_sequence"
    assert result["scenes"][1]["visual_state"]["mode"] == "continue"


def test_pre_render_gate_reports_unsupported_visible_quantity():
    scene = {
        "id": 1,
        "semantic_contract": {
            "required_entities": ["droplet"],
            "state_facts": [],
            "exact_anchors": [],
            "authorized_quantities": [],
        },
    }
    plan = {
        "canvas": {"width": 1280, "height": 720},
        "elements": [
            {"id": "droplet", "type": "circle", "x": 300, "y": 300, "radius": 80},
            {"id": "flow", "type": "arrow", "x1": 390, "y1": 300, "x2": 700, "y2": 300},
            {"id": "destination", "type": "circle", "x": 790, "y": 300, "radius": 80},
            {"id": "bad_stat", "type": "text", "x": 500, "y": 200, "text": "30%"},
        ],
    }
    gate = evaluate_pre_render_quality_gate(plan, scene)
    assert gate["status"] == "fail"
    assert any(issue.startswith("unsupported_visible_quantities:") for issue in gate["issues"])


def test_visible_worked_numbers_must_come_from_scene_contract():
    scene = {
        "id": 1,
        "semantic_contract": {
            "required_entities": ["worked result"],
            "authorized_quantities": ["3"],
        },
    }
    plan = {
        "canvas": {"width": 1280, "height": 720},
        "elements": [
            {"id": "worked_result", "type": "rect", "x": 100, "y": 100,
             "width": 300, "height": 200, "semantic_refs": ["E1"]},
            {"id": "authorized", "type": "text", "x": 150, "y": 160, "text": "Input 3"},
            {"id": "invented", "type": "text", "x": 150, "y": 220, "text": "Loss 0.42"},
        ],
    }
    gate = evaluate_pre_render_quality_gate(plan, scene)
    assert any("0.42" in issue for issue in gate["blocking_issues"])


def test_beat_timeline_scales_with_arbitrary_beat_count():
    scene = {
        "narration_beats": [
            _beat("First idea.", "Show the first state"),
            {**_beat("Second idea.", "Transform to the second state"), "beat_id": "b2"},
            {**_beat("Third idea.", "Reveal the outcome"), "beat_id": "b3"},
        ]
    }
    instruction = beat_visual_timeline_instruction(scene)
    assert '"beat_id":"b1"' in instruction
    assert '"beat_id":"b3"' in instruction
    assert '"end_fraction":1.0' in instruction
    assert '"spoken_narration":"First idea."' in instruction


def test_planner_beat_id_survives_sanitizing_and_controls_timing():
    animations = sanitize_animations(
        [{"t": 0, "e": 0, "a": "fade", "d": 0.4, "beat_id": "b2"}],
        narration_seconds=10,
        n_elements=1,
    )
    animations[0].update(_beat_start_fraction=0.5, _beat_end_fraction=1.0)
    aligned = narrate_align_animations(animations, total_seconds=10)
    assert aligned[0]["_beat_id"] == "b2"
    assert aligned[0]["t"] >= 5


def test_fallback_animation_uses_spoken_semantics_instead_of_element_order():
    beats = [
        _beat("The cache stores the answer.", "Show the cached answer"),
        {**_beat("A new request reaches the cache.", "Move the request to the cache"), "beat_id": "b2"},
    ]
    elements = [
        {"id": "incoming_request", "text": "New request"},
        {"id": "cached_answer", "text": "Cached answer"},
    ]
    mapping = _semantic_element_beat_map(elements, beats)
    assert mapping == {0: 1, 1: 0}


def test_entity_coverage_uses_open_world_semantic_references():
    scene = {
        "id": 1,
        "semantic_contract": {
            "required_entities": ["arbitrary subject", "another arbitrary state"],
            "state_facts": ["arbitrary subject transitions to another arbitrary state"],
            "exact_anchors": [],
        },
    }
    plan = {
        "elements": [
            {"id": "subject", "type": "circle", "semantic_refs": ["E1"]},
            {"id": "state", "type": "circle", "semantic_refs": ["E2"]},
            {"id": "transition", "type": "arrow", "semantic_refs": ["F1"]},
        ]
    }
    report = assess_visual_anchor_coverage(plan, scene)
    assert report["missing_required_entities"] == []
    assert report["missing_state_facts"] == []
    assert report["semantic_binding_mode"] == "semantic_references"


def test_repetition_without_declared_identity_does_not_create_continuity():
    plan = {
        "learning_goal": "Explain a process",
        "explanation_strategy": {"mental_model": "a process", "progression": []},
        "scenes": [
            {"id": 1, "builds_on": "", "semantic_contract": {"required_entities": ["arbitrary subject"]}},
            {"id": 2, "builds_on": "scene 1", "semantic_contract": {"required_entities": ["arbitrary subject"]}},
            {"id": 3, "builds_on": "scene 2", "semantic_contract": {"required_entities": ["arbitrary subject"]}},
        ],
    }
    result = _apply_visual_sequence_contract(plan)
    assert result["visual_sequence_blueprint"]["mode"] == "independent"
    assert result["visual_sequence_blueprint"]["stable_subjects"] == []


def test_normalization_preserves_semantic_references_for_any_visual_vocabulary():
    scene = {
        "id": 1,
        "semantic_contract": {
            "required_entities": ["first unknown concept", "second unknown concept"],
            "state_facts": [],
            "exact_anchors": [],
        },
    }
    plan = {
        "elements": [
            {"id": "left", "type": "rect", "x": 10, "y": 10, "width": 100, "height": 80, "semantic_refs": ["E1"]},
            {"id": "right", "type": "ellipse", "x": 300, "y": 80, "rx": 50, "ry": 40, "semantic_refs": ["E2"]},
            {"id": "link", "type": "arrow", "x1": 110, "y1": 50, "x2": 250, "y2": 70},
        ]
    }
    normalized = normalize_visual_plan(plan)
    report = assess_visual_anchor_coverage(normalized, scene)
    assert report["missing_required_entities"] == []
