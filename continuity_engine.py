"""Deterministic visual state reconciliation across independently planned scenes.

LLM calls remain parallel.  Once each scene plan is ready, this coordinator
commits them in lesson order and preserves the rendered geometry of recurring
semantic objects.  It uses stable element IDs/text/icon queries and the
director's compact visual_state contract; it contains no topic-specific rules
and makes no extra model calls.
"""

from __future__ import annotations

import copy
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


MAX_ELEMENTS = 64
WAIT_SECONDS = max(10.0, float(os.environ.get("CONTINUITY_WAIT_SECONDS", "180")))
_GENERIC_IDS = {"", "element", "shape", "text", "label", "node", "box", "icon"}
_ID_SUFFIXES = {
    "box", "shape", "label", "text", "icon", "node", "circle", "rect",
    "line", "arrow", "path", "value", "title",
}
_ID_PREFIXES = {"label", "text", "icon", "node", "shape", "box", "value"}
_GEOMETRY_TYPES = {"text", "rect", "circle", "ellipse", "line", "arrow", "icon", "polygon", "polyline"}
_SUPPORT_TOKENS = {
    "bg", "background", "backdrop", "frame", "container", "panel", "surface",
    "outline", "border", "perimeter", "base",
}
_ENVIRONMENT_SUPPORT_NAMES = {"field", "rink", "pitch", "court", "canvas"}
_NON_SUBJECT_TYPES = {"text", "line", "arrow"}
_GENERIC_ROLE_NAMES = {
    "input", "output", "decision", "process", "result", "step", "state",
    "start", "end", "value", "data", "action", "logic", "system",
}


def _slug(value: Any) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    parts = [part for part in value.split("_") if part]
    while len(parts) > 1 and parts[0] in _ID_PREFIXES:
        parts.pop(0)
    while len(parts) > 1 and parts[-1] in _ID_SUFFIXES:
        parts.pop()
    return "_".join(parts)


def _acronym(value: str) -> str:
    parts = [part for part in str(value or "").split("_") if part not in _ID_PREFIXES and part not in _ID_SUFFIXES]
    return "".join(part[0] for part in parts if part) if len(parts) >= 2 else str(value or "")


def _same_semantic_name(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    # Match compact abbreviations such as RA <-> right_atrium or API <->
    # application_programming_interface without a topic dictionary.
    return (
        len(a) <= 5 and _acronym(b) == a
    ) or (
        len(b) <= 5 and _acronym(a) == b
    )


def _tokens(value: Any) -> set:
    return {
        token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) > 1
    }


def _semantic_id(element: Dict[str, Any]) -> str:
    value = _slug(element.get("id"))
    if value in _GENERIC_IDS or re.fullmatch(r"(?:el|diag|element)_?\d+", value):
        return ""
    return value


def _semantic_text(element: Dict[str, Any]) -> str:
    kind = str(element.get("type", "")).lower()
    if kind == "text":
        return _slug(element.get("text"))
    if kind == "icon":
        return _slug(element.get("query"))
    return ""


def _identity_score(current: Dict[str, Any], previous: Dict[str, Any]) -> float:
    # Connector geometry belongs to the current node arrangement. Reusing it
    # after a layout change creates detached lines and floating arrowheads.
    if str(current.get("id", "")).startswith("relation_") or str(previous.get("id", "")).startswith("relation_"):
        return 0.0
    if str(current.get("type", "")).lower() != str(previous.get("type", "")).lower():
        return 0.0
    current_id, previous_id = _semantic_id(current), _semantic_id(previous)
    if _same_semantic_name(current_id, previous_id):
        return 1.0
    current_text, previous_text = _semantic_text(current), _semantic_text(previous)
    if _same_semantic_name(current_text, previous_text):
        return 0.98
    if _same_semantic_name(current_id, previous_text) or _same_semantic_name(current_text, previous_id):
        return 0.94
    # Stable id prefixes allow an object label to evolve while retaining its
    # position (for example counter_value before and after an update).
    if current_id and previous_id:
        a, b = _tokens(current_id), _tokens(previous_id)
        union = a | b
        score = len(a & b) / len(union) if union else 0.0
        if score >= 0.66:
            return 0.80 + score * 0.1
    return 0.0


