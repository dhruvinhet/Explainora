"""Deterministic duration-first narration/visual beat compiler."""
from __future__ import annotations

import os
from typing import Any, Dict, List

from duration_calibrator import duration_budget, estimate_text_seconds


def _clean_beats(scene: Dict[str, Any]) -> List[Dict[str, Any]]:
    cleaned = []
    raw = scene.get("narration_beats")
    if isinstance(raw, list):
        for index, beat in enumerate(raw[:12], start=1):
            if not isinstance(beat, dict):
                continue
            narration = str(beat.get("narration", "")).strip()
            visual = str(beat.get("visual_action", "")).strip()
            if not narration or not visual:
                continue
            try:
                priority = max(1, min(5, int(beat.get("priority", 3))))
            except (TypeError, ValueError):
                priority = 3
            exact_anchors = beat.get("exact_anchors", [])
            cleaned.append({
                "beat_id": str(beat.get("beat_id", f"b{index}")).strip()[:64] or f"b{index}",
                "required": bool(beat.get("required", True)), "priority": priority,
                "narration": narration,
                # Exact literals remain structured metadata. They must not be
                # concatenated into the visible scene request; visual models
                # otherwise render the constraint list as tiny footer text.
                "visual_action": visual,
                "exact_anchors": exact_anchors if isinstance(exact_anchors, list) else [],
                "_order": index,
            })
    if not cleaned:
        scene_contract = scene.get("semantic_contract")
        scene_contract = scene_contract if isinstance(scene_contract, dict) else {}
        scene_anchors = scene_contract.get("exact_anchors", [])
        cleaned = [{"beat_id": "core", "required": True, "priority": 5,
                    "narration": str(scene.get("narration", "")).strip(),
                    "visual_action": str(scene.get("visual_prompt", "")).strip(),
                    "exact_anchors": scene_anchors if isinstance(scene_anchors, list) else [],
                    "_order": 1}]
    return [beat for beat in cleaned if beat["narration"] and beat["visual_action"]]


def _allocate(total: float, weights: List[float], minimum: float = 2.0) -> List[float]:
    if not weights:
        return []
    if minimum * len(weights) > total:
        minimum = 0.0
    remaining = max(0.0, total - minimum * len(weights))
    weight_sum = sum(max(0.01, weight) for weight in weights)
    values = [minimum + remaining * max(0.01, weight) / weight_sum for weight in weights]
    values[-1] += total - sum(values)
    return values


def allocate_final_scene_durations(
    raw_durations: List[float], target_seconds: float, planned_targets: List[float] | None = None,
) -> List[float]:
    """Allocate a *bounded* global gap across scene boundaries.

    This avoids accumulating per-scene prediction errors. If raw speech itself
    exceeds the request, nothing is truncated and the honest raw durations are
    returned; otherwise the returned values sum exactly to the request.
    """
    raw = [max(0.0, float(value)) for value in raw_durations]
    gap = float(target_seconds) - sum(raw)
    if not raw or gap <= 0:
        return raw
    total_pause_cap = max(0.0, float(target_seconds) * float(
        os.environ.get("MAX_BOUNDARY_PAUSE_RATIO", "0.05")
    ))
    per_scene_cap = max(0.0, float(os.environ.get("MAX_SCENE_BOUNDARY_PAUSE_SECONDS", "0.9")))
    gap = min(gap, total_pause_cap, per_scene_cap * len(raw))
    planned = planned_targets or []
    deficits = [
        max(0.05, (float(planned[i]) - value) if i < len(planned) else 1.0)
        for i, value in enumerate(raw)
    ]
    total_weight = sum(deficits)
    pauses = [0.0] * len(raw)
    remaining = gap
    active = set(range(len(raw)))
    while remaining > 1e-9 and active:
        active_weight = sum(deficits[index] for index in active)
        distributed = 0.0
        for index in list(active):
            share = remaining * deficits[index] / max(1e-9, active_weight)
            room = per_scene_cap - pauses[index]
            added = min(room, share)
            pauses[index] += added
            distributed += added
            if room - added <= 1e-9:
                active.remove(index)
        if distributed <= 1e-9:
            break
        remaining -= distributed
    final = [value + pause for value, pause in zip(raw, pauses)]
    return final


