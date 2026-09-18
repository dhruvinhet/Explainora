"""Contract-preserving visual candidate utilities.

This module is deliberately topic agnostic.  It canonicalizes common model
schemas, scores candidates against semantic evidence, and builds a deterministic
entity/relation fallback without turning the visual instruction into a text card.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from storyboard_engine import build_storyboard_scene
from explanation_quality import assess_visual_anchor_coverage


SCHEMA_VERSION = "visual-plan-v2"
ALIASES = {
    "fontSize": "font_size", "fontWeight": "font_weight",
    "fontFamily": "font_family", "textAnchor": "align", "text_anchor": "align",
    "anchor": "align", "cx": "x", "cy": "y", "r": "radius",
    "content": "text", "markerEnd": "marker_end", "marker-end": "marker_end",
    "strokeWidth": "stroke_width", "strokeDasharray": "dash",
    "semanticRefs": "semantic_refs", "fromId": "from_id", "toId": "to_id",
    "ownerId": "owner_id", "labelFor": "label_for", "targetIds": "target_ids",
}
KNOWN_ELEMENT_FIELDS = {
    "type", "id", "x", "y", "x1", "y1", "x2", "y2", "width", "height",
    "radius", "rx", "ry", "d", "points", "text", "query", "fill", "stroke",
    "stroke_width", "dash", "opacity", "rotation", "layer", "font_size",
    "font_weight", "font_family", "align", "semantic_refs", "from_id", "to_id",
    "owner_id", "label_for", "persistent", "pose", "minor_annotation",
    "marker_end",
}


def _slug(value: Any, fallback: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return (result or fallback)[:80]


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _truncate_words(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    while re.search(r"\b(?:and|or|to|of|in|for|with|by|from|as|the|a|an)$", clipped, re.I):
        clipped = clipped.rsplit(" ", 1)[0].rstrip(" ,;:-")
    return clipped or text[:limit].rstrip(" ,;:-")


def canonicalize_visual_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Translate common SVG/camelCase output without losing the raw plan."""
    if not isinstance(plan, dict):
        raise ValueError("Visual plan must be an object.")
    result = copy.deepcopy(plan)
    diagnostics = {
        "schema_version": SCHEMA_VERSION, "translated_fields": [],
        "generated_ids": [], "unknown_fields": [], "input_hash": _hash(plan),
    }
    elements = result.get("elements")
    if not isinstance(elements, list):
        result["_canonicalization"] = diagnostics
        return result

    used = set()
    nested_labels: List[Dict[str, Any]] = []
    # Iterate over a snapshot because SVG-style nested labels are materialized
    # as first-class text elements after their owning shape is canonicalized.
    for index, element in enumerate(list(elements)):
        if not isinstance(element, dict):
            continue
        original_kind = str(element.get("type", "")).strip().lower()
        has_arrow_marker = any(
            str(element.get(key, "")).strip()
            for key in ("markerEnd", "marker-end", "marker_end")
        )
        for old, new in ALIASES.items():
            if old in element and new not in element:
                element[new] = element.pop(old)
                diagnostics["translated_fields"].append(f"elements[{index}].{old}->{new}")
        if element.get("align") in {"middle", "center"}:
            element["align"] = "center"
        elif element.get("align") in {"end", "right"}:
            element["align"] = "right"
        elif element.get("align") in {"start", "left"}:
            element["align"] = "left"
        refs = element.get("semantic_refs")
        if isinstance(refs, str):
            element["semantic_refs"] = [refs]
        base = element.get("id")
        if not str(base or "").strip():
            ref = next(iter(element.get("semantic_refs") or []), "")
            hint = element.get("text") or element.get("query") or ref or element.get("type")
            base = _slug(hint, f"element_{index + 1}")
            diagnostics["generated_ids"].append(base)
        candidate, suffix = _slug(base, f"element_{index + 1}"), 2
        original = candidate
        while candidate in used:
            candidate = f"{original}_{suffix}"
            suffix += 1
        element["id"] = candidate
        used.add(candidate)
        if original_kind == "line" and has_arrow_marker:
            element["type"] = "arrow"

        raw_label = element.pop("label", None)
        if isinstance(raw_label, str):
            raw_label = {"text": raw_label}
        if isinstance(raw_label, dict) and str(
            raw_label.get("text") or raw_label.get("content") or ""
        ).strip():
            label = copy.deepcopy(raw_label)
            for old, new in ALIASES.items():
                if old in label and new not in label:
                    label[new] = label.pop(old)
                    diagnostics["translated_fields"].append(
                        f"elements[{index}].label.{old}->{new}"
                    )
            label["type"] = "text"
            label["text"] = str(label.get("text", "")).strip()
            if label.get("align") in {"middle", "center"}:
                label["align"] = "center"
            elif label.get("align") in {"end", "right"}:
                label["align"] = "right"
            elif label.get("align") in {"start", "left"}:
                label["align"] = "left"
            label.setdefault("x", element.get("x", element.get("x2", 100)))
            label.setdefault(
                "y", float(element.get("y", element.get("y2", 100)) or 100)
                + float(element.get("radius", 0) or 0) + 34,
            )
            label.setdefault("font_size", 26)
            label.setdefault("align", "center")
            label.setdefault("layer", max(3, int(element.get("layer", 1) or 1) + 1))
            label.setdefault("semantic_refs", copy.deepcopy(element.get("semantic_refs", [])))
            label["owner_id"] = candidate
            label["label_for"] = candidate
            label_base = _slug(label.get("id"), f"{candidate}_label")
            label_id, suffix = label_base, 2
            while label_id in used:
                label_id = f"{label_base}_{suffix}"
                suffix += 1
            label["id"] = label_id
            used.add(label_id)
            nested_labels.append(label)
        for field_name in element:
            if field_name not in KNOWN_ELEMENT_FIELDS and not field_name.startswith("__"):
                diagnostics["unknown_fields"].append(f"elements[{index}].{field_name}")

    elements.extend(nested_labels)

    by_id = {str(element.get("id")): element for element in elements if isinstance(element, dict)}
    for element in elements:
        if not isinstance(element, dict) or element.get("type") != "text":
            continue
        if element.get("owner_id") or element.get("label_for"):
            continue
        element_id = str(element.get("id", ""))
        candidates = []
        for prefix in ("label_", "text_"):
            if element_id.startswith(prefix):
                candidates.append(element_id[len(prefix):])
        for suffix in ("_label", "_text", "_title"):
            if element_id.endswith(suffix):
                candidates.append(element_id[:-len(suffix)])
        for candidate in candidates:
            matches = [key for key in by_id if key == candidate or key.endswith("_" + candidate)]
            target = next((key for key in matches if by_id[key].get("type") != "text"), None)
            if target:
                element["owner_id"] = target
                element["label_for"] = target
                break

    animations = result.get("animations")
    if isinstance(animations, list):
        for index, animation in enumerate(animations):
            if not isinstance(animation, dict):
                continue
            for old, new in ALIASES.items():
                if old in animation and new not in animation:
                    animation[new] = animation.pop(old)
                    diagnostics["translated_fields"].append(f"animations[{index}].{old}->{new}")
            if "target_ids" in animation and "target_id" not in animation:
                targets = animation.get("target_ids")
                if isinstance(targets, list) and targets:
                    animation["target_id"] = targets[0]
    diagnostics["output_hash"] = _hash(result)
    result["schema_version"] = SCHEMA_VERSION
    result["_canonicalization"] = diagnostics
    return result


