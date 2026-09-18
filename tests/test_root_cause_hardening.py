import json
import io
from pathlib import Path

import api_key_pool
import director
import freeform_repair
import llm_provider
import run_log
import visual_engine
import video_engine
import visual_quality
import quantity_policy
import app as app_module
from job_runtime import JobRuntimeConfig, activate_runtime


def _beat(scene_id, beat_id):
    return {
        "beat_id": f"s{scene_id}_{beat_id}", "required": True, "priority": 5,
        "narration": f"Explain semantic unit {scene_id} {beat_id} with enough useful detail.",
        "visual_action": f"Show semantic unit {scene_id} {beat_id}.",
        "exact_anchors": [],
    }


def test_five_minute_plan_is_repartitioned_into_watchable_scenes(monkeypatch):
    monkeypatch.setenv("MAX_STATIC_SCENE_SECONDS", "20")
    plan = {"scenes": [
        {
            "id": scene_id,
            "narration_beats": [_beat(scene_id, beat_id) for beat_id in range(1, 5)],
            "semantic_contract": {"required_entities": [f"subject {scene_id}"]},
            "visual_state": {"mode": "continue", "group": "old", "keep": ["old"]},
        }
        for scene_id in range(1, 5)
    ]}
    expanded = director._ensure_duration_scene_capacity(plan, 300)
    assert len(expanded["scenes"]) == 15
    assert expanded["scene_capacity"]["expanded"] is True
    assert all(scene["visual_state"]["mode"] == "new" for scene in expanded["scenes"])
    assert all(scene["narration"] and scene["visual_prompt"] for scene in expanded["scenes"])


def test_five_minute_director_contract_uses_static_capacity(monkeypatch):
    monkeypatch.setenv("MAX_STATIC_SCENE_SECONDS", "20")
    monkeypatch.delenv("PLANNING_SCENE_SECONDS_TARGET", raising=False)
    assert director.desired_scene_count(300) == 15


def test_director_scene_contract_rejects_collapsed_visual_beats():
    budgets = [{"required_narration_beats": 2}]
    plan = {"scenes": [{"narration_beats": [_beat(1, 1)]}]}
    try:
        director._validate_director_scene_contract(plan, budgets)
    except ValueError as exc:
        assert "paired narration/visual beats" in str(exc)
    else:
        raise AssertionError("collapsed visual coverage was accepted")


def test_director_scene_contract_rejects_word_short_scene():
    budgets = [{
        "scene_id": 1, "required_narration_beats": 2,
        "hard_minimum_words": 40, "hard_maximum_words": 60,
    }]
    plan = {"scenes": [{"narration_beats": [_beat(1, 1), _beat(1, 2)]}]}
    try:
        director._validate_director_scene_contract(plan, budgets)
    except ValueError as exc:
        assert "hard required range is 40-60" in str(exc)
    else:
        raise AssertionError("word-short director scene was accepted")


def test_initial_director_candidate_preserves_repairable_short_scene():
    budgets = [{
        "scene_id": 1, "required_narration_beats": 2,
        "hard_minimum_words": 49, "hard_maximum_words": 59,
    }]
    plan = {"scenes": [{"narration_beats": [_beat(1, 1)]}]}

    # This is an intermediate candidate, not publication acceptance. The strict
    # validator above still rejects it and the duration controller must repair it.
    director._validate_initial_director_candidate(plan, budgets)


def test_five_minute_route_cannot_be_disabled_by_legacy_flag(monkeypatch):
    monkeypatch.setenv("DIRECTOR_HIERARCHICAL_PLANNING", "false")
    monkeypatch.setenv("DIRECTOR_HIERARCHICAL_SCENE_THRESHOLD", "999")
    monkeypatch.delenv("DIRECTOR_FORCE_MONOLITHIC", raising=False)

    assert director._should_use_hierarchical_planning(300, 10) is True


def test_short_request_can_use_monolithic_draft_route(monkeypatch):
    monkeypatch.delenv("DIRECTOR_FORCE_MONOLITHIC", raising=False)
    monkeypatch.setenv("DIRECTOR_HIERARCHICAL_SCENE_THRESHOLD", "12")

    assert director._should_use_hierarchical_planning(60, 5) is False


def test_common_svg_aliases_are_preserved_before_normalization():
    plan = {"elements": [{
        "type": "text", "x": 640, "y": 100, "text": "Title",
        "fontSize": 48, "fontWeight": "bold", "textAnchor": "middle",
        "strokeWidth": 4, "semanticRefs": ["E1"],
    }]}
    result = visual_quality.canonicalize_visual_plan(plan)
    text = result["elements"][0]
    assert text["font_size"] == 48 and text["align"] == "center"
    assert text["stroke_width"] == 4 and text["semantic_refs"] == ["E1"]
    assert not text["id"].startswith("el_")


def test_canonicalizer_preserves_svg_geometry_nested_labels_and_arrow_markers():
    plan = {"elements": [
        {
            "id": "earth", "type": "circle", "cx": 340, "cy": 360, "r": 80,
            "semanticRefs": ["earth"],
            "label": {"text": "Earth", "x": 340, "y": 465,
                      "fontSize": 28, "anchor": "middle"},
        },
        {
            "id": "gravity", "type": "line", "x1": 440, "y1": 340,
            "x2": 560, "y2": 290, "markerEnd": "url(#arrow)",
            "label": {"content": "gravity", "x": 500, "y": 275,
                      "textAnchor": "middle"},
        },
    ]}
    result = visual_quality.canonicalize_visual_plan(plan)
    by_id = {element["id"]: element for element in result["elements"]}
    assert by_id["earth"]["x"] == 340
    assert by_id["earth"]["y"] == 360
    assert by_id["earth"]["radius"] == 80
    assert by_id["gravity"]["type"] == "arrow"
    assert by_id["earth_label"]["text"] == "Earth"
    assert by_id["earth_label"]["align"] == "center"
    assert by_id["earth_label"]["owner_id"] == "earth"
    assert by_id["gravity_label"]["text"] == "gravity"


def test_semantic_fallback_never_displays_visual_instruction():
    prompt = "Show a founder at a desk inside a garage."
    fallback = visual_quality.semantic_fallback_plan(prompt, {
        "required_entities": ["founder", "desk", "garage"],
    })
    visible = " ".join(str(element.get("text", "")) for element in fallback["elements"])
    assert "Show a founder" not in visible
    assert fallback["source"] == "semantic_fallback"
    assert {"E1", "E2", "E3"}.issubset({
        ref for element in fallback["elements"] for ref in element.get("semantic_refs", [])
    })


def test_semantic_fallback_is_an_illustrated_relationship_not_concept_cards():
    fallback = visual_quality.semantic_fallback_plan("Explain a change", {
        "claim": "Heat moves between regions",
        "required_entities": ["warm region", "cold region"],
        "state_facts": ["heat moves from warm to cold"],
    })
    assert fallback["_fallback"]["strategy"] == "illustrated_evidence_rescue"
    assert fallback["_storyboard"]["card_only"] is False
    assert any(element.get("type") == "ellipse" for element in fallback["elements"])
    assert any(
        element.get("type") == "arrow" and "F1" in element.get("semantic_refs", [])
        for element in fallback["elements"]
    )


def test_groq_visual_contract_is_bounded_across_model_versions():
    assert visual_engine.VISUAL_GENERATION_RETRIES >= 3
    assert visual_engine._requires_bounded_visual_contract("groq", "qwen/qwen3.6-27b")
    assert visual_engine._requires_bounded_visual_contract("groq", "qwen/qwen3.8-27b")
    assert visual_engine._requires_bounded_visual_contract("groq", "future/planner-model")
    assert not visual_engine._requires_bounded_visual_contract("cerebras", "qwen/qwen3.8-27b")


