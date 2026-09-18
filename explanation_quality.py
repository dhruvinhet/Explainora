"""Token-free normalization and diagnostics for instructional video plans.

The director still makes one planning call. This module turns that response
into an explicit teaching contract that downstream visual planners and the
downloadable evaluation report can inspect. It deliberately contains no topic
or audience-name lookup tables.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


def _text(value: Any, fallback: str = "", limit: int = 500) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    text = cleaned or fallback
    if len(text) <= limit:
        return text
    clipped = text[:limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return clipped or text[:limit].rstrip(" ,;:-")


def _unique_texts(values: Any, *, limit: int = 12, item_limit: int = 240) -> List[str]:
    if not isinstance(values, list):
        return []
    result: List[str] = []
    seen = set()
    for value in values:
        cleaned = _text(value, limit=item_limit)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            result.append(cleaned)
            seen.add(key)
        if len(result) >= limit:
            break
    return result


def duration_teaching_contract(
    target_seconds: float,
    scene_count: int,
    target_words: int,
) -> Dict[str, Any]:
    """Describe continuous teaching capacity without assuming a fixed runtime."""
    count = max(1, int(scene_count))
    seconds = max(1.0, float(target_seconds))
    words = max(1, int(target_words))
    opportunities: List[str] = [
        "motivate the learner with the question, problem, or consequence",
        "establish a clear mental model",
        "show the mechanism or reasoning",
        "synthesize the answer into a transferable takeaway",
    ]
    if count >= 4:
        opportunities.insert(3, "work through one concrete example or demonstration")
    if count >= 6:
        opportunities.insert(-1, "address a likely misconception, limitation, or decision boundary")
    if count >= 8:
        opportunities.insert(-1, "deepen the same idea with a second case, edge case, or application")
    return {
        "strategy": "continuous_duration_capacity",
        "target_seconds": round(seconds, 3),
        "target_words": words,
        "planned_scene_count": count,
        "average_scene_seconds": round(seconds / count, 3),
        "depth_opportunities": opportunities,
        "rules": [
            "Merge teaching roles when capacity is short; never end mid-explanation.",
            "Use added capacity for intuition, evidence, examples, misconceptions, application, and recap.",
            "Do not add adjacent topics merely to fill a longer duration.",
            "Prefer fewer ideas taught clearly over an encyclopedic checklist taught superficially.",
        ],
    }


def _normalize_audience_profile(plan: Dict[str, Any], audience: str) -> Dict[str, Any]:
    raw = plan.get("audience_profile")
    raw = raw if isinstance(raw, dict) else {}
    description = _text(audience, "general learner", 300)
    return {
        "user_description": description,
        "inferred_context": _text(
            raw.get("inferred_context"),
            "The audience description is underspecified; avoid unsupported assumptions.",
        ),
        "prior_knowledge": _text(raw.get("prior_knowledge"), "uncertain", 200),
        "learning_goal": _text(raw.get("learning_goal"), plan.get("learning_goal", "Understand the requested topic.")),
        "language_and_tone": _text(
            raw.get("language_and_tone"),
            "Clear, respectful language calibrated from demonstrated familiarity rather than labels.",
        ),
        "terminology_policy": _text(
            raw.get("terminology_policy"),
            "Define specialized terms before use unless the request itself demonstrates that knowledge.",
        ),
        "assumptions": _unique_texts(raw.get("assumptions"), limit=8),
        "terms_to_define": _unique_texts(raw.get("terms_to_define"), limit=16),
    }


def _normalize_strategy(plan: Dict[str, Any]) -> Dict[str, Any]:
    raw = plan.get("explanation_strategy")
    raw = raw if isinstance(raw, dict) else {}
    return {
        "central_question": _text(raw.get("central_question"), plan.get("learning_goal", "What should the learner understand?")),
        "core_takeaway": _text(raw.get("core_takeaway"), plan.get("scope_summary", "")),
        "mental_model": _text(raw.get("mental_model"), "Build a concrete relationship the learner can reuse."),
        "narrative_thread": _text(raw.get("narrative_thread"), "Carry one coherent question, object, case, or example across scenes."),
        "primary_example": _text(raw.get("primary_example"), "Use a concrete example when it materially improves understanding."),
        "progression": _unique_texts(raw.get("progression"), limit=20),
        "excluded_for_capacity": _unique_texts(raw.get("excluded_for_capacity"), limit=20),
    }


def _normalize_semantic_contract(scene: Dict[str, Any]) -> Dict[str, Any]:
    raw = scene.get("semantic_contract")
    raw = raw if isinstance(raw, dict) else {}
    contract = {
        "claim": _text(raw.get("claim"), scene.get("teaching_beat", "")),
        "visual_evidence": _text(raw.get("visual_evidence"), scene.get("visual_prompt", "")),
        "before_state": _text(raw.get("before_state"), "", 300),
        "after_state": _text(raw.get("after_state"), "", 300),
        "inherited_state": _text(raw.get("inherited_state"), "", 300),
        "required_entities": _unique_texts(raw.get("required_entities"), limit=10, item_limit=120),
        "state_facts": _unique_texts(raw.get("state_facts"), limit=10, item_limit=180),
        "exact_anchors": _unique_texts(raw.get("exact_anchors"), limit=20, item_limit=160),
        "constructed_example": bool(raw.get("constructed_example", False)),
        "example_context": _text(raw.get("example_context"), "", 300),
        "example_literals": _unique_texts(raw.get("example_literals"), limit=40, item_limit=80),
    }
    narration = " ".join(
        str(beat.get("narration", ""))
        for beat in scene.get("narration_beats", []) if isinstance(beat, dict)
    ) or str(scene.get("narration", ""))
    visual_actions = " ".join(
        str(beat.get("visual_action", ""))
        for beat in scene.get("narration_beats", []) if isinstance(beat, dict)
    ) or str(scene.get("visual_prompt", ""))
    semantic_corpus = " ".join(str(contract.get(key, "")) for key in (
        "claim", "visual_evidence", "before_state", "after_state", "inherited_state",
    )) + " " + narration
    semantic_tokens = set(re.findall(r"[a-z0-9]+", semantic_corpus.casefold()))

    presentation_actions: List[str] = []
    semantic_entities: List[str] = []
    for entity in contract["required_entities"]:
        tokens = set(re.findall(r"[a-z0-9]+", entity.casefold()))
        if tokens and not (tokens & semantic_tokens) and entity.casefold() in visual_actions.casefold():
            presentation_actions.append(entity)
        else:
            semantic_entities.append(entity)

    def requests_literal_label(anchor: str) -> bool:
        anchor_folded = anchor.casefold().strip()
        quoted_labels = re.findall(
            r"\b(?:label(?:ed)?|text|title|caption|button|code|formula)\b"
            r"[^.\n]{0,80}?['\"]([^'\"]+)['\"]",
            visual_actions,
            flags=re.IGNORECASE,
        )
        if any(value.casefold().strip() == anchor_folded for value in quoted_labels):
            return True
        return bool(re.search(
            rf"\b(?:label(?:ed)?|text|title|caption|button|code|formula)\s+"
            rf"(?:the\s+)?{re.escape(anchor)}\b",
            visual_actions,
            flags=re.IGNORECASE,
        ))

    downgraded: List[str] = []
    literal_anchors: List[str] = []
    entity_token_sets = [
        set(re.findall(r"[a-z0-9]+", entity.casefold())) for entity in semantic_entities
    ]
    for anchor in contract["exact_anchors"]:
        tokens = set(re.findall(r"[a-z0-9]+", anchor.casefold()))
        has_literal_notation = bool(re.search(r"\d|[<>=+*/%$€£₹]|[`{}\[\]]", anchor))
        duplicates_entity = bool(tokens) and any(
            tokens <= entity_tokens or entity_tokens <= tokens
            for entity_tokens in entity_token_sets if entity_tokens
        )
        if duplicates_entity and not has_literal_notation and not requests_literal_label(anchor):
            downgraded.append(anchor)
        else:
            literal_anchors.append(anchor)
    for anchor in downgraded:
        if anchor.casefold() not in {value.casefold() for value in semantic_entities}:
            semantic_entities.append(anchor)

    contract["required_entities"] = semantic_entities
    contract["exact_anchors"] = literal_anchors
    contract["presentation_actions"] = presentation_actions
    contract["_role_normalization"] = {
        "presentation_entities_reclassified": presentation_actions,
        "entity_anchors_reclassified": downgraded,
    }
    return contract


def _selected_scene_roles(scenes: Iterable[Dict[str, Any]]) -> List[str]:
    return [_text(scene.get("teaching_role"), "unspecified", 80) for scene in scenes]


def normalize_explanation_plan(
    plan: Dict[str, Any],
    *,
    audience: str,
    target_seconds: float,
    target_words: int,
) -> Dict[str, Any]:
    """Normalize instructional metadata and attach downloadable diagnostics."""
    scenes = [scene for scene in plan.get("scenes", []) if isinstance(scene, dict)]
    plan["audience_profile"] = _normalize_audience_profile(plan, audience)
    plan["explanation_strategy"] = _normalize_strategy(plan)
    plan["duration_teaching_contract"] = duration_teaching_contract(
        target_seconds, len(scenes), target_words
    )

    previous_takeaway = ""
    previous_after_state = ""
    scene_diagnostics = []
    for index, scene in enumerate(scenes, start=1):
        scene["teaching_role"] = _text(scene.get("teaching_role"), "explain", 80)
        scene["learner_takeaway"] = _text(
            scene.get("learner_takeaway"), scene.get("teaching_beat", f"Understand scene {index}.")
        )
        scene["builds_on"] = _text(scene.get("builds_on"), previous_takeaway, 300)
        scene["semantic_contract"] = _normalize_semantic_contract(scene)
        previous_takeaway = scene["learner_takeaway"]
        contract = scene["semantic_contract"]
        if previous_after_state and not contract.get("inherited_state"):
            contract["inherited_state"] = previous_after_state
        previous_after_state = contract.get("after_state", "")
        narration_source = scene.get("narration")
        if not _text(narration_source):
            raw_beats = scene.get("narration_beats")
            if isinstance(raw_beats, list):
                narration_source = " ".join(
                    _text(beat.get("narration"), limit=4000)
                    for beat in raw_beats if isinstance(beat, dict)
                )
        narration_folded = _text(narration_source, limit=20_000).casefold()
        missing_from_narration = [
            anchor for anchor in contract["exact_anchors"]
            if anchor.casefold() not in narration_folded
        ]
        # Do not forward unspoken literals to the graphics model. They create
        # extra labels and can introduce obsolete or unexplained concepts.
        # Keep the rejection visible in diagnostics for debugging.
        if missing_from_narration:
            contract["exact_anchors"] = [
                anchor for anchor in contract["exact_anchors"]
                if anchor not in missing_from_narration
            ]
        scene_diagnostics.append({
            "scene_id": int(scene.get("id", index)),
            "teaching_role": scene["teaching_role"],
            "learner_takeaway": scene["learner_takeaway"],
            "builds_on": scene["builds_on"],
            "claim": contract["claim"],
            "visual_evidence": contract["visual_evidence"],
            "required_entities": contract["required_entities"],
            "state_facts": contract["state_facts"],
            "exact_anchors": contract["exact_anchors"],
            "rejected_ungrounded_anchors": missing_from_narration,
            "anchors_missing_from_narration": missing_from_narration,
            "narration_anchor_coverage_pass": not missing_from_narration,
            "has_claim": bool(contract["claim"]),
            "has_visual_evidence": bool(contract["visual_evidence"]),
        })

    roles = _selected_scene_roles(scenes)
    strategy = plan["explanation_strategy"]
    profile = plan["audience_profile"]
    plan["explanation_diagnostics"] = {
        "version": "1.0",
        "extra_llm_calls": 0,
        "audience_is_free_form": True,
        "duration_is_continuous": True,
        "target_seconds": round(float(target_seconds), 3),
        "scene_count": len(scenes),
        "audience_profile_complete": all(
            bool(profile.get(key))
            for key in ("user_description", "prior_knowledge", "language_and_tone", "terminology_policy")
        ),
        "strategy_complete": all(
            bool(strategy.get(key))
            for key in ("central_question", "core_takeaway", "mental_model", "narrative_thread")
        ),
        "scene_roles": roles,
        "all_scenes_have_takeaways": all(bool(row["learner_takeaway"]) for row in scene_diagnostics),
        "all_scenes_have_claims": all(row["has_claim"] for row in scene_diagnostics),
        "all_scenes_have_visual_evidence": all(row["has_visual_evidence"] for row in scene_diagnostics),
        "all_exact_anchors_grounded_in_narration": all(
            row["narration_anchor_coverage_pass"] for row in scene_diagnostics
        ),
        "scenes": scene_diagnostics,
    }
    return plan


def _visible_plan_strings(visual_plan: Dict[str, Any]) -> List[str]:
    """Return only strings a viewer can actually read on screen."""
    result: List[str] = []
    elements = visual_plan.get("elements")
    if isinstance(elements, list):
        for element in elements:
            if not isinstance(element, dict):
                continue
            if str(element.get("type", "")).lower() == "text":
                value = str(element.get("text", "")).strip()
                if value:
                    result.append(value)
    storyboard = visual_plan.get("storyboard")
    if isinstance(storyboard, dict):
        title = str(storyboard.get("title", "")).strip()
        if title:
            result.append(title)
        for item in storyboard.get("items", []) if isinstance(storyboard.get("items"), list) else []:
            if isinstance(item, dict):
                result.extend(
                    str(item.get(key, "")).strip()
                    for key in ("label", "detail") if str(item.get(key, "")).strip()
                )
    return result


def _semantic_plan_strings(visual_plan: Dict[str, Any]) -> List[str]:
    """Return viewer-facing text plus semantic ids/icon queries for evidence QA."""
    result = _visible_plan_strings(visual_plan)
    elements = visual_plan.get("elements")
    if isinstance(elements, list):
        for element in elements:
            if not isinstance(element, dict):
                continue
            result.extend(
                str(element.get(key, "")).strip()
                for key in ("id", "query") if str(element.get(key, "")).strip()
            )
    return result


def _semantic_reference_set(visual_plan: Dict[str, Any]) -> set[str]:
    """Return references attached to visible visual evidence.

    Text can name an entity or repeat a fact without depicting it.  Requiring
    a non-text mark prevents a labelled placeholder from satisfying the same
    contract as an actual object, state, or relationship.
    """
    refs: set[str] = set()
    elements = visual_plan.get("elements")
    if not isinstance(elements, list):
        return refs
    for element in elements:
        if not isinstance(element, dict) or _is_background_semantic_element(element):
            continue
        values = element.get("semantic_refs", [])
        if isinstance(values, list):
            refs.update(
                str(value).strip().upper() for value in values
                if re.fullmatch(r"[EF]\d{1,2}", str(value).strip(), flags=re.I)
                and (
                    str(element.get("type", "")).strip().lower() != "text"
                    or str(value).strip().upper().startswith("F")
                )
            )
    return refs


def _text_semantic_reference_set(visual_plan: Dict[str, Any]) -> set[str]:
    """Return semantic references carried by visible text elements.

    Text is valid evidence when the contract explicitly asks for text-like
    content (a label, title, question, formula, value, or annotation).  It is
    still not allowed to stand in for a concrete object such as a person,
    planet, machine, or physical process.
    """
    refs: set[str] = set()
    elements = visual_plan.get("elements")
    if not isinstance(elements, list):
        return refs
    for element in elements:
        if (
            not isinstance(element, dict)
            or _is_background_semantic_element(element)
            or str(element.get("type", "")).strip().lower() != "text"
        ):
            continue
        if float(element.get("opacity", 1.0) or 0.0) <= 0.01:
            continue
        for value in element.get("semantic_refs", []) if isinstance(element.get("semantic_refs"), list) else []:
            value = str(value).strip().upper()
            if re.fullmatch(r"[EF]\d{1,2}", value):
                refs.add(value)
    return refs


_TEXTUAL_REQUIREMENT_WORDS = {
    "text", "label", "caption", "title", "heading", "question", "prompt",
    "equation", "formula", "symbol", "value", "number", "annotation",
    "legend", "word", "phrase", "sentence", "quote", "name",
}
_TEXTUAL_STATE_WORDS = {
    "zero", "none", "absent", "removed", "missing", "empty", "off",
    "on", "open", "closed", "enabled", "disabled", "true", "false",
}


def _is_textual_requirement(value: str) -> bool:
    tokens = {token for token in _anchor_tokens(value) if token.isalnum()}
    # A concise state label such as “zero velocity” or “switch off” is itself
    # visible evidence when it is explicitly bound to the contract. Concrete
    # object nouns still require a non-text mark.
    return bool(tokens & (_TEXTUAL_REQUIREMENT_WORDS | _TEXTUAL_STATE_WORDS))


def _declared_semantic_reference_set(visual_plan: Dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for element in visual_plan.get("elements", []) if isinstance(visual_plan.get("elements"), list) else []:
        if not isinstance(element, dict) or _is_background_semantic_element(element):
            continue
        for value in element.get("semantic_refs", []) if isinstance(element.get("semantic_refs"), list) else []:
            value = str(value).strip().upper()
            if re.fullmatch(r"[EF]\d{1,2}", value):
                refs.add(value)
    return refs


def _is_background_semantic_element(element: Dict[str, Any]) -> bool:
    element_id = str(element.get("id", "")).casefold()
    try:
        layer = int(float(element.get("layer", 2) or 2))
    except (TypeError, ValueError):
        layer = 2
    return layer <= 0 and any(
        token in element_id for token in ("background", "canvas", "field")
    )


def _anchor_tokens(value: str) -> List[str]:
    # Models and source documents freely mix ASCII hyphen, non-breaking
    # hyphen, en dash and minus. They are visually equivalent separators for
    # anchors such as "in-transit" and must not create false missing-anchor
    # retries.
    normalized = str(value).casefold().translate(str.maketrans({
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
        "\u2014": "-", "\u2212": "-",
    }))
    return re.findall(r"[a-z0-9]+|[<>!=]=?|\+|-|/", normalized)


_EVIDENCE_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "with", "from", "in",
    "on", "at", "by", "for", "is", "are", "was", "were", "be", "due",
}


def _evidence_token_present(token: str, available: set) -> bool:
    if token in available:
        return True
    stem = token[:-1] if token.endswith("s") and len(token) > 4 else token
    for candidate in available:
        candidate_stem = candidate[:-1] if candidate.endswith("s") and len(candidate) > 4 else candidate
        if stem == candidate_stem:
            return True
        if min(len(stem), len(candidate_stem)) >= 4 and (
            stem.startswith(candidate_stem) or candidate_stem.startswith(stem)
        ):
            return True
    return False


def _contract_entity_alias(
    entity: str, contract: Dict[str, Any], scene: Dict[str, Any]
) -> List[str]:
    """Return one unambiguous terminology alias already established upstream.

    This does not invent a synonym. It only reconciles a required phrase with
    the sole same-shaped phrase used by the director's own claim, evidence and
    narration (for example, `paid plan` versus the consistently used
    `premium plan`). Ambiguous alternatives are deliberately rejected.
    """
    entity_tokens = [token for token in _anchor_tokens(entity) if token.isalnum()]
    if len(entity_tokens) < 2:
        return []
    corpus = " ".join(str(value or "") for value in (
        contract.get("claim"), contract.get("visual_evidence"),
        contract.get("before_state"), contract.get("after_state"),
        scene.get("narration"),
    ))
    corpus_tokens = [token for token in _anchor_tokens(corpus) if token.isalnum()]
    size, head = len(entity_tokens), entity_tokens[-1]
    original = " ".join(entity_tokens)
    if original in " ".join(corpus_tokens):
        return []
    candidates = {
        tuple(corpus_tokens[index:index + size])
        for index in range(0, max(0, len(corpus_tokens) - size + 1))
        if corpus_tokens[index + size - 1] == head
    }
    candidates.discard(tuple(entity_tokens))
    return list(next(iter(candidates))) if len(candidates) == 1 else []


def assess_visual_anchor_coverage(
    visual_plan: Dict[str, Any], scene: Dict[str, Any]
) -> Dict[str, Any]:
    """Measure whether literal scene facts survived visual generation."""
    contract = scene.get("semantic_contract")
    contract = contract if isinstance(contract, dict) else {}
    anchors = _unique_texts(contract.get("exact_anchors"), limit=20, item_limit=160)
    haystack_tokens = set(_anchor_tokens(" ".join(_visible_plan_strings(visual_plan))))
    rows = []
    for anchor in anchors:
        tokens = _anchor_tokens(anchor)
        matched = sum(1 for token in tokens if token in haystack_tokens)
        ratio = matched / max(1, len(tokens))
        rows.append({
            "anchor": anchor,
            "token_count": len(tokens),
            "matched_token_count": matched,
            "coverage_ratio": round(ratio, 6),
            "present": ratio >= 0.8,
        })
    missing = [row["anchor"] for row in rows if not row["present"]]
    semantic_tokens = set(_anchor_tokens(" ".join(_semantic_plan_strings(visual_plan))))
    semantic_strings = _semantic_plan_strings(visual_plan)
    semantic_refs = _semantic_reference_set(visual_plan)
    text_semantic_refs = _text_semantic_reference_set(visual_plan)
    declared_semantic_refs = _declared_semantic_reference_set(visual_plan)

    def coverage(
        values: List[str], label: str, threshold: float, reference_prefix: str
    ) -> List[Dict[str, Any]]:
        checked = []
        for index, value in enumerate(values, start=1):
            tokens = _anchor_tokens(value)
            evidence_tokens = [
                token for token in tokens
                if token.isalnum() and token not in _EVIDENCE_STOPWORDS
            ] or [token for token in tokens if token.isalnum()]
            matched = sum(
                1 for token in evidence_tokens
                if _evidence_token_present(token, semantic_tokens)
            )
            ratio = matched / max(1, len(evidence_tokens))
            # Numeric values are never optional: an F-reference cannot claim
            # coverage while showing a different value. Operators in a state
            # ledger ("status = delivered") describe the relationship; they
            # are not viewer-facing text. Formula/operator fidelity belongs to
            # exact_anchors, which is validated independently above.
            literals = [token for token in tokens if any(ch.isdigit() for ch in token)]
            literals_present = all(token in semantic_tokens for token in literals)
            semantic_reference = f"{reference_prefix}{index}"
            reference_present = semantic_reference in semantic_refs
            text_reference_allowed = (
                reference_prefix == "E" and _is_textual_requirement(value)
            )
            if text_reference_allowed and semantic_reference in text_semantic_refs:
                reference_present = True
            alias_tokens = (
                _contract_entity_alias(value, contract, scene)
                if reference_prefix == "E" else []
            )
            alias_present = bool(alias_tokens) and all(
                token in semantic_tokens for token in alias_tokens
            )
            structural_head_present = False
            if reference_prefix == "E" and len(evidence_tokens) >= 2:
                head = evidence_tokens[-1]
                structural_head_present = sum(
                    any(
                        _evidence_token_present(head, {token})
                        for token in _anchor_tokens(item) if token.isalnum()
                    )
                    for item in semantic_strings
                ) >= 2
            card_only = bool(
                isinstance(visual_plan.get("_storyboard"), dict)
                and visual_plan["_storyboard"].get("card_only")
            )
            if declared_semantic_refs:
                lexical_present = ratio >= threshold or alias_present or structural_head_present
                # A card-only scene can repeat every contract word and still
                # show none of the requested evidence.  In that representation
                # lexical duplication is diagnostic, never proof.
                misplaced_reference = (
                    semantic_reference in declared_semantic_refs and not reference_present
                )
                present = (
                    reference_present
                    or (lexical_present and not card_only and not misplaced_reference)
                ) and literals_present
                evidence_mode = (
                    "semantic_reference" if reference_present
                    else (
                        "structural_head_evidence" if structural_head_present and ratio < threshold
                        else "hybrid_lexical_evidence"
                    )
                )
            else:
                present = (ratio >= threshold or alias_present or structural_head_present) and literals_present
                evidence_mode = (
                    "director_contract_alias" if alias_present and ratio < threshold else (
                        "structural_head_evidence" if structural_head_present and ratio < threshold
                        else "legacy_lexical_fallback"
                    )
                )
            checked.append({
                label: value,
                "token_count": len(tokens),
                "matched_token_count": matched,
                "coverage_ratio": round(ratio, 6),
                "literal_tokens_present": literals_present,
                "semantic_reference": semantic_reference,
                "semantic_reference_present": reference_present,
                "text_reference_allowed": text_reference_allowed,
                "contract_alias": " ".join(alias_tokens),
                "evidence_mode": evidence_mode,
                "present": present,
            })
        return checked

    entities = _unique_texts(contract.get("required_entities"), limit=10, item_limit=120)
    state_facts = _unique_texts(contract.get("state_facts"), limit=10, item_limit=180)
    entity_rows = coverage(entities, "entity", 0.75, "E")
    fact_rows = coverage(state_facts, "fact", 0.65, "F")
    missing_entities = [row["entity"] for row in entity_rows if not row["present"]]
    missing_facts = [row["fact"] for row in fact_rows if not row["present"]]
    return {
        "scene_id": scene.get("id"),
        "teaching_role": scene.get("teaching_role"),
        "learner_takeaway": scene.get("learner_takeaway"),
        "semantic_contract": contract,
        "anchor_count": len(rows),
        "anchors": rows,
        "missing_anchors": missing,
        "anchor_coverage_pass": not missing,
        "required_entities": entity_rows,
        "state_facts": fact_rows,
        "missing_required_entities": missing_entities,
        "missing_state_facts": missing_facts,
        "evidence_coverage_pass": not missing_entities and not missing_facts,
        "semantic_binding_mode": "semantic_references" if declared_semantic_refs else "legacy_lexical_fallback",
        "semantic_references_present": sorted(semantic_refs),
        "text_semantic_references_present": sorted(text_semantic_refs),
        "semantic_references_declared": sorted(declared_semantic_refs),
    }


__all__ = [
    "assess_visual_anchor_coverage",
    "duration_teaching_contract",
    "normalize_explanation_plan",
]