def semantic_fallback_plan(text: str, constraints: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compile a provider-independent evidence diagram.

    This is the final availability safety net.  It deliberately uses pictured
    objects and labelled relationships; printing the names of requested
    visuals inside a grid of cards is not an explanation.
    """
    contract = constraints if isinstance(constraints, dict) else {}
    entities = [str(value).strip() for value in contract.get("required_entities", []) if str(value).strip()]
    facts = [str(value).strip() for value in contract.get("state_facts", []) if str(value).strip()]
    if not entities:
        clauses = [part.strip(" .") for part in re.split(r"\bthen\b|[.;]", str(text), flags=re.I) if part.strip()]
        entities = clauses[:6] or ["Main idea"]
    refs = contract.get("constraint_reference_map") if isinstance(contract.get("constraint_reference_map"), dict) else {}
    reverse_refs = {str(value): str(key) for key, value in refs.items()}
    anchors = [str(value).strip() for value in contract.get("exact_anchors", []) if str(value).strip()]
    # A rescue scene stays deliberately small so labels remain readable.  The
    # rich planner is responsible for more elaborate compositions.
    entities = entities[:6]
    if len(entities) < 2:
        state_candidates = [
            str(contract.get("before_state", "")).strip(),
            str(contract.get("after_state", "")).strip(),
        ]
        for value in state_candidates:
            if value and value.casefold() not in {item.casefold() for item in entities}:
                entities.append(value)
            if len(entities) >= 2:
                break
    if len(entities) < 2 and facts:
        entities.append(facts[0])

    items = []
    for index, entity in enumerate(entities):
        label = re.sub(r"[_-]+", " ", entity).strip().title()
        item = {
            "id": _slug(entity, f"entity_{index + 1}"), "label": _truncate_words(label, 42),
            "kind": "object", "representation": "ellipse",
            "emphasis": index == 0,
        }
        ref = reverse_refs.get(entity) or f"E{index + 1}"
        item["semantic_refs"] = [ref] if ref else []
        items.append(item)
    visible_labels = " ".join(str(item.get("label", "")) for item in items).casefold()
    for index, anchor in enumerate(anchors):
        if anchor.casefold() in visible_labels or len(items) >= 10:
            continue
        items.append({
            "id": f"anchor_{index + 1}", "label": _truncate_words(anchor, 42),
            "kind": "evidence", "representation": "pill",
            "semantic_refs": [], "emphasis": False,
        })
    claim = str(contract.get("claim", "")).strip()
    evidence = " ".join(str(contract.get(key, "")) for key in (
        "visual_evidence", "before_state", "after_state"
    )).casefold()
    if any(word in evidence for word in ("before", "after", "versus", "compare", "split")):
        layout = "comparison"
    elif any(word in evidence for word in ("cycle", "loop", "circulat", "repeat")) and len(items) > 2:
        layout = "cycle"
    else:
        layout = "flow" if len(items) > 1 else "focus"

    relations = []
    if len(items) > 1:
        # visual_action is an instruction to the renderer, never viewer-facing
        # copy.  Showing it produces labels such as "Overlay equation...".
        relation_facts = facts or [claim or "relates to"]
        edge_count = len(items) if layout == "cycle" and len(items) > 2 else len(items) - 1
        for index in range(edge_count):
            fact = relation_facts[index] if index < len(relation_facts) else ""
            relations.append({
                "from": items[index]["id"],
                "to": items[(index + 1) % len(items)]["id"],
                "label": _truncate_words(fact, 30),
                "kind": "arrow",
                "semantic_refs": [f"F{index + 1}"] if index < len(facts) else [],
            })
        # Preserve every state-fact reference without drawing duplicate or reverse
        # connectors on top of the small deterministic rescue diagram.
        for index in range(edge_count, len(facts)):
            relation = relations[index % len(relations)]
            relation.setdefault("semantic_refs", []).append(f"F{index + 1}")
    if claim and len(claim) <= 72:
        fallback_title = claim
    elif len(entities) >= 2:
        fallback_title = f"{_truncate_words(entities[0], 28)} and {_truncate_words(entities[1], 28)}"
    elif entities:
        fallback_title = _truncate_words(entities[0], 60)
    else:
        fallback_title = "Visual explanation"
    storyboard = {
        "title": fallback_title,
        "layout": layout,
        "items": items,
        "relations": relations,
    }
    result = build_storyboard_scene(storyboard)
    result["source"] = "semantic_fallback"
    result["_semantic_constraints"] = copy.deepcopy(contract)
    result["_fallback"] = {
        "strategy": "illustrated_evidence_rescue", "silent": True,
        "reason": "all richer candidates exhausted", "instruction_text_rendered": False,
    }
    return canonicalize_visual_plan(result)


def validate_chart_spec(
    plan: Dict[str, Any], constraints: Optional[Dict[str, Any]] = None,
) -> None:
    """Reject quantitative whole charts whose proportions are not authorized."""
    chart = plan.get("chart") or plan.get("chart_spec")
    if not isinstance(chart, dict):
        return
    kind = str(chart.get("kind") or chart.get("type") or "").strip().lower()
    if kind not in {"pie", "donut", "doughnut"}:
        return
    rows = chart.get("values") or chart.get("segments") or []
    values: List[float] = []
    raw_values: List[str] = []
    for row in rows if isinstance(rows, list) else []:
        raw = row.get("value") if isinstance(row, dict) else row
        raw_values.append(str(raw).strip())
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            raise ValueError("Malformed pie/donut chart: every segment needs a numeric value.")
    if len(values) < 2 or any(value < 0 for value in values):
        raise ValueError("Malformed pie/donut chart: at least two non-negative segments are required.")
    total = sum(values)
    if not (abs(total - 1.0) <= 0.005 or abs(total - 100.0) <= 0.05):
        raise ValueError("Malformed pie/donut chart: authorized segments must form one complete whole.")
    contract = constraints if isinstance(constraints, dict) else {}
    authorized = {
        re.sub(r"\s+", "", str(value)).casefold()
        for value in contract.get("authorized_quantities", [])
        if str(value).strip()
    }
    if authorized and any(
        re.sub(r"\s+", "", value).casefold() not in authorized
        and re.sub(r"\s+", "", value + "%").casefold() not in authorized
        for value in raw_values
    ):
        raise ValueError(
            "Malformed pie/donut chart: one or more proportions are not authorized by the semantic contract."
        )


def candidate_score(plan: Dict[str, Any], constraints: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    contract = constraints if isinstance(constraints, dict) else {}
    elements = [e for e in plan.get("elements", []) if isinstance(e, dict)]
    required = [str(v) for v in contract.get("required_entities", []) if str(v).strip()]
    state_facts = [str(v) for v in contract.get("state_facts", []) if str(v).strip()]
    anchors = [str(v) for v in contract.get("exact_anchors", []) if str(v).strip()]
    coverage = assess_visual_anchor_coverage(
        plan, {"id": None, "semantic_contract": contract}
    )
    required_hits = len(required) - len(coverage.get("missing_required_entities", []))
    fact_hits = len(state_facts) - len(coverage.get("missing_state_facts", []))
    visible = " ".join(str(e.get("text", "")) + " " + str(e.get("id", "")) for e in elements).casefold()
    anchor_hits = sum(1 for value in anchors if value.casefold() in visible)
    repair = plan.get("_freeform_repair") if isinstance(plan.get("_freeform_repair"), dict) else {}
    hard_issues = sum(int(repair.get(name, 0) or 0) for name in (
        "unresolved_text_overlap_count", "unresolved_cross_type_collision_count",
        "foreground_occlusion_count", "unresolved_detached_label_count",
    ))
    gate = plan.get("_quality_gate") if isinstance(plan.get("_quality_gate"), dict) else {}
    hard_issues += len(gate.get("blocking_issues", []) or [])
    score = 20 + min(20, len(elements) * 2) + required_hits * 12 + fact_hits * 10 + anchor_hits * 8 - hard_issues * 10
    if plan.get("source") == "semantic_fallback":
        # A renderable emergency diagram is preferable to a broken frame, but
        # absence of exceptions is not visual quality. Keep enough penalty that
        # a shallow icon row cannot beat a rich, semantically equivalent scene
        # by one point merely because the fallback owns fewer objects.
        score -= 20
    return {
        "score": max(0, min(100, int(score))), "element_count": len(elements),
        "required_coverage": required_hits / max(1, len(required)) if required else 1.0,
        "state_fact_coverage": fact_hits / max(1, len(state_facts)) if state_facts else 1.0,
        "anchor_coverage": anchor_hits / max(1, len(anchors)) if anchors else 1.0,
        "hard_issue_count": hard_issues, "renderable": bool(elements),
    }


@dataclass
class CandidateRegistry:
    constraints: Dict[str, Any]
    candidates: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, candidate_id: str, stage: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        frozen = copy.deepcopy(plan)
        quality = candidate_score(frozen, self.constraints)
        record = {"candidate_id": candidate_id, "stage": stage, "plan": frozen, "quality": quality}
        self.candidates.append(record)
        return record

    def best(self) -> Optional[Dict[str, Any]]:
        eligible = [item for item in self.candidates if item["quality"].get("renderable")]
        # Semantic evidence is the reason the scene exists. A sparse fallback
        # must not replace a rich candidate merely because it owns fewer
        # objects and therefore triggers fewer geometry counters. Prefer a
        # hard-gate pass only among candidates with equivalent semantic
        # completeness; otherwise keep the most complete renderable candidate
        # as the last resort and preserve its composition for targeted repair.
        def selection_key(item: Dict[str, Any]) -> Any:
            plan = item.get("plan", {})
            gate = plan.get("_quality_gate") if isinstance(plan, dict) else {}
            gate = gate if isinstance(gate, dict) else {}
            quality = item["quality"]
            passes_hard_gate = gate.get("status") != "fail" and not gate.get("blocking_issues")
            semantic_coverage = min(
                float(quality.get("required_coverage", 0)),
                float(quality.get("state_fact_coverage", 0)),
                float(quality.get("anchor_coverage", 0)),
            )
            semantic_complete = semantic_coverage >= 0.999
            return (
                int(semantic_complete),
                int(passes_hard_gate),
                -int(quality.get("hard_issue_count", 0)),
                semantic_coverage,
                float(quality.get("score", 0)),
            )

        if not eligible:
            return None
        passing_complete = []
        rich_candidates = []
        for item in eligible:
            plan = item.get("plan", {})
            gate = plan.get("_quality_gate") if isinstance(plan, dict) else {}
            gate = gate if isinstance(gate, dict) else {}
            quality = item["quality"]
            semantic_coverage = min(
                float(quality.get("required_coverage", 0)),
                float(quality.get("state_fact_coverage", 0)),
                float(quality.get("anchor_coverage", 0)),
            )
            if gate.get("status") == "pass" and not gate.get("blocking_issues") and semantic_coverage >= 0.999:
                passing_complete.append(item)
            if (
                plan.get("source") != "semantic_fallback"
                and "fallback" not in str(item.get("stage", "")).casefold()
            ):
                rich_candidates.append(item)
        # Once a complete candidate passes the publish gate, a more elaborate
        # candidate with known overlap, clipping, or unsupported facts cannot
        # outrank it merely by containing more objects.
        passing_rich = [
            item for item in passing_complete
            if item.get("plan", {}).get("source") != "semantic_fallback"
        ]
        if passing_rich:
            return max(passing_rich, key=selection_key)
        # A drawable provider-created composition is always retained after the
        # bounded repair attempts. Semantic or geometry diagnostics may lower
        # its rank among rich candidates, but can never replace it with a
        # generic emergency storyboard. The fallback remains available only
        # when no provider candidate contains renderable elements at all.
        if rich_candidates:
            return max(rich_candidates, key=selection_key)
        return max(passing_complete or eligible, key=selection_key)


__all__ = [
    "CandidateRegistry", "SCHEMA_VERSION", "candidate_score",
    "canonicalize_visual_plan", "semantic_fallback_plan", "validate_chart_spec",
]