def test_illustrated_rescue_handles_unrelated_open_world_contracts():
    contracts = [
        {
            "claim": "Water changes state as energy changes",
            "required_entities": ["ice", "liquid water", "water vapor"],
            "state_facts": ["heating causes melting", "more heating causes evaporation"],
        },
        {
            "claim": "A request passes through several services",
            "required_entities": ["browser", "API", "database"],
            "state_facts": ["browser sends request", "API reads database"],
        },
        {
            "claim": "Supply and demand meet at a market price",
            "required_entities": ["supply", "demand", "market price"],
            "state_facts": ["supply and demand influence price"],
        },
    ]
    for contract in contracts:
        plan = visual_quality.semantic_fallback_plan("unused instruction", contract)
        assert plan["_storyboard"]["card_only"] is False
        assert {"ellipse", "arrow"}.issubset({e.get("type") for e in plan["elements"]})
        svg, _ = visual_engine.render_visual_plan(plan, sketch=False)
        assert svg.startswith("<svg") and "semantic_fallback" not in svg


def test_storyboard_titles_end_at_word_boundaries():
    plan = visual_quality.semantic_fallback_plan("Explain currents", {
        "claim": "The Gulf Stream carries warm water to Western Europe, influencing winter climate conditions.",
        "required_entities": ["Gulf Stream", "Western Europe"],
    })
    assert "influencin" not in plan["storyboard"]["title"]
    assert plan["storyboard"]["title"].endswith("Europe")
    assert not plan["storyboard"]["title"].endswith(" and")


def test_rescue_extra_facts_do_not_create_reverse_overlapping_arrows():
    plan = visual_quality.semantic_fallback_plan("Explain a current", {
        "claim": "A current changes a nearby region",
        "required_entities": ["current", "region", "temperature"],
        "state_facts": ["water moves", "heat transfers", "winter becomes milder"],
    })
    relations = plan["storyboard"]["relations"]
    directed_pairs = {(relation["from"], relation["to"]) for relation in relations}
    assert len(relations) == 2
    assert not any((target, source) in directed_pairs for source, target in directed_pairs)
    assert {"F1", "F2", "F3"}.issubset({
        ref for relation in relations for ref in relation.get("semantic_refs", [])
    })


def test_candidate_registry_keeps_stronger_prior_candidate():
    registry = visual_quality.CandidateRegistry({"required_entities": ["founder"]})
    strong = {"elements": [{"type": "icon", "id": "founder", "semantic_refs": ["E1"]}]}
    weak = {"elements": [{"type": "text", "id": "generic", "text": "Explanation"}],
            "_quality_gate": {"blocking_issues": ["missing_required_entities:founder"]}}
    registry.add("strong", "raw", strong)
    registry.add("weak", "fallback", weak)
    assert registry.best()["candidate_id"] == "strong"


def test_semantic_fallback_cannot_beat_equivalent_rich_candidate_by_one_point():
    contract = {"required_entities": ["customer", "website", "buy button"]}
    registry = visual_quality.CandidateRegistry(contract)
    rich = {"elements": [
        {"type": "icon", "id": "customer", "semantic_refs": ["E1"]},
        {"type": "rect", "id": "website", "semantic_refs": ["E2"]},
        {"type": "rect", "id": "buy_button", "semantic_refs": ["E3"]},
        *[{"type": "rect", "id": f"detail_{index}"} for index in range(6)],
    ], "_quality_gate": {"status": "pass", "blocking_issues": []}}
    fallback = visual_quality.semantic_fallback_plan("Customer buys", contract)
    fallback["_quality_gate"] = {"status": "pass", "blocking_issues": []}
    registry.add("rich", "planner", rich)
    registry.add("fallback", "deterministic_fallback", fallback)
    assert registry.best()["candidate_id"] == "rich"


def test_semantically_incomplete_fallback_cannot_replace_repairable_rich_scene():
    contract = {
        "required_entities": ["feature list", "paid plan"],
        "exact_anchors": ["Layer Masks", "Advanced Filters"],
    }
    registry = visual_quality.CandidateRegistry(contract)
    rich = {
        "elements": [
            {"type": "rect", "id": "feature_list", "semantic_refs": ["E1"]},
            {"type": "rect", "id": "paid_plan", "semantic_refs": ["E2"]},
            {"type": "text", "id": "a", "text": "Layer Masks"},
            {"type": "text", "id": "b", "text": "Advanced Filters"},
        ],
        "_quality_gate": {"status": "fail", "blocking_issues": ["empty_container_count:1"]},
    }
    fallback = {
        "source": "semantic_fallback",
        "elements": [
            {"type": "icon", "id": "feature_list", "semantic_refs": ["E1"]},
            {"type": "icon", "id": "paid_plan", "semantic_refs": ["E2"]},
        ],
        "_quality_gate": {"status": "partial", "blocking_issues": []},
    }
    registry.add("rich", "planner", rich)
    registry.add("fallback", "deterministic", fallback)
    assert registry.best()["candidate_id"] == "rich"


def test_semantic_omission_does_not_replace_drawable_rich_scene_with_rescue():
    contract = {"required_entities": ["engine", "wheel"]}
    registry = visual_quality.CandidateRegistry(contract)
    rich = {
        "elements": [
            {"type": "circle", "id": "engine", "semantic_refs": ["E1"]},
            {"type": "arrow", "id": "motion", "x1": 10, "y1": 10, "x2": 80, "y2": 10},
        ],
        "_quality_gate": {
            "status": "fail",
            "blocking_issues": ["missing_required_entities:wheel"],
        },
    }
    rescue = visual_quality.semantic_fallback_plan("engine drives wheel", contract)
    rescue["_quality_gate"] = {"status": "pass", "blocking_issues": []}
    registry.add("planner_3", "canonicalized_planner", rich)
    registry.add("semantic_fallback", "deterministic_fallback", rescue)
    assert registry.best()["candidate_id"] == "planner_3"


def test_geometry_warning_does_not_replace_drawable_rich_scene_with_rescue():
    contract = {"required_entities": ["subject"]}
    registry = visual_quality.CandidateRegistry(contract)
    rich = {
        "elements": [{"type": "circle", "id": "subject", "semantic_refs": ["E1"]}],
        "_quality_gate": {
            "status": "fail",
            "blocking_issues": ["unresolved_cross_type_collision_count:1"],
        },
    }
    rescue = visual_quality.semantic_fallback_plan("Explain subject", contract)
    rescue["_quality_gate"] = {"status": "pass", "blocking_issues": []}
    registry.add("rich", "post_continuity", rich)
    registry.add("rescue", "active_gate_repair", rescue)
    assert registry.best()["candidate_id"] == "rich"


def test_semantic_fallback_does_not_render_visual_action_as_copy():
    plan = visual_quality.semantic_fallback_plan("Explain a relation", {
        "claim": "Energy moves through the system",
        "required_entities": ["source", "receiver"],
        "beat_visual_requirements": [{
            "beat_id": "b1", "visual_action": "Overlay a flashing equation beside the source",
        }],
    })
    visible = " ".join(str(item.get("text", "")) for item in plan["elements"])
    assert "Overlay" not in visible