def continuity_planner_instruction(scene: Dict[str, Any]) -> str:
    """Small same-call instruction; no additional LLM request is introduced."""
    state = scene.get("visual_state") if isinstance(scene.get("visual_state"), dict) else {}
    mode = str(state.get("mode", "new")).lower()
    if mode != "continue":
        return ""
    group = _slug(state.get("group")) or "continued_scene"
    keep = [str(value).strip() for value in state.get("keep", []) if str(value).strip()]
    keep_text = ", ".join(keep[:10]) or "the recurring objects named in the scene"
    return (
        "CONTINUITY CONTRACT: this scene continues visual group " + group + ". "
        "Keep these semantic objects visually stable: " + keep_text + ". "
        "Use short stable visual element ids derived from those exact object names. "
        "Keep the same layout and representation for recurring objects. "
        "Generic role words such as input, output, decision, process, result, "
        "or step are layout roles, not persistent object identities. Return every "
        "kept object as a complete object plus label in this target scene. "
        "Return the complete target scene; the local state engine preserves geometry."
    )


def _layout(plan: Dict[str, Any]) -> Tuple[float, float, float]:
    layout = plan.get("layout") if isinstance(plan.get("layout"), dict) else {}
    try:
        scale = max(1e-6, float(layout.get("scale", 1.0)))
        tx = float(layout.get("translate_x", 0.0))
        ty = float(layout.get("translate_y", 0.0))
    except (TypeError, ValueError):
        return 1.0, 0.0, 0.0
    return scale, tx, ty


def _point_from_previous(value: float, previous_offset: float, previous_scale: float,
                         current_offset: float, current_scale: float) -> float:
    displayed = value * previous_scale + previous_offset
    return (displayed - current_offset) / current_scale


def _copy_geometry(current: Dict[str, Any], previous: Dict[str, Any],
                   current_plan: Dict[str, Any], previous_plan: Dict[str, Any]) -> bool:
    kind = str(current.get("type", "")).lower()
    if kind not in _GEOMETRY_TYPES:
        return False
    ps, ptx, pty = _layout(previous_plan)
    cs, ctx, cty = _layout(current_plan)
    ratio = ps / cs

    def px(name: str) -> None:
        if name in previous:
            current[name] = _point_from_previous(float(previous[name]), ptx, ps, ctx, cs)

    def py(name: str) -> None:
        if name in previous:
            current[name] = _point_from_previous(float(previous[name]), pty, ps, cty, cs)

    try:
        if kind in {"text", "rect", "circle", "ellipse", "icon"}:
            px("x")
            py("y")
        if kind in {"line", "arrow"}:
            px("x1"); py("y1"); px("x2"); py("y2")
        for name in ("width", "height", "radius", "rx", "ry", "font_size", "stroke_width"):
            if name in previous and name in current:
                current[name] = float(previous[name]) * ratio
        for name in ("fill", "stroke", "query"):
            if name in previous:
                current[name] = copy.deepcopy(previous[name])
        if kind in {"polygon", "polyline"}:
            numbers = re.findall(r"[-+]?(?:\d*\.\d+|\d+)", str(previous.get("points", "")))
            values = [float(value) for value in numbers]
            if len(values) < 4:
                return False
            transformed = []
            for index in range(0, len(values) - 1, 2):
                transformed.append(
                    f"{_point_from_previous(values[index], ptx, ps, ctx, cs):.3f},"
                    f"{_point_from_previous(values[index + 1], pty, ps, cty, cs):.3f}"
                )
            current["points"] = " ".join(transformed)
        current["persistent"] = True
        return True
    except (TypeError, ValueError):
        return False


def _matches_keep(element: Dict[str, Any], keep: List[str]) -> bool:
    if str(element.get("id", "")).startswith("relation_"):
        return False
    if not keep:
        return False
    return any(_matches_one_keep(element, value) for value in keep)


def _matches_one_keep(element: Dict[str, Any], keep_value: str) -> bool:
    semantic = " ".join(str(element.get(key, "")) for key in ("id", "text", "query"))
    wanted = _tokens(keep_value)
    if wanted and wanted <= _tokens(semantic):
        return True
    wanted_name = _slug(keep_value)
    return any(
        _same_semantic_name(wanted_name, candidate)
        for candidate in (_semantic_id(element), _semantic_text(element))
        if candidate
    )