def compile_duration_first_plan(plan: Dict[str, Any], target_seconds: float) -> Dict[str, Any]:
    scenes = [scene for scene in plan.get("scenes", []) if isinstance(scene, dict)]
    if not scenes:
        return plan
    budget = duration_budget(target_seconds, len(scenes))
    all_beats = [_clean_beats(scene) for scene in scenes]
    weights = [sum(estimate_text_seconds(beat["narration"]) for beat in beats) for beats in all_beats]
    spoken_targets = _allocate(float(budget["spoken_target_seconds"]), weights, minimum=1.5)

    emitted_words = sum(len(beat["narration"].split()) for beats in all_beats for beat in beats)
    preserve_all = emitted_words <= int(budget["target_words"] * 1.08)
    metadata, predictions = [], []
    for scene, beats, target in zip(scenes, all_beats, spoken_targets):
        required = [beat for beat in beats if beat["required"]]
        optional = sorted((beat for beat in beats if not beat["required"]),
                          key=lambda beat: (-beat["priority"], beat["_order"]))
        selected = list(beats) if preserve_all else list(required)
        current = sum(estimate_text_seconds(beat["narration"]) for beat in selected)
        for beat in ([] if preserve_all else optional):
            candidate = current + estimate_text_seconds(beat["narration"])
            if candidate <= target * 1.03 or current < target * 0.82:
                selected.append(beat)
                current = candidate
        selected.sort(key=lambda beat: beat["_order"])
        scene["narration_beats"] = [
            {key: beat[key] for key in (
                "beat_id", "required", "priority", "narration", "visual_action", "exact_anchors"
            )}
            for beat in beats
        ]
        scene["selected_beat_ids"] = [beat["beat_id"] for beat in selected]
        # Both outputs come from the same selected units; removed narration can
        # never leave a now-unrelated visual instruction behind.
        scene["narration"] = " ".join(beat["narration"] for beat in selected).strip()
        scene["visual_prompt"] = " Then, ".join(beat["visual_action"] for beat in selected).strip()
        contract = scene.get("semantic_contract")
        if not isinstance(contract, dict):
            contract = {}
            scene["semantic_contract"] = contract
        # Duration repair may add teaching beats after the director created the
        # original visual contract.  Persist the selected visual actions in the
        # authoritative contract so planning, rescue, and QA all see the same
        # version of the lesson.
        contract["beat_visual_requirements"] = [
            {"beat_id": beat["beat_id"], "visual_action": beat["visual_action"]}
            for beat in selected
        ]
        predicted = max(0.1, estimate_text_seconds(scene["narration"]))
        predictions.append(predicted)
        scene["target_spoken_seconds"] = round(target, 4)
        metadata.append({
            "scene_id": scene.get("id"), "selected_beat_ids": scene["selected_beat_ids"],
            "required_beat_ids": [beat["beat_id"] for beat in required],
            "predicted_spoken_seconds": round(predicted, 4),
        })

    final_targets = _allocate(float(target_seconds), predictions, minimum=2.0)
    for scene, final_target, meta in zip(scenes, final_targets, metadata):
        scene["target_scene_seconds"] = round(final_target, 4)
        meta["target_scene_seconds"] = round(final_target, 4)
        meta["target_spoken_seconds"] = scene["target_spoken_seconds"]
    plan["scenes"] = scenes
    plan["duration_compiler"] = {
        "version": "1.0", "strategy": "semantic_beats_first_single_tts_pass",
        "visual_binding": "selected_beat_ids", "extra_llm_calls": 0,
        "tempo_adjustment_allowed": False, "all_beats_preserved_for_short_plan": preserve_all,
        **budget, "scenes": metadata,
    }
    words = sum(len(str(scene.get("narration", "")).split()) for scene in scenes)
    minimum_ratio = float(os.environ.get("NARRATION_MIN_WORD_RATIO", "0.88"))
    maximum_ratio = float(os.environ.get("NARRATION_MAX_WORD_RATIO", "1.15"))
    word_ratio = words / max(1, int(budget["target_words"]))
    plan["duration_preflight"] = {
        "status": "pass" if minimum_ratio <= word_ratio <= maximum_ratio else "fail",
        "actual_words": words, "target_words": int(budget["target_words"]),
        "word_ratio": round(word_ratio, 6), "minimum_word_ratio": minimum_ratio,
        "maximum_word_ratio": maximum_ratio,
        "predicted_spoken_seconds": round(sum(predictions), 4),
        "reason": "within_narration_budget" if minimum_ratio <= word_ratio <= maximum_ratio
        else ("narration_too_short" if word_ratio < minimum_ratio else "narration_too_long"),
    }
    return plan


def assert_duration_preflight(plan: Dict[str, Any]) -> None:
    check = plan.get("duration_preflight") if isinstance(plan.get("duration_preflight"), dict) else {}
    if check.get("status") == "pass":
        return
    raise NarrationDurationError(check)


class NarrationDurationError(ValueError):
    def __init__(self, diagnostics: Dict[str, Any]):
        self.diagnostics = dict(diagnostics)
        super().__init__(
            "Director narration duration contract failed after automatic repair: "
            f"{diagnostics.get('actual_words', 0)} words generated for a "
            f"{diagnostics.get('target_words', 0)}-word target "
            f"({float(diagnostics.get('word_ratio', 0)) * 100:.1f}%). "
            "No TTS, visuals, or encoding were started, so a short script cannot be hidden with silence."
        )