def test_shared_entity_reference_allows_label_inside_its_shape():
    elements = [
        {"type": "circle", "id": "planet", "x": 400, "y": 300,
         "radius": 90, "semantic_refs": ["E1"]},
        {"type": "text", "id": "planet_label", "text": "Planet",
         "x": 400, "y": 300, "font_size": 24, "semantic_refs": ["E1"]},
    ]
    assert freeform_repair._cross_type_collision_pairs(elements) == []


def test_toolbar_is_not_misclassified_as_an_empty_content_container():
    elements = [
        {"type": "rect", "id": "toolbar_top", "x": 60, "y": 60,
         "width": 1160, "height": 40, "layer": 1},
        {"type": "rect", "id": "feature_panel", "x": 60, "y": 120,
         "width": 300, "height": 300, "layer": 2},
        {"type": "text", "id": "feature_label", "text": "Feature",
         "x": 210, "y": 250, "font_size": 24, "align": "center"},
    ]
    defects = freeform_repair._composition_defects(elements)
    assert defects["empty_container_ids"] == []


def test_connector_aimed_at_node_center_is_snapped_before_its_label():
    plan = {"elements": [
        {"type": "rect", "id": "source", "x": 260, "y": 620,
         "width": 160, "height": 40},
        {"type": "polygon", "id": "condition",
         "points": "500,620 620,640 500,660 380,640"},
        {"type": "arrow", "id": "route", "x1": 420, "y1": 640,
         "x2": 500, "y2": 640},
        {"type": "text", "id": "condition_text", "text": "High-Res / Fast?",
         "x": 500, "y": 645, "font_size": 18, "align": "center",
         "owner_id": "condition", "label_for": "condition"},
    ]}
    fixed = freeform_repair.repair_post_continuity_plan(plan)
    route = next(item for item in fixed["elements"] if item["id"] == "route")
    condition = next(item for item in fixed["elements"] if item["id"] == "condition")
    condition_left = min(float(value) for value in condition["points"].replace(",", " ").split()[0::2])
    assert route["x2"] <= condition_left + 1
    assert fixed["_freeform_repair"]["unresolved_cross_type_collision_count"] == 0


def test_single_scene_director_object_is_wrapped_without_retry():
    result = {"id": 6, "narration_beats": [{"narration": "Valid", "visual_action": "Show it"}]}
    director._coerce_single_scene_response(result, [6])
    assert [scene["id"] for scene in result["scenes"]] == [6]
    assert result["_response_shape_repair"] == "wrapped_single_scene_object"


def test_post_continuity_rejection_is_diagnostic_not_render_failure():
    scene = {"semantic_contract": {}}
    plan = {
        "canvas": {"width": 1280, "height": 720},
        "elements": [
            {"type": "rect", "id": "subject", "x": 100, "y": 100,
             "width": 300, "height": 200, "layer": 2},
            {"type": "text", "id": "subject_label", "text": "Subject",
             "x": 250, "y": 210, "font_size": 24, "layer": 3},
        ],
        "_continuity": {"reason": "continuity_rejected_no_complete_keep_objects"},
    }
    result = visual_engine.evaluate_pre_render_quality_gate(plan, scene)
    assert any(issue.startswith("continuity_incompatible:") for issue in result["issues"])
    assert not any(issue.startswith("continuity_incompatible:") for issue in result["blocking_issues"])


def test_connector_quality_count_is_strictly_boolean_for_unlinked_paths():
    runtime = JobRuntimeConfig.from_payload(
        "connector-check",
        {"style": "clean-diagrams", "audience": "beginner"},
        default_planner_model=visual_engine.PLANNER_MODEL,
    )
    plan = {"elements": [
        {"type": "path", "id": "orbit", "d": "M 10 10 L 40 40"},
        {"type": "circle", "id": "earth", "x": 100, "y": 100, "radius": 40},
        {"type": "text", "id": "earth_label", "text": "Earth", "x": 100, "y": 100},
    ]}
    with activate_runtime(runtime):
        result = visual_engine.evaluate_pre_render_quality_gate(plan, {"semantic_contract": {}})
    assert result["status"] in {"pass", "partial", "fail"}


def test_quality_diagnostic_exception_is_non_blocking(monkeypatch):
    monkeypatch.setattr(
        visual_engine,
        "_semantic_visual_quality_issues",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError("diagnostic bug")),
    )
    result = visual_engine.evaluate_pre_render_quality_gate(
        {"elements": [{"type": "circle", "id": "subject"}]},
        {"semantic_contract": {}},
    )
    assert result["status"] == "partial"
    assert result["blocking_issues"] == []
    assert result["issues"] == ["quality_diagnostic_error:TypeError"]


def test_freeform_repair_enforces_publication_font_floor():
    repaired = freeform_repair.repair_freeform_plan({"elements": [{
        "type": "text", "id": "label", "text": "Readable",
        "x": 100, "y": 100, "font_size": 14,
    }]})
    assert repaired["elements"][0]["font_size"] >= 18


def test_canvas_background_does_not_shrink_foreground_or_fonts():
    normalized = visual_engine.normalize_visual_plan({"elements": [
        {"type": "rect", "id": "background", "x": 0, "y": 0,
         "width": 1280, "height": 720, "layer": 0, "fill": "white"},
        {"type": "rect", "id": "subject", "x": 200, "y": 180,
         "width": 400, "height": 260, "layer": 2},
        {"type": "text", "id": "subject_label", "text": "Subject",
         "x": 400, "y": 330, "font_size": 18, "layer": 3},
    ]})
    assert normalized["layout"]["scale"] == 1.0
    assert normalized["_legibility"]["text_below_minimum_ids"] == []


def test_effective_font_floor_survives_large_plan_scaling():
    normalized = visual_engine.normalize_visual_plan({"elements": [
        {"type": "rect", "id": "wide_subject", "x": -100, "y": 40,
         "width": 1600, "height": 500, "layer": 2},
        {"type": "text", "id": "label", "text": "Readable",
         "x": 640, "y": 300, "font_size": 18, "layer": 3},
    ]})
    assert normalized["layout"]["scale"] < 1.0
    assert normalized["_legibility"]["text_below_minimum_ids"] == []


def test_populated_overlay_container_is_not_foreground_occlusion():
    plan = {"elements": [
        {"type": "rect", "id": "context", "x": 100, "y": 100,
         "width": 700, "height": 400, "layer": 1, "fill": "white"},
        {"type": "rect", "id": "modal", "x": 200, "y": 150,
         "width": 520, "height": 300, "layer": 2, "fill": "white"},
        {"type": "text", "id": "modal_title", "text": "Return request",
         "x": 460, "y": 220, "font_size": 24, "layer": 3},
        {"type": "rect", "id": "modal_field", "x": 260, "y": 260,
         "width": 400, "height": 60, "layer": 3, "fill": "#eeeeee"},
    ]}
    repaired = freeform_repair.repair_post_continuity_plan(plan)
    assert repaired["_freeform_repair"]["foreground_occlusion_count"] == 0


def test_hard_gate_pass_outranks_higher_polish_failed_candidate():
    registry = visual_quality.CandidateRegistry({})
    rich_but_failed = {
        "elements": [{"type": "rect", "id": f"detail_{index}"} for index in range(20)],
        "_quality_gate": {"status": "fail", "blocking_issues": ["true_overlap:a:b"]},
    }
    safe = {
        "elements": [{"type": "text", "id": "safe", "text": "Safe"}],
        "_quality_gate": {"status": "pass", "blocking_issues": []},
    }
    registry.add("rich_failed", "planner", rich_but_failed)
    registry.add("safe", "fallback", safe)
    assert registry.best()["candidate_id"] == "safe"