def _is_subject_representation(element: Dict[str, Any]) -> bool:
    kind = str(element.get("type", "")).lower()
    # Support surfaces count as drawable components for gate purposes so the
    # existing orphan-support suppression can safely decide not to carry them.
    # Plain labels and connectors never count by themselves.
    return kind not in _NON_SUBJECT_TYPES


def _continuity_gate(
    keep: List[str], previous_elements: List[Dict[str, Any]], current_elements: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Require complete recurring objects, not isolated matching labels.

    A model-provided group name is only a hint. Every kept semantic object must
    exist as a drawable subject in the previous composition. If it appears in
    the new composition, it must again have a drawable subject; a matching text
    label by itself cannot authorize geometry reuse.
    """
    rows = []
    for value in keep:
        previous_matches = [
            element for element in previous_elements
            if isinstance(element, dict) and _matches_one_keep(element, value)
        ]
        current_matches = [
            element for element in current_elements
            if isinstance(element, dict) and _matches_one_keep(element, value)
        ]
        previous_subjects = [element for element in previous_matches if _is_subject_representation(element)]
        current_subjects = [element for element in current_matches if _is_subject_representation(element)]
        wanted_name = _slug(value)
        generic_role = wanted_name in _GENERIC_ROLE_NAMES
        # Continuity is geometry reuse, not element injection. Both plans must
        # independently contain the complete concrete object. This prevents a
        # generic overview box from being carried into a detailed composition.
        valid = not generic_role and bool(previous_subjects) and bool(current_subjects)
        rows.append({
            "object": value,
            "previous_match_count": len(previous_matches),
            "previous_subject_count": len(previous_subjects),
            "current_match_count": len(current_matches),
            "current_subject_count": len(current_subjects),
            "valid": valid,
            "generic_role": generic_role,
            "reason": (
                "complete_component"
                if valid else
                "generic_role_is_not_object_identity"
                if generic_role else
                "missing_previous_subject"
                if not previous_subjects else
                "missing_current_subject"
                if not current_subjects else
                "current_label_without_subject"
            ),
        })
    rejected = [row["object"] for row in rows if not row["valid"]]
    return {
        "evaluated": bool(keep),
        "passed": bool(keep) and not rejected,
        "objects": rows,
        "rejected_objects": rejected,
    }


def _current_represents_kept_object(
    previous: Dict[str, Any], current_elements: List[Dict[str, Any]], keep: List[str]
) -> bool:
    previous_tokens = _tokens(" ".join(str(previous.get(key, "")) for key in ("id", "text", "query")))
    matching_keep = [_tokens(value) for value in keep if _tokens(value) and _tokens(value) <= previous_tokens]
    if not matching_keep:
        return False
    previous_is_text = str(previous.get("type", "")).lower() == "text"
    for current in current_elements:
        if not isinstance(current, dict):
            continue
        if (str(current.get("type", "")).lower() == "text") != previous_is_text:
            continue
        current_tokens = _tokens(" ".join(str(current.get(key, "")) for key in ("id", "text", "query")))
        if any(tokens <= current_tokens for tokens in matching_keep):
            return True
    return False


def _is_support_shape(element: Dict[str, Any]) -> bool:
    """True only for explicitly named visual support, never subject geometry."""
    if str(element.get("type", "")).lower() not in {"rect", "circle", "ellipse"}:
        return False
    element_id = re.sub(r"[^a-z0-9]+", "_", str(element.get("id", "")).lower())
    tokens = set(filter(None, element_id.split("_")))
    return bool(tokens & (_SUPPORT_TOKENS | _ENVIRONMENT_SUPPORT_NAMES))


def _support_semantic_root(element: Dict[str, Any]) -> set:
    tokens = _tokens(" ".join(str(element.get(key, "")) for key in ("id", "text", "query")))
    return tokens - _SUPPORT_TOKENS


def _current_has_kept_semantics(
    previous: Dict[str, Any], current_elements: List[Dict[str, Any]], keep: List[str]
) -> bool:
    """Allow a label/icon to replace an old support surface as representation.

    A background carrying the same semantic name is not an independent object.
    Carrying it after the current scene already contains the named concept
    creates the empty overlay seen in the GDP test.
    """
    if not _is_support_shape(previous):
        return False
    previous_tokens = _tokens(" ".join(str(previous.get(key, "")) for key in ("id", "text", "query")))
    matching_keep = [_tokens(value) for value in keep if _tokens(value) and _tokens(value) <= previous_tokens]
    if not matching_keep:
        return False
    previous_root = _support_semantic_root(previous)
    for current in current_elements:
        if not isinstance(current, dict) or str(current.get("type", "")).lower() in {"line", "arrow"}:
            continue
        current_tokens = _tokens(" ".join(str(current.get(key, "")) for key in ("id", "text", "query")))
        if any(tokens <= current_tokens for tokens in matching_keep):
            return True
        current_root = _support_semantic_root(current)
        if previous_root and current_root and (
            previous_root <= current_root or current_root <= previous_root
        ):
            return True
    return False


def reconcile_visual_plan(
    scene: Dict[str, Any],
    current_plan: Dict[str, Any],
    previous_scene_id: Optional[int],
    previous_plan: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    plan = copy.deepcopy(current_plan)
    state = scene.get("visual_state") if isinstance(scene.get("visual_state"), dict) else {}
    mode = str(state.get("mode", "new")).lower()
    group = _slug(state.get("group")) or f"scene_{scene.get('id', 0)}"
    keep = [str(value).strip() for value in state.get("keep", []) if str(value).strip()][:10]
    diagnostic = {
        "declared_mode": mode if mode in {"new", "continue"} else "new",
        "group": group,
        "base_scene_id": previous_scene_id,
        "keep": keep,
        "applied": False,
        "reused_element_count": 0,
        "carried_element_count": 0,
        "reused_ids": [],
        "carried_ids": [],
        "duplicate_carries_suppressed": [],
        "orphan_support_carries_suppressed": [],
        "carried_layer_reorders": 0,
        "stable_identity_style_count": 0,
        "stable_identity_style_ids": [],
        "continuity_gate": {},
        "reason": "new_visual_state",
    }
    if mode != "continue" or previous_plan is None:
        if mode == "continue" and previous_plan is None:
            diagnostic["reason"] = "continuity_base_unavailable"
        plan["_continuity"] = diagnostic
        return plan

    previous_elements = previous_plan.get("elements") if isinstance(previous_plan.get("elements"), list) else []
    current_elements = plan.get("elements") if isinstance(plan.get("elements"), list) else []
    previous_storyboard = (
        previous_plan.get("_storyboard")
        if isinstance(previous_plan.get("_storyboard"), dict) else {}
    )
    current_storyboard = (
        plan.get("_storyboard") if isinstance(plan.get("_storyboard"), dict) else {}
    )
    previous_family = str(
        previous_storyboard.get("layout") or previous_plan.get("diagram_kind")
        or previous_plan.get("source") or "freeform"
    ).strip().lower()
    current_family = str(
        current_storyboard.get("layout") or plan.get("diagram_kind")
        or plan.get("source") or "freeform"
    ).strip().lower()
    structured_families = {
        "flow", "cycle", "comparison", "timeline", "hierarchy", "grid", "focus",
        "compare", "tree", "map", "diagram", "semantic_storyboard",
    }
    if (
        previous_family in structured_families
        and current_family in structured_families
        and previous_family != current_family
    ):
        diagnostic["reason"] = "continuity_rejected_representation_family_incompatible"
        diagnostic["continuity_gate"] = {
            "evaluated": True, "passed": False,
            "previous_representation_family": previous_family,
            "current_representation_family": current_family,
        }
        plan["_continuity"] = diagnostic
        return plan
    previous_canvas = previous_plan.get("canvas") if isinstance(previous_plan.get("canvas"), dict) else {}
    current_canvas = plan.get("canvas") if isinstance(plan.get("canvas"), dict) else {}
    previous_space = (previous_canvas.get("width"), previous_canvas.get("height"))
    current_space = (current_canvas.get("width"), current_canvas.get("height"))
    if all(previous_space) and all(current_space) and previous_space != current_space:
        diagnostic["reason"] = "continuity_rejected_coordinate_system_incompatible"
        diagnostic["continuity_gate"] = {
            "evaluated": True, "passed": False,
            "previous_canvas": previous_space, "current_canvas": current_space,
        }
        plan["_continuity"] = diagnostic
        return plan
    gate = _continuity_gate(keep, previous_elements, current_elements)
    diagnostic["continuity_gate"] = gate
    if not gate["passed"]:
        diagnostic["reason"] = (
            "continuity_rejected_no_complete_keep_objects"
            if keep else "continuity_rejected_empty_keep"
        )
        plan["_continuity"] = diagnostic
        return plan
    used_previous = set()
    reused_indices = set()

    # A stable subject may legitimately change representation as its state
    # changes (shape -> particles -> icon). Preserve its visual identity token
    # even when geometry cannot be copied across element types.
    for current in current_elements:
        if not isinstance(current, dict) or not _matches_keep(current, keep):
            continue
        if str(current.get("type", "")).lower() in _NON_SUBJECT_TYPES:
            continue
        current_id = _semantic_id(current)
        if not current_id:
            continue
        matches = [
            previous for previous in previous_elements
            if isinstance(previous, dict)
            and _matches_keep(previous, keep)
            and _semantic_id(previous)
            and _same_semantic_name(current_id, _semantic_id(previous))
        ]
        if not matches:
            continue
        previous = matches[0]
        changed = False
        for name in ("fill", "stroke"):
            value = previous.get(name)
            if value not in {None, "", "none", "transparent"}:
                current[name] = copy.deepcopy(value)
                changed = True
        if changed:
            diagnostic["stable_identity_style_count"] += 1
            diagnostic["stable_identity_style_ids"].append(str(current.get("id", ""))[:80])

    for current_index, current in enumerate(current_elements):
        if not isinstance(current, dict):
            continue
        if not _matches_keep(current, keep):
            continue
        candidates = []
        for previous_index, previous in enumerate(previous_elements):
            if previous_index in used_previous or not isinstance(previous, dict):
                continue
            if not _matches_keep(previous, keep):
                continue
            score = _identity_score(current, previous)
            if score:
                candidates.append((score, previous_index, previous))
        if not candidates:
            continue
        _, previous_index, previous = max(candidates, key=lambda item: item[0])
        if _copy_geometry(current, previous, plan, previous_plan):
            used_previous.add(previous_index)
            reused_indices.add(current_index)
            diagnostic["reused_ids"].append(
                str(current.get("id") or current.get("text") or current.get("query") or current_index)[:80]
            )

    # Never inject objects that the current scene did not draw. Continuity is
    # limited to geometry reconciliation between two independently complete
    # representations; missing objects are reported instead of carried.
    for previous_index, previous in enumerate(previous_elements):
        if previous_index in used_previous or len(current_elements) >= MAX_ELEMENTS:
            continue
        if not isinstance(previous, dict) or not _matches_keep(previous, keep):
            continue
        if _current_has_kept_semantics(previous, current_elements, keep):
            diagnostic["orphan_support_carries_suppressed"].append(
                str(previous.get("id") or previous_index)[:80]
            )
            continue
        if _current_represents_kept_object(previous, current_elements, keep):
            diagnostic["duplicate_carries_suppressed"].append(
                str(previous.get("id") or previous.get("text") or previous_index)[:80]
            )
            continue
        diagnostic.setdefault("missing_object_carries_disabled", []).append(
            str(previous.get("id") or previous.get("text") or previous_index)[:80]
        )

    # Persistent elements are already visible at the cut, so do not replay
    # their entrance animations. Existing indices remain stable because carried
    # elements are appended after the planner's original list.
    animations = plan.get("animations")
    if isinstance(animations, list) and reused_indices:
        plan["animations"] = [
            animation for animation in animations
            if not isinstance(animation, dict) or int(animation.get("e", -1)) not in reused_indices
        ]

    # Carried elements used to be appended after labels/icons, so an opaque
    # support surface could paint over the entire current scene. Re-establish
    # layer order and place support surfaces before objects at the same layer.
    indexed_elements = list(enumerate(current_elements))
    ordered = sorted(
        indexed_elements,
        key=lambda item: (
            int(item[1].get("layer", 2)),
            0 if _is_support_shape(item[1]) else 1,
            item[0],
        ),
    )
    old_to_new = {old_index: new_index for new_index, (old_index, _) in enumerate(ordered)}
    diagnostic["carried_layer_reorders"] = sum(
        1 for new_index, (old_index, _) in enumerate(ordered) if new_index != old_index
    )
    current_elements = [element for _, element in ordered]
    animations = plan.get("animations")
    if isinstance(animations, list):
        for animation in animations:
            if not isinstance(animation, dict):
                continue
            try:
                old_index = int(animation.get("e", -1))
            except (TypeError, ValueError):
                continue
            if old_index in old_to_new:
                animation["e"] = old_to_new[old_index]

    diagnostic["reused_element_count"] = len(diagnostic["reused_ids"])
    diagnostic["carried_element_count"] = len(diagnostic["carried_ids"])
    semantic_matches = diagnostic["reused_element_count"] + diagnostic["carried_element_count"]
    diagnostic["applied"] = semantic_matches > 0
    diagnostic["reason"] = "geometry_reconciled" if semantic_matches else "no_semantic_match"
    plan["elements"] = current_elements
    plan["_continuity"] = diagnostic
    return plan


class ContinuityCoordinator:
    """Commit parallel visual plans in scene order without serializing LLM calls."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: Dict[int, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        self._resolved: Dict[int, Dict[str, Any]] = {}
        self._committed_ids: set[int] = set()
        self._next_scene_id = 1
        self._groups: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self._last_group: Optional[str] = None
        self._last_committed: Tuple[Optional[int], Optional[Dict[str, Any]]] = (None, None)

    def submit(self, scene: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
        scene_id = int(scene.get("id", 0))
        deadline = time.monotonic() + WAIT_SECONDS
        with self._condition:
            state = scene.get("visual_state") if isinstance(scene.get("visual_state"), dict) else {}
            mode = str(state.get("mode", "new")).lower()
            # Independent scenes have no prior geometry dependency. Resolve
            # them immediately so a slow earlier scene cannot hold every later
            # scene behind an ordered continuity barrier.
            if mode != "continue":
                group = _slug(state.get("group")) or f"scene_{scene_id}"
                resolved = reconcile_visual_plan(scene, plan, None, None)
                self._resolved[scene_id] = resolved
                self._groups[group] = (scene_id, resolved)
                self._committed_ids.add(scene_id)
            else:
                self._pending[scene_id] = (scene, plan)
            while self._next_scene_id in self._committed_ids:
                self._next_scene_id += 1
            while self._next_scene_id in self._pending:
                current_scene, current_plan = self._pending.pop(self._next_scene_id)
                state = current_scene.get("visual_state") if isinstance(current_scene.get("visual_state"), dict) else {}
                group = _slug(state.get("group")) or f"scene_{self._next_scene_id}"
                previous_id, previous_plan = self._groups.get(group, (None, None))
                mode = str(state.get("mode", "new")).lower()
                # An unknown continuation group is unsafe. Falling back to the
                # previous scene can merge unrelated compositions merely
                # because they are adjacent, so it is treated as a new state.
                resolved = reconcile_visual_plan(
                    current_scene, current_plan, previous_id, previous_plan
                )
                self._resolved[self._next_scene_id] = resolved
                self._groups[group] = (self._next_scene_id, resolved)
                self._last_group = group
                self._last_committed = (self._next_scene_id, resolved)
                self._committed_ids.add(self._next_scene_id)
                self._next_scene_id += 1
                while self._next_scene_id in self._committed_ids:
                    self._next_scene_id += 1
            self._condition.notify_all()
            while scene_id not in self._resolved:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fallback = copy.deepcopy(plan)
                    fallback["_continuity"] = {
                        "declared_mode": "new",
                        "group": _slug(scene.get("visual_state", {}).get("group")),
                        "base_scene_id": None,
                        "keep": [],
                        "applied": False,
                        "reused_element_count": 0,
                        "carried_element_count": 0,
                        "reused_ids": [],
                        "carried_ids": [],
                        "orphan_support_carries_suppressed": [],
                        "carried_layer_reorders": 0,
                        "reason": "coordinator_timeout",
                    }
                    return fallback
                self._condition.wait(timeout=min(remaining, 2.0))
            return self._resolved.pop(scene_id)


__all__ = [
    "ContinuityCoordinator",
    "continuity_planner_instruction",
    "reconcile_visual_plan",
]