def test_spoken_percentage_is_source_locked():
    tokens = quantity_policy.quantified_tokens("often close to forty percent")
    assert any("forty percent" in token.lower() for token in tokens)


def test_rejected_date_rewrites_grammatical_phrase_not_several():
    rewritten = director._replace_rejected_quantity(
        "In 1994, the organization began operating.", "1994"
    )
    assert rewritten == "historically, the organization began operating."
    assert "several" not in rewritten


def test_long_director_plan_expands_bounded_batches_with_shared_contract(monkeypatch):
    budgets = [
        {"scene_id": index, "target_spoken_words": 30,
         "hard_minimum_words": 27, "hard_maximum_words": 33,
         "required_narration_beats": 2, "target_words_per_beat": 15}
        for index in range(1, 13)
    ]
    scene_schema = {
        "semantic_contract": {"required_entities": ["visible evidence"]},
        "visual_state": {"mode": "new", "group": "group", "keep": []},
    }
    payload = {
        "user_request": "Explain a general process", "audience": "general",
        "target_duration_seconds": 240, "narration_word_budget": {},
        "duration_calibration": {}, "desired_scene_count": 12,
        "duration_teaching_contract": {}, "instructions": ["rule"] * 14,
        "required_schema": {
            "audience_profile": {}, "explanation_strategy": {},
            "scenes": [scene_schema],
        },
    }
    calls = []

    def fake_call(system, user_payload, **kwargs):
        calls.append((kwargs["stage"], user_payload))
        if kwargs["stage"] == "director_strategy":
            result = {
                "title": "Process", "learning_goal": "Understand it",
                "audience_profile": {}, "explanation_strategy": {},
                "scope_summary": "Process", "must_cover": [], "out_of_scope": [],
                "fact_ledger": [{"claim": "qualitative", "status": "qualitative"}],
                "terminology_ledger": [{"term": "step", "meaning": "stage"}],
                "scene_briefs": [
                    {"id": index, "teaching_beat": f"Beat {index}",
                     "semantic_contract": scene_schema["semantic_contract"],
                     "visual_state": scene_schema["visual_state"]}
                    for index in range(1, 13)
                ],
            }
        else:
            result = {"scenes": [
                {
                    "id": brief["id"], "teaching_beat": brief["teaching_beat"],
                    "semantic_contract": brief["semantic_contract"],
                    "visual_state": brief["visual_state"],
                    "narration_beats": [
                        {
                            **_beat(brief["id"], beat_id),
                            "narration": (
                                f"This semantic unit {brief['id']} {beat_id} explains the "
                                "mechanism with clear intuition and a concrete useful consequence."
                            ),
                        }
                        for beat_id in (1, 2)
                    ],
                }
                for brief in user_payload["scene_briefs"]
            ]}
        validator = kwargs.get("response_validator")
        if validator:
            validator(result)
        return result

    monkeypatch.setattr(director, "_call_json", fake_call)
    result = director._hierarchical_director_plan(payload, budgets, 9000, "job")
    assert len(result["scenes"]) == 12
    assert result["hierarchical_planning"]["batch_count"] == 3
    batch_payloads = [body for stage, body in calls if stage.startswith("director_scene_batch_")]
    assert len(batch_payloads) == 3
    assert all(body["global_contract"]["fact_ledger"] for body in batch_payloads)


def test_long_outline_limit_uses_global_and_bounded_brief_batches(monkeypatch):
    budgets = [
        {"scene_id": index, "target_spoken_words": 30,
         "hard_minimum_words": 27, "hard_maximum_words": 33,
         "required_narration_beats": 2, "target_words_per_beat": 15}
        for index in range(1, 13)
    ]
    scene_schema = {
        "semantic_contract": {"required_entities": ["evidence"]},
        "visual_state": {"mode": "new", "group": "group", "keep": []},
    }
    payload = {
        "user_request": "Explain a process", "audience": "general",
        "target_duration_seconds": 240, "narration_word_budget": {},
        "duration_calibration": {}, "desired_scene_count": 12,
        "duration_teaching_contract": {}, "instructions": ["rule"] * 14,
        "required_schema": {
            "audience_profile": {}, "explanation_strategy": {},
            "scenes": [scene_schema],
        },
    }
    stages = []

    def fake_call(system, body, **kwargs):
        stage = kwargs["stage"]
        stages.append(stage)
        if stage == "director_strategy":
            raise RuntimeError("reached max_tokens")
        if stage == "director_strategy_global":
            return {
                "title": "Process", "learning_goal": "Understand",
                "audience_profile": {},
                "explanation_strategy": {"progression": [f"step {i}" for i in range(1, 13)]},
                "scope_summary": "Process", "must_cover": [], "out_of_scope": [],
                "fact_ledger": [], "terminology_ledger": [],
            }
        if stage.startswith("director_outline_batch_"):
            result = {"scene_briefs": [
                {"id": item["scene_id"], "teaching_beat": f"Step {item['scene_id']}",
                 "required_entities": ["evidence"]}
                for item in body["assigned_scene_word_budgets"]
            ]}
        else:
            result = {"scenes": [{
                "id": brief["id"], "teaching_beat": brief["teaching_beat"],
                "semantic_contract": scene_schema["semantic_contract"],
                "visual_state": scene_schema["visual_state"],
                "narration_beats": [{
                    **_beat(brief["id"], 1),
                    "narration": "A useful mechanism explanation with concrete visible evidence.",
                }],
            } for brief in body["scene_briefs"]]}
        if kwargs.get("response_validator"):
            kwargs["response_validator"](result)
        return result

    monkeypatch.setattr(director, "_call_json", fake_call)
    result = director._hierarchical_director_plan(payload, budgets, 9000, "job")
    assert len(result["scenes"]) == 12
    assert result["outline_fallback"]["strategy"] == "global_plus_bounded_brief_batches"
    assert stages.count("director_strategy_global") == 1
    assert len([stage for stage in stages if stage.startswith("director_outline_batch_")]) == 3


def test_duration_repair_rewrites_only_failing_scenes_in_bounded_batches(monkeypatch):
    scenes = []
    for scene_id in range(1, 6):
        word_count = 6 if scene_id in {2, 4, 5} else 20
        scenes.append({
            "id": scene_id,
            "narration_beats": [{
                "beat_id": f"s{scene_id}_b1", "required": True, "priority": 5,
                "narration": " ".join(["word"] * word_count),
                "visual_action": f"Show scene {scene_id}", "exact_anchors": [],
            }],
            "semantic_contract": {}, "visual_state": {"mode": "new"},
        })
    plan = {"title": "Test", "scenes": scenes}
    contracts = [
        {"scene_id": scene_id, "target_words": 20,
         "minimum_words": 18, "maximum_words": 22}
        for scene_id in range(1, 6)
    ]
    repaired_ids = []

    def fake_call(system, payload, **kwargs):
        result_scenes = []
        for row in payload["scenes"]:
            scene_id = int(row["id"])
            repaired_ids.append(scene_id)
            result_scenes.append({
                "id": scene_id,
                "narration_beats": [{
                    "beat_id": f"s{scene_id}_b1", "required": True, "priority": 5,
                    "narration": " ".join(["useful"] * 12),
                    "visual_action": f"Show repaired scene {scene_id}",
                    "exact_anchors": [],
                }],
            })
        result = {"scenes": result_scenes}
        kwargs["response_validator"](result)
        return result

    monkeypatch.setattr(director, "_call_json", fake_call)
    repaired = director._repair_duration_in_batches(
        plan, 100, contracts, 4000, None, 1
    )
    assert sorted(repaired_ids) == [2, 4, 5]
    by_id = {scene["id"]: scene for scene in repaired["scenes"]}
    assert len(by_id[1]["narration"].split()) == 20
    assert len(by_id[2]["narration"].split()) == 18


def test_one_good_beat_is_completed_with_only_missing_depth(monkeypatch):
    plan = {"title": "Test", "scenes": [{
        "id": 1, "narration_beats": [{
            "beat_id": "s1_core", "required": True, "priority": 5,
            "narration": " ".join(["existing"] * 36),
            "visual_action": "Show the core mechanism", "exact_anchors": [],
        }],
        "semantic_contract": {}, "visual_state": {"mode": "new"},
    }]}
    contracts = [{
        "scene_id": 1, "target_words": 54,
        "minimum_words": 49, "maximum_words": 59,
    }]
    received_existing_words = []

    def fake_call(system, payload, **kwargs):
        received_existing_words.append(sum(
            len(beat["narration"].split())
            for beat in payload["scenes"][0]["narration_beats"]
        ))
        result = {"scenes": [{
            "id": 1, "narration_beats": [{
                "beat_id": "s1_depth", "required": True, "priority": 4,
                "narration": " ".join(["additional"] * 13),
                "visual_action": "Show the concrete consequence", "exact_anchors": [],
            }],
        }]}
        kwargs["response_validator"](result)
        return result

    monkeypatch.setattr(director, "_call_json", fake_call)
    repaired = director._repair_duration_in_batches(
        plan, 20, contracts, 3000, None, 1
    )
    beats = repaired["scenes"][0]["narration_beats"]
    assert received_existing_words == [36]
    assert len(beats) == 2
    assert len(repaired["scenes"][0]["narration"].split()) == 49
    assert beats[0]["narration"].startswith("existing")


def test_one_word_scene_deficit_is_repaired_without_full_scene_retry(monkeypatch):
    plan = {"title": "Test", "scenes": [{
        "id": 1, "narration_beats": [
            {
                "beat_id": "s1_core", "required": True, "priority": 5,
                "narration": " ".join(["existing"] * 24),
                "visual_action": "Show the mechanism", "exact_anchors": [],
            },
            {
                "beat_id": "s1_example", "required": True, "priority": 4,
                "narration": " ".join(["example"] * 24),
                "visual_action": "Show the worked example", "exact_anchors": [],
            },
        ],
        "semantic_contract": {}, "visual_state": {"mode": "new"},
    }]}
    contracts = [{
        "scene_id": 1, "target_words": 54,
        "minimum_words": 49, "maximum_words": 59,
    }]
    requested_ranges = []

    def fake_call(system, payload, **kwargs):
        requested_ranges.append(payload["scenes"][0]["additional_word_range"])
        result = {"scenes": [{
            "id": 1, "narration_beats": [{
                "beat_id": "s1_consequence", "required": True, "priority": 3,
                "narration": "consequence", "visual_action": "Show the consequence",
                "exact_anchors": [],
            }],
        }]}
        kwargs["response_validator"](result)
        return result

    monkeypatch.setattr(director, "_call_json", fake_call)
    repaired = director._repair_duration_in_batches(
        plan, 20, contracts, 3000, None, 1
    )

    assert requested_ranges == [[1, 11]]
    assert len(repaired["scenes"][0]["narration"].split()) == 49
    assert repaired["scenes"][0]["narration_beats"][0]["narration"].startswith("existing")


def test_overlong_rewrite_receives_exact_per_beat_budgets(monkeypatch):
    plan = {"title": "Test", "scenes": [{
        "id": 1,
        "narration_beats": [
            {**_beat(1, 1), "narration": " ".join(["first"] * 40)},
            {**_beat(1, 2), "narration": " ".join(["second"] * 40)},
        ],
        "semantic_contract": {}, "visual_state": {"mode": "new"},
    }]}
    contracts = [{
        "scene_id": 1, "target_words": 54,
        "minimum_words": 50, "maximum_words": 58,
    }]
    observed = {}

    def fake_call(system, payload, **kwargs):
        observed["contracts"] = payload["scenes"][0]["beat_word_contracts"]
        observed["max_tokens"] = kwargs["max_tokens"]
        result = {"scenes": [{
            "id": 1,
            "narration_beats": [
                {**_beat(1, 1), "narration": " ".join(["first"] * 27)},
                {**_beat(1, 2), "narration": " ".join(["second"] * 27)},
            ],
        }]}
        kwargs["response_validator"](result)
        return result

    monkeypatch.setattr(director, "_call_json", fake_call)
    repaired = director._repair_duration_in_batches(
        plan, 20, contracts, 8000, None, 1
    )

    assert observed["contracts"] == [
        {"beat_index": 1, "target_words": 27, "minimum_words": 26, "maximum_words": 28},
        {"beat_index": 2, "target_words": 27, "minimum_words": 26, "maximum_words": 28},
    ]
    assert observed["max_tokens"] >= 2600
    assert len(repaired["scenes"][0]["narration"].split()) == 54


def test_factuality_fallback_reaches_fixed_point_for_adjacent_sample_values():
    plan = {"scenes": [{
        "id": 1,
        "narration_beats": [{
            **_beat(1, 1),
            "narration": "Use the sample list [1,3,5,7,9,11,13] for this demonstration.",
        }],
        "semantic_contract": {
            "claim": "The sample list [1,3,5,7,9,11,13] demonstrates the mechanism.",
        },
    }]}

    result = director._apply_deterministic_factuality_fallback(plan, [])

    assert result["remaining"] == []
    assert result["pass_count"] >= 2


def test_global_constructed_example_provenance_is_propagated_by_literal_identity():
    plan = {
        "example_contract": {
            "constructed_example": True,
            "example_context": "A hypothetical input used only to demonstrate the procedure.",
            "example_literals": ["[1,3,5,7,9,11,13]", "target 7"],
        },
        "scenes": [{
            "id": 1,
            "narration_beats": [{
                **_beat(1, 1),
                "narration": "Search [1,3,5,7,9,11,13] for the target 7.",
            }],
            "semantic_contract": {"constructed_example": False},
        }],
    }

    director._propagate_global_example_contract(plan)

    contract = plan["scenes"][0]["semantic_contract"]
    assert contract["constructed_example"] is True
    assert contract["example_context"]
    assert director._scene_factuality_violations(plan, []) == []


def test_render_salvage_replaces_only_offending_element(monkeypatch):
    plan = {"elements": [
        {"type": "rect", "id": "good", "x": 20, "y": 20},
        {"type": "path", "id": "broken", "x": 100, "y": 100,
         "semantic_refs": ["E1"]},
    ]}

    def fake_render(candidate, sketch=False):
        if any(element.get("id") == "broken" for element in candidate["elements"]):
            raise RuntimeError("bad path")
        return "<svg/>", []

    monkeypatch.setattr(app_module, "render_visual_plan", fake_render)
    salvaged, svg, _, offending = app_module._salvage_single_render_element(
        plan, sketch=False
    )
    assert svg == "<svg/>" and offending["id"] == "broken"
    assert any(element.get("id") == "good" for element in salvaged["elements"])
    replacement = next(
        element for element in salvaged["elements"]
        if str(element.get("id", "")).startswith("salvaged_")
    )
    assert replacement["semantic_refs"] == ["E1"]


def test_generalized_regression_corpus_has_renderable_semantic_fallbacks():
    corpus_path = Path("tests/fixtures/visual_regression_corpus.json")
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    assert {item["category"] for item in corpus} >= {
        "physical_illustration", "timeline", "map", "algorithm", "mathematics",
        "code", "comparison", "quantitative_chart", "branded_product",
        "long_narrative",
    }
    for case in corpus:
        plan = visual_quality.semantic_fallback_plan(
            case["prompt"], {"required_entities": case["entities"]}
        )
        normalized = visual_engine.normalize_visual_plan(plan)
        assert normalized["elements"], case["category"]
        assert visual_quality.candidate_score(
            normalized, {"required_entities": case["entities"]}
        )["required_coverage"] == 1.0


def test_pipeline_trace_redacts_secret_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(run_log, "LOGS_DIR", tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_super_secret_value")
    run_log.log_local_transform(
        "abc123abc123", "safe_trace",
        before={"api_key": "gsk_super_secret_value"},
        after={"nested": {"authorization": "Bearer gsk_super_secret_value"}},
    )
    call = run_log.get_calls("abc123abc123")[0]
    assert call["before"]["api_key"] == "[REDACTED]"
    assert call["after"]["nested"]["authorization"] == "[REDACTED]"


def test_pie_chart_requires_complete_authorized_whole():
    malformed = {
        "chart": {"kind": "pie", "segments": [
            {"label": "A", "value": 40}, {"label": "B", "value": 20},
        ]}
    }
    try:
        visual_quality.validate_chart_spec(malformed, {"authorized_quantities": ["40", "20"]})
    except ValueError as exc:
        assert "complete whole" in str(exc)
    else:
        raise AssertionError("incomplete pie chart was accepted")

    complete = {
        "chart": {"kind": "donut", "segments": [
            {"label": "A", "value": 40}, {"label": "B", "value": 60},
        ]}
    }
    visual_quality.validate_chart_spec(
        complete, {"authorized_quantities": ["40", "60"]}
    )


def test_explicit_owner_is_not_reported_as_shape_collision():
    elements = [
        {"type": "polygon", "id": "layer_aws", "points": "0,0 100,0 50,100"},
        {"type": "text", "id": "label_aws", "text": "AWS", "x": 50, "y": 50,
         "font_size": 24, "owner_id": "layer_aws", "label_for": "layer_aws"},
    ]
    assert freeform_repair._cross_type_collision_pairs(elements) == []


def test_diagonal_connector_uses_stroke_geometry_not_its_bounding_box():
    elements = [
        {"type": "arrow", "id": "route", "x1": 100, "y1": 100,
         "x2": 900, "y2": 500, "stroke_width": 5},
        {"type": "text", "id": "status_text", "text": "Picked",
         "x": 700, "y": 180, "font_size": 20},
    ]
    assert freeform_repair._cross_type_collision_pairs(elements) == []


def test_named_container_relationship_is_persisted_for_later_gates():
    plan = {"elements": [
        {"type": "rect", "id": "status_badge", "x": 300, "y": 200,
         "width": 160, "height": 60},
        {"type": "text", "id": "status_text", "text": "Ready",
         "x": 380, "y": 238, "font_size": 20, "align": "center"},
    ]}
    fixed = freeform_repair.repair_post_continuity_plan(plan)
    text = next(item for item in fixed["elements"] if item["id"] == "status_text")
    assert text["owner_id"] == "status_badge"
    assert text["label_for"] == "status_badge"


def test_hidden_alternative_state_does_not_create_static_layout_failure():
    plan = {"elements": [
        {"type": "icon", "id": "check_icon_final", "x": 500, "y": 300,
         "width": 32, "height": 32, "opacity": 0},
        {"type": "text", "id": "status_label_final", "text": "Confirmed",
         "x": 500, "y": 300, "font_size": 20, "opacity": 0},
    ]}
    fixed = freeform_repair.repair_post_continuity_plan(plan)
    diagnostics = fixed["_freeform_repair"]
    assert diagnostics["unresolved_cross_type_collision_count"] == 0
    assert diagnostics["unresolved_detached_label_count"] == 0


def test_static_state_compiler_applies_only_declared_final_values():
    plan = {
        "elements": [
            {"type": "text", "id": "moving_label", "x": 100, "y": 100, "opacity": 0},
            {"type": "text", "id": "old_state", "x": 200, "y": 100, "opacity": 1},
        ],
        "animations": [
            {"target_id": "moving_label", "action": "move",
             "to": {"x": 420, "y": 260}},
            {"target_ids": ["moving_label"], "type": "fade_in"},
            {"actions": [{"element_id": "old_state", "property": "opacity", "value": 0}]},
        ],
    }
    fixed = visual_engine.materialize_static_visual_state(plan)
    by_id = {item["id"]: item for item in fixed["elements"]}
    assert (by_id["moving_label"]["x"], by_id["moving_label"]["y"]) == (420, 260)
    assert by_id["moving_label"]["opacity"] == 1
    assert by_id["old_state"]["opacity"] == 0
    assert fixed["_static_state"]["applied_count"] == 3


def test_static_state_compiler_emits_one_cumulative_state_per_narration_beat():
    plan = {
        "elements": [
            {"type": "text", "id": "first", "text": "First", "opacity": 0},
            {"type": "text", "id": "second", "text": "Second", "opacity": 0},
        ],
        "animations": [
            {"beat_id": "b1", "target_id": "first", "action": "fade_in"},
            {"beat_id": "b2", "target_id": "second", "action": "fade_in"},
        ],
    }
    states = visual_engine.compile_static_beat_states(
        plan, [{"beat_id": "b1"}, {"beat_id": "b2"}]
    )
    assert len(states) == 2
    first = {item["id"]: item for item in states[0]["elements"]}
    second = {item["id"]: item for item in states[1]["elements"]}
    assert first["first"]["opacity"] == 1 and first["second"]["opacity"] == 0
    assert second["first"]["opacity"] == 1 and second["second"]["opacity"] == 1


def test_unrelated_icon_is_displaced_from_owned_label_container():
    plan = {
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "elements": [
            {"type": "rect", "id": "shipping_label", "x": 480, "y": 330,
             "width": 140, "height": 90},
            {"type": "icon", "id": "shoes", "query": "sneakers", "x": 550,
             "y": 375, "width": 70, "height": 60},
            {"type": "text", "id": "tracking_number_text", "text": "1Z999AA10123456784",
             "x": 550, "y": 380, "font_size": 18, "align": "center",
             "owner_id": "shipping_label", "label_for": "shipping_label"},
        ],
    }
    fixed = freeform_repair.repair_post_continuity_plan(plan)
    icon = next(item for item in fixed["elements"] if item["id"] == "shoes")
    assert (icon["x"], icon["y"]) != (550, 375)
    assert fixed["_freeform_repair"]["unresolved_cross_type_collision_count"] == 0


def test_explicit_higher_and_surface_layers_are_reordered_without_topic_rules():
    plan = {"elements": [
        {"type": "rect", "id": "warm_layer", "x": 0, "y": 40,
         "width": 1280, "height": 210},
        {"type": "text", "id": "warm_label", "text": "Warm Air",
         "x": 60, "y": 130, "font_size": 24},
        {"type": "rect", "id": "cool_layer", "x": 0, "y": 250,
         "width": 1280, "height": 420},
        {"type": "text", "id": "cool_label", "text": "Cool Air",
         "x": 60, "y": 380, "font_size": 24},
    ]}
    fixed = freeform_repair.repair_post_continuity_plan(
        plan, spoken_context="A gradient from warm near the surface to cool higher up."
    )
    by_id = {item["id"]: item for item in fixed["elements"]}
    assert by_id["cool_layer"]["y"] < by_id["warm_layer"]["y"]
    assert by_id["cool_label"]["y"] < by_id["warm_label"]["y"]
    assert fixed["_freeform_repair"]["explicit_vertical_order_repairs"] == 1


def test_label_is_hidden_when_its_owned_visual_state_is_hidden():
    plan = {"elements": [
        {"type": "circle", "id": "subject", "x": 300, "y": 300,
         "radius": 30, "opacity": 0},
        {"type": "text", "id": "subject_label", "text": "Subject",
         "x": 300, "y": 360, "font_size": 20, "owner_id": "subject"},
    ]}
    fixed = freeform_repair.repair_post_continuity_plan(plan)
    by_id = {item["id"]: item for item in fixed["elements"]}
    assert by_id["subject_label"]["opacity"] == 0
    assert fixed["_freeform_repair"]["labels_hidden_with_owner"] == 1
    assert fixed["_freeform_repair"]["unresolved_detached_label_count"] == 0


def test_owned_badge_and_label_move_together_around_connector():
    plan = {"elements": [
        {"type": "rect", "id": "status_badge", "x": 500, "y": 300,
         "width": 150, "height": 70},
        {"type": "text", "id": "status_label", "text": "Current state",
         "x": 575, "y": 342, "font_size": 20, "align": "center",
         "owner_id": "status_badge"},
        {"type": "arrow", "id": "transition", "x1": 430, "y1": 335,
         "x2": 720, "y2": 335, "stroke_width": 4},
    ]}
    fixed = freeform_repair.repair_post_continuity_plan(plan)
    by_id = {item["id"]: item for item in fixed["elements"]}
    assert (by_id["status_badge"]["x"], by_id["status_badge"]["y"]) != (500, 300)
    assert by_id["status_label"]["x"] - by_id["status_badge"]["x"] == 75
    assert fixed["_freeform_repair"]["unresolved_cross_type_collision_count"] == 0


def test_canvas_edge_line_uses_visual_stroke_bounds_not_arrowhead_padding():
    bounds = visual_engine.element_bounds_raw({
        "type": "line", "x1": 0, "y1": 700, "x2": 1280, "y2": 700,
        "stroke_width": 4,
    })
    assert bounds == (-0.5, 699.5, 1280.5, 700.5)


def test_director_plan_boundary_rejects_error_shaped_json():
    try:
        director._validate_director_plan_response({
            "error": "Unable to generate the complete detailed lesson plan."
        })
    except ValueError as exc:
        assert "non-empty scenes" in str(exc)
        assert "Unable to generate" in str(exc)
    else:
        raise AssertionError("provider refusal was accepted as a plan")


def test_browser_renderer_retries_zero_exit_without_png(monkeypatch, tmp_path):
    calls = []

    class Completed:
        returncode = 0
        stderr = b""
        stdout = b""

    def fake_run(command, **kwargs):
        calls.append(command)
        screenshot = next(item for item in command if item.startswith("--screenshot="))
        if len(calls) == 2:
            Path(screenshot.split("=", 1)[1]).write_bytes(b"png")
        return Completed()

    monkeypatch.setattr(video_engine, "_find_browser", lambda: "chrome.exe")
    monkeypatch.setattr(video_engine.subprocess, "run", fake_run)
    monkeypatch.setattr(video_engine.time, "sleep", lambda _: None)
    destination = tmp_path / "scene.png"
    video_engine._svg_to_png_with_browser("<svg/>", destination)
    assert destination.read_bytes() == b"png"
    assert len(calls) == 2


def test_independent_sequence_clears_stale_llm_continuity():
    plan = {"scenes": [
        {
            "id": index,
            "builds_on": str(index - 1) if index > 1 else "",
            "semantic_contract": {"required_entities": [f"unique {index}"]},
            "visual_state": {"mode": "continue", "group": "dirty", "keep": [f"unique {index}"]},
        }
        for index in range(1, 4)
    ]}
    result = director._apply_visual_sequence_contract(plan)
    assert result["visual_sequence_blueprint"]["mode"] == "independent"
    assert [scene["visual_state"]["mode"] for scene in result["scenes"]] == ["new"] * 3
    assert len({scene["visual_state"]["group"] for scene in result["scenes"]}) == 3


def test_declared_terminology_repairs_missing_keep_for_recurring_subject():
    plan = {
        "learning_goal": "Explain a connected mechanism",
        "terminology_ledger": [{"term": "weight", "meaning": "adjustable connection"}],
        "explanation_strategy": {"mental_model": "a process", "progression": ["predict", "adjust"]},
        "scenes": [
            {"id": 1, "builds_on": "", "semantic_contract": {"required_entities": ["network weight"]}},
            {"id": 2, "builds_on": "scene 1", "semantic_contract": {"required_entities": ["updated weights"]}},
            {"id": 3, "builds_on": "scene 2", "semantic_contract": {"required_entities": ["weight adjustment"]}},
        ],
    }
    result = director._apply_visual_sequence_contract(plan)
    assert result["visual_sequence_blueprint"]["mode"] == "connected"
    assert result["visual_sequence_blueprint"]["stable_subjects"] == ["weight"]


def test_pedagogical_gate_detects_dangling_and_misaligned_beat():
    plan = {"scenes": [{
        "id": 1,
        "narration_beats": [{
            "beat_id": "b1",
            "narration": "The learner sees a random guess and",
            "visual_action": "Show an unrelated mailbox",
        }],
    }]}
    violations = director._pedagogical_quality_violations(plan)
    assert "dangling_ending" in violations[0]["reasons"]
    assert "visual_action_not_grounded_in_narration" in violations[0]["reasons"]


def test_pedagogical_gate_detects_force_direction_and_constraint_category_errors():
    plan = {"scenes": [{
        "id": 1,
        "narration_beats": [{
            "beat_id": "b1",
            "narration": "Forward velocity redirects gravity while a string makes the ball free fall.",
            "visual_action": "Show velocity redirecting gravity on a constrained ball.",
        }],
    }]}
    violations = director._pedagogical_quality_violations(plan)
    assert "conceptual_category_conflict" in violations[0]["reasons"]


def test_pedagogical_gate_accepts_observed_motion_match_and_word_stems():
    plan = {"scenes": [{
        "id": 1,
        "narration_beats": [{
            "beat_id": "b1",
            "narration": "The calculated speed matches the observed orbital motion.",
            "visual_action": "Show the calculation beside an orbiting object.",
        }],
    }]}
    violations = director._pedagogical_quality_violations(plan)
    reasons = {reason for row in violations for reason in row["reasons"]}
    assert "conceptual_category_conflict" not in reasons
    assert "visual_action_not_grounded_in_narration" not in reasons


def test_pedagogical_gate_rejects_outward_motion_and_force_velocity_balance():
    plan = {"scenes": [{
        "id": 1,
        "narration_beats": [{
            "beat_id": "b1",
            "narration": (
                "A balanced scale compares gravity with orbital velocity, "
                "while the Moon's motion pushes outward."
            ),
            "visual_action": "Show a balance scale with gravity and velocity icons.",
        }],
    }]}
    violations = director._pedagogical_quality_violations(plan)
    reasons = {reason for row in violations for reason in row["reasons"]}
    assert "conceptual_category_conflict" in reasons


def test_svg_arc_flags_are_not_treated_as_canvas_coordinates():
    bounds = visual_engine.element_bounds_raw({
        "type": "path",
        "d": "M 560 200 A 160 160 0 0 1 720 360 M 560 520 A 160 160 0 0 1 400 360",
    })
    assert bounds is not None
    assert 350 <= bounds[0] <= 560
    assert 720 <= bounds[2] <= 900
    assert 30 <= bounds[1] <= 200
    assert 520 <= bounds[3] <= 690


def test_named_container_repairs_center_anchor_and_icon_label_lane():
    plan = {"elements": [
        {"type": "rect", "id": "plus_badge", "x": 955, "y": 220, "width": 140, "height": 60, "layer": 2},
        {"type": "text", "id": "plus_text", "text": "Plus", "x": 955, "y": 255, "font_size": 24, "align": "center", "layer": 3},
        {"type": "rect", "id": "upgrade_btn", "x": 250, "y": 480, "width": 150, "height": 50, "layer": 2},
        {"type": "icon", "id": "cursor_icon", "query": "mouse cursor", "x": 325, "y": 505, "width": 40, "height": 40, "layer": 2},
        {"type": "text", "id": "upgrade_text", "text": "Upgrade", "x": 325, "y": 540, "font_size": 20, "align": "center", "layer": 3},
    ]}
    fixed = freeform_repair.repair_post_continuity_plan(plan)
    by_id = {element["id"]: element for element in fixed["elements"]}
    assert by_id["plus_text"]["x"] == 1025
    plus_bounds = freeform_repair._text_bounds(by_id["plus_text"])
    assert 955 <= plus_bounds[0] < plus_bounds[2] <= 1095
    upgrade_bounds = freeform_repair._text_bounds(by_id["upgrade_text"])
    assert 250 <= upgrade_bounds[0] < upgrade_bounds[2] <= 400
    assert 480 <= upgrade_bounds[1] < upgrade_bounds[3] <= 530
    assert not freeform_repair._cross_type_collision_pairs(fixed["elements"])


def test_explicit_brand_query_tries_brand_family_without_brand_table(monkeypatch):
    calls = []
    def fake_search(query, preferred_family=None):
        calls.append((query, preferred_family))
        return "simple-icons:example" if preferred_family == "simple-icons" else None
    monkeypatch.setattr(visual_engine, "search_iconify", fake_search)
    icon, _, _ = visual_engine.resolve_iconify("ExampleProduct logo", preferred_family="mdi")
    assert icon == "simple-icons:example"
    assert calls[0][1] == "simple-icons"


def test_compound_icon_query_rejects_single_word_semantic_drift(monkeypatch):
    monkeypatch.setattr(
        visual_engine,
        "search_iconify",
        lambda query, preferred_family=None: "mdi:network" if query == "network" else None,
    )
    icon, _, _ = visual_engine.resolve_iconify(
        "deep neural network", preferred_family="mdi"
    )
    assert icon is None


def test_unresolved_icon_fallback_displays_full_concept_instead_of_initials():
    svg = visual_engine._render_icon_monogram(
        {"x": 100, "y": 100, "width": 180, "height": 100, "opacity": 1},
        "deep neural network",
    )
    assert "deep neural" in svg
    assert "network" in svg
    assert ">DN<" not in svg


def test_download_bundle_contains_complete_model_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(run_log, "LOGS_DIR", tmp_path)
    run_log.log_llm_call(
        job_id="abc123abc123", stage="director_plan", provider="groq",
        model="openai/gpt-oss-120b", key_prefix="gsk_test",
        prompt_tokens=10, completion_tokens=5, latency_ms=12,
        request_payload={"messages": [{"role": "user", "content": "input"}]},
        response_payload={"content": "output"},
    )
    bundle = run_log.get_download_bundle("abc123abc123")
    assert bundle["calls"][0]["request"]["messages"][0]["content"] == "input"
    assert bundle["calls"][0]["response"]["content"] == "output"
    run_log.log_local_transform(
        "abc123abc123", "canonicalize", scene_id=2, candidate_id="raw",
        before={"fontSize": 48}, after={"font_size": 48},
        details={"translated": ["fontSize->font_size"]},
    )
    bundle = run_log.get_download_bundle("abc123abc123")
    assert bundle["schema_version"] == "pipeline-run-bundle-v2"
    assert bundle["calls"][-1]["kind"] == "local_transform"


def test_daily_token_limit_and_reconciliation_are_enforced(monkeypatch):
    monkeypatch.setenv("GROQ_TPM_LIMIT", "8000")
    monkeypatch.setenv("GROQ_TPD_LIMIT", "100")
    monkeypatch.setenv("GROQ_API_KEY", "key")
    monkeypatch.setenv("GROQ_API_KEYS", "")
    pool = api_key_pool.APIKeyPool("groq")
    entry = api_key_pool._KeyEntry("key", object())
    pool._entries = [entry]
    assert pool.checkout(80, now=1000) is entry
    assert pool.checkout(30, now=1001) is None
    pool.reconcile(entry, 80, 40, now=1002)
    assert pool.checkout(30, now=1003) is entry


def test_qwen_completion_budget_respects_groq_output_limit(monkeypatch):
    monkeypatch.delenv("GROQ_OTPM_LIMIT", raising=False)
    monkeypatch.setenv("GROQ_OTPM_SAFETY_TOKENS", "100")
    config = llm_provider.LLMConfig(
        provider="groq", model="qwen/qwen3.6-27b", api_key="test",
        base_url="https://example.invalid/v1", default_headers={},
    )

    budget = llm_provider.fit_completion_token_budget(
        config,
        [{"role": "user", "content": "Create a compact visual."}],
        4500,
    )

    assert budget == 900


def test_explicit_groq_output_limit_overrides_model_default(monkeypatch):
    monkeypatch.setenv("GROQ_OTPM_LIMIT", "2400")
    monkeypatch.setenv("GROQ_OTPM_SAFETY_TOKENS", "100")
    config = llm_provider.LLMConfig(
        provider="groq", model="qwen/qwen3.6-27b", api_key="test",
        base_url="https://example.invalid/v1", default_headers={},
    )

    assert llm_provider.fit_completion_token_budget(
        config,
        [{"role": "user", "content": "x"}],
        4500,
    ) == 2300


def test_unicode_diagnostics_cannot_discard_visual_response(monkeypatch):
    class LegacyConsole(io.StringIO):
        encoding = "cp1252"

        def write(self, value):
            value.encode(self.encoding)
            return super().write(value)

    console = LegacyConsole()
    monkeypatch.setattr("sys.stdout", console)

    visual_engine._safe_diagnostic_print("signal → receiver")

    assert "signal \\u2192 receiver" in console.getvalue()


def test_provider_workers_scale_to_distinct_organization_keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "one")
    monkeypatch.setenv("GROQ_API_KEYS", "two,three,four,five,six,seven,eight")
    monkeypatch.setenv("AUTO_SCALE_PROVIDER_WORKERS", "true")
    monkeypatch.setenv("MAX_PROVIDER_WORKERS", "8")
    assert api_key_pool.configured_key_count("groq") == 8
    assert api_key_pool.recommended_provider_workers("groq", 2) == 8


def test_stream_player_has_valid_buffer_tail_condition():
    source = Path("templates/stream_player.html").read_text(encoding="utf-8")
    assert "video.currentTime >= video.buffered.length ? 0 : 0" not in source
    assert "video.buffered.end(video.buffered.length - 1)" in source


def test_batch_result_restores_video_after_prior_failure():
    source = Path("templates/index.html").read_text(encoding="utf-8")
    assert "video.style.display = 'block'" in source
    assert "videoLink.style.display = 'inline-block'" in source
    assert "manifestLink.style.display = 'inline-block'" in source
