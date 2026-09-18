import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, Tuple


# Director code is also imported by worker.py and standalone validation tools,
# which do not pass through app.py's console setup. A legacy Windows CP1252
# stream must never turn valid Unicode model output into a failed generation.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass

from llm_provider import (
    create_llm_client,
    fit_completion_token_budget,
    get_llm_config,
    missing_key_message,
)
from explanation_quality import duration_teaching_contract, normalize_explanation_plan
from quantity_policy import (
    MAGNITUDE_WORD_RE,
    canonical_quantity as _canonical_quantity,
    quantified_tokens as _quantified_tokens,
    quantity_requires_source,
)


DIRECTOR_LLM = get_llm_config("director")
LLM_PROVIDER = DIRECTOR_LLM.provider
DIRECTOR_MODEL = DIRECTOR_LLM.model
DIRECTOR_WPM = float(os.environ.get("DIRECTOR_WPM", "145"))
SCENE_SECONDS_TARGET = float(os.environ.get("SCENE_SECONDS_TARGET", "14"))
MIN_SCENES = int(os.environ.get("MIN_SCENES", "3"))
MAX_SCENES = int(os.environ.get("MAX_SCENES", "30"))


client = create_llm_client(DIRECTOR_LLM)


DIRECTOR_SYSTEM_PROMPT = """
You are the director and instructional designer for an educational explainer-video system.

Your job is NOT to wander around a subject. Your job is to explain exactly the user's requested
concept/question as completely as possible within the requested duration.

CORE RULE: duration defines teaching capacity while the user's question defines the objective.
- A short video must answer the question completely at an appropriate level, but should select
  the irreducible ideas instead of racing through an encyclopedic checklist.
- A longer video should deepen the SAME objective with clearer intuition, mechanisms, worked
  examples, comparisons, misconceptions, edge cases, application, or recap. Do not drift into
  adjacent topics just to fill time.
- The supplied audience is unrestricted free-form text. Infer likely prior knowledge, goal,
  vocabulary, tone, and assumptions from the complete description. Never use a fixed lookup
  such as student/developer/non-technical, and never assume that one label fully defines a person.
- If the audience description is ambiguous, state conservative assumptions in audience_profile,
  define specialized terms before use, and remain respectful rather than childish.
- Establish a scope lock with must_cover and out_of_scope before writing scenes.
- Narration across all scenes must form one coherent explanation from beginning to end.
- Establish one central question, one reusable mental model, and a coherent narrative thread.
  Prefer carrying one object, case, dataset, example, or decision through the explanation instead
  of creating unrelated summary slides.
- Every scene must have a teaching_role, learner_takeaway, builds_on dependency, and semantic_contract.
- The semantic_contract states the scene's claim, the visible evidence that proves it, the before
  and after states when something changes, and exact_anchors for literal labels, values, formulas,
  quotations, or code that narration and visuals must not contradict.
- semantic_contract.example_literals may contain model-constructed values only for an explicitly
  hypothetical teaching example, test input, mathematical demonstration, or worked procedure.
  Describe that purpose in example_context and set constructed_example=true. These literals are
  not factual evidence and must never be presented as measured statistics, real-world rates,
  sourced facts, prices, dates, or claims about an actual entity.
- semantic_contract.required_entities lists the concrete objects/states that must visibly exist.
  semantic_contract.state_facts lists short atomic facts containing the subject, phase or role,
  relation, and literal value when one exists. Reuse applicable facts from the preceding scene so
  the same example never silently changes values or meaning.
- required_entities must name the subjects or states whose presence carries the meaning.
  Rendering instructions and presentation formats belong in visual_evidence, not in the entity list.
- Before returning, verify claim, visual_evidence, before_state, after_state, state_facts,
  narration, and exact_anchors agree. A successful branch must satisfy its stated conditions;
  otherwise show the rejection/error branch. Never place an after-state value in an input/before
  position without explicitly labeling it as the result.
- Every scene must have a visual purpose. Avoid scenes that are only decorative.
- Treat the visuals as a stateful sequence, not unrelated slides. For every
  scene return a compact visual_state object. Use mode "continue" only when
  the same objects/diagram genuinely persist from an earlier scene; otherwise
  use "new". Reuse the same short group name across a continued sequence.
  A group names one stable composition, not the lesson topic or teaching arc.
  Changing from illustration to architecture, example, comparison, or another
  substantially different composition requires mode "new" and a new group.
  In keep, list only the recurring semantic objects that must stay visually
  stable as complete objects (shape/icon plus label), and only if every kept
  object was visibly present in the earlier composition. Never keep a word or
  label by itself. Generic layout roles such as Input, Output, Decision,
  Process, Result, Step, or State are not object identities and must never
  appear in keep. This rule is generic and applies to every subject.
- visual_action is an instruction to a separate text-to-SVG visualizer. Describe what must be
  visually shown, including important literal values, arrows, states, labels, or relationships.
- narration is spoken text only. Do not include stage directions in narration.
- Build each scene from narration_beats as the source of truth.
  Every beat has a stable beat_id, required flag, priority, spoken narration,
  and its matching visual_action. Required beats contain essential meaning;
  optional beats add depth/examples. Do not duplicate beat text into combined
  narration or visual_prompt fields; the local compiler creates those fields.
- Do not mention the requested duration in the narration unless the user explicitly asks.
- Do not use filler such as "let's dive in" repeatedly.
- Do not use an unsupported decorative comparison chart. A comparison must name its basis or
  workload and visually show evidence relevant to the spoken claim.
- Introduce terms before relying on them. Show causality, transformation, or evidence rather than
  merely placing icons beside nouns.
- Keep conceptual categories correct. A force, cause, constraint, motion, stored quantity,
  observation, probability, and reference-frame effect are different kinds of things. Never say
  two items balance, cancel, match, cause, or equal one another unless that relation is valid in
  the stated model. Define equation symbols in plain language before using the equation.
- Treat empirical quantities as closed-world evidence. A percentage, measurement, date,
  monetary amount, scaled magnitude, or unitless numeral asserted as a factual claim may appear
  only when it occurs in the user's request or supplied source document. The requested video
  duration is not factual evidence. When no source supports a statistic, teach with mechanism,
  intuition, or a qualitative example instead. Never invent a plausible-looking quantity.
- A bare unitless numeral may be used only as visual structure (for example cardinality,
  indexing, logic, mathematics, or code) without becoming a factual claim. Do not speak it as
  an empirical fact. If it is spoken as part of a hypothetical worked example, declare it using
  the constructed-example contract below.
- When narration intentionally numbers a multi-beat explanation, use a complete beat-aligned
  sequence: beat 1 uses 1, beat 2 uses 2, and so on. Put each spoken sequence marker in that
  beat's exact_anchors so the visualizer preserves the numeral rather than replacing it with an
  icon. Sequence markers label explanation order; they must not be phrased as measured facts.
- Constructed teaching examples are the sole exception: unitless sample values may be invented
  when a concrete demonstration genuinely requires them, but they must be declared in
  semantic_contract.example_literals and clearly framed by example_context as hypothetical.
  Keep one consistent example across scenes. Never use this exception for empirical statistics.

Return strict JSON only.
""".strip()


REVISION_SYSTEM_PROMPT = """
You revise an already valid, already scoped educational
video script so its locally generated narration approaches
a requested duration.

The INPUT contains a complete valid video plan.

The OUTPUT must also be a complete video plan.

ABSOLUTE RULES

1. Return ONE JSON object only.

2. The ROOT JSON object MUST contain:
   "scenes": [...]

3. NEVER return only:
   - narration
   - revised_scenes
   - changes
   - patches
   - analysis
   - suggestions

4. NEVER omit the scenes array.

5. Preserve the exact topic.

6. Preserve the exact learning goal.

7. Preserve every must_cover item.

8. Preserve out_of_scope.

9. Keep the same number of scenes.

10. Keep the same scene IDs.

11. Every scene MUST contain:
    - id
    - teaching_beat
    - narration
    - visual_prompt
    - visual_state

Preserve every scene's visual_state mode, group, and keep list unless the
revised narration genuinely changes which semantic objects persist.

12. Every narration must be non-empty.

13. Every visual_prompt must be non-empty.

14. If shortening:
    - remove repetition
    - make sentences tighter
    - shorten examples
    - compress transitions
    - do NOT remove essential teaching points
    - do NOT cut off the conclusion

15. If lengthening:
    - deepen existing intuition
    - clarify an existing step
    - expand an existing example
    - improve transitions
    - strengthen the recap
    - do NOT introduce a new adjacent topic

16. Preserve the scoped objective. Use the available duration for clearer
    intuition, evidence, examples, application, or recap without adding
    adjacent topics.

Return strict JSON only.
""".strip()


PREFLIGHT_REPAIR_SYSTEM_PROMPT = """
You repair the duration contract of an already scoped educational video plan.

Return strict JSON only with one root key: scenes.
Return every original scene exactly once and preserve every scene ID, teaching
purpose, factual claim, example, and exact anchor.

COMPACT OUTPUT CONTRACT: every scene object must contain exactly two keys:
id and narration_beats. Do not repeat teaching_beat, semantic_contract,
visual_state, narration, visual_prompt, learner_takeaway, or any other metadata.
The local merger retains all authoritative metadata from the original plan.

For every returned scene, provide narration_beats. Every narration beat must
contain beat_id, required, priority, narration, visual_action, and exact_anchors.
Narration is spoken text. visual_action must remain semantically paired with
that exact narration beat, so expanding narration must also expand its matching
visible explanation. Never add filler, unrelated facts, or a new adjacent topic.

Meet every supplied per-scene word range and the total narration word range.
Count words in narration_beats[*].narration only. Before returning, silently
count them and correct the response until it satisfies the ranges.
""".strip()


FACTUALITY_REPAIR_SYSTEM_PROMPT = """
You repair unsupported quantitative claims in an educational video plan.

Return strict JSON with one root key, scenes. Each scene must contain only id,
narration_beats, example_literals, example_context, and constructed_example.
Preserve every scene ID, teaching purpose, causal
explanation, required beat, and approximate per-scene word count.

The supplied evidence boundary is closed-world: a percentage, measurement,
date, monetary amount, scaled magnitude, or unitless numeral asserted as a
factual claim may be stated only when it occurs in authorized_source_text. A
bare unitless numeral used only as structural visual direction (cardinality,
indexing, logic, mathematics, or code) is not an empirical claim. Do not treat the
requested video duration, a model-created exact anchor, or general background
knowledge as authorization. Replace unsupported quantitative claims with
accurate qualitative mechanism, intuition, or an evidence-free example; never
invent a different number. Keep narration and visual_action semantically paired.
Do not add filler or adjacent topics.

A complete one-based sequence aligned with a scene's narration beats is spoken
enumeration, not empirical evidence: beat 1 may say 1, beat 2 may say 2, and so
on. Preserve these markers and their exact_anchors. This exception does not
authorize mismatched counts, percentages, units, dates, money, or magnitudes.

Exception for constructed teaching examples: unitless values used only as
hypothetical inputs, algorithm test data, mathematical demonstration values, or
worked-example state may be preserved and returned in example_literals. Set
constructed_example=true and describe why the values are hypothetical in
example_context. Never classify a percentage, measurement, price, date,
real-world statistic, or claim about an actual entity as a constructed example.
""".strip()


def _authorized_quantities(user_prompt: str, source_text: str) -> List[str]:
    # Intentionally excludes target_duration_seconds. Runtime configuration is
    # not evidence for a factual claim in the lesson.
    return _quantified_tokens(f"{user_prompt}\n{source_text}")


def _constructed_example_quantities(contract: Dict[str, Any]) -> List[str]:
    """Authorize explicit low-risk literals in a declared hypothetical example.

    Measurements such as a sample timer duration are valid teaching inputs;
    treating them as empirical claims caused verifier and rewrite calls that
    destroyed the example. Percentages, currency, dates and scaled population
    magnitudes remain source-locked because they are easily mistaken for real
    statistics.
    """
    if not bool(contract.get("constructed_example", False)):
        return []
    if not str(contract.get("example_context", "")).strip():
        return []
    values = contract.get("example_literals", [])
    if not isinstance(values, list):
        return []
    result: List[str] = []
    seen = set()
    for value in values:
        for token in _quantified_tokens(value):
            canonical = _canonical_quantity(token)
            raw = str(token).strip()
            if (
                re.search(r"[%$€£₹]", raw)
                or re.search(r"\b(?:percent|percentage)\b", raw, re.I)
                or MAGNITUDE_WORD_RE.search(raw)
                or re.fullmatch(r"(?:1\d{3}|20\d{2}|2100)", canonical)
            ):
                continue
            if canonical not in seen:
                seen.add(canonical)
                result.append(token)
    return result


def _propagate_global_example_contract(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Carry explicit model-created example provenance into scenes that use it.

    Hierarchical planning creates the example once in the global strategy and
    expands scenes in separate calls.  A scene batch can copy the values while
    accidentally dropping their provenance, causing the factuality gate to
    misclassify test data as a real-world statistic.  Propagation is based only
    on the declared literal set, never on topic names or guessed intent.
    """
    global_contract = plan.get("example_contract")
    if not isinstance(global_contract, dict):
        return plan
    if not bool(global_contract.get("constructed_example", False)):
        return plan
    context = str(global_contract.get("example_context", "")).strip()
    literals = global_contract.get("example_literals", [])
    if not context or not isinstance(literals, list):
        return plan
    declared = {
        _canonical_quantity(token): str(literal).strip()
        for literal in literals if str(literal).strip()
        for token in _quantified_tokens(literal)
    }
    if not declared:
        return plan
    for scene in plan.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        contract = scene.get("semantic_contract")
        if not isinstance(contract, dict):
            contract = {}
            scene["semantic_contract"] = contract
        searchable = " ".join([
            str(scene.get("narration", "")),
            str(scene.get("visual_prompt", "")),
            json.dumps(scene.get("narration_beats", []), ensure_ascii=False),
            json.dumps(contract, ensure_ascii=False),
        ])
        used = {
            _canonical_quantity(token)
            for token in _quantified_tokens(searchable)
        } & set(declared)
        if not used:
            continue
        contract["constructed_example"] = True
        contract["example_context"] = context[:300]
        existing = contract.get("example_literals", [])
        existing = existing if isinstance(existing, list) else []
        contract["example_literals"] = list(dict.fromkeys([
            *[str(value).strip() for value in existing if str(value).strip()],
            *[declared[value] for value in sorted(used)],
        ]))[:40]
    return plan


def _coerce_single_scene_response(
    result: Dict[str, Any], expected_ids: List[int]
) -> None:
    """Normalize a valid singleton scene object without spending a retry."""
    if "scenes" in result or len(expected_ids) != 1:
        return
    try:
        scene_id = int(result.get("id", 0))
    except (TypeError, ValueError):
        return
    if scene_id != int(expected_ids[0]) or not isinstance(result.get("narration_beats"), list):
        return
    scene = deepcopy(result)
    result.clear()
    result["scenes"] = [scene]
    result["_response_shape_repair"] = "wrapped_single_scene_object"


def _beat_aligned_sequence_literals(scene: Dict[str, Any]) -> Dict[str, set[str]]:
    """Infer explicit enumeration from plan structure, independent of topic.

    A numeral is structural only when every narration beat in a multi-beat
    scene contains its own one-based beat index. This relational condition
    recognizes 1/2/3-style spoken organization without maintaining vocabulary
    lists such as "step", "rule", "reason", or any domain-specific terms.
    Other numerals in those beats retain normal factuality enforcement.
    """
    beats = [
        beat for beat in scene.get("narration_beats", [])
        if isinstance(beat, dict)
    ]
    if len(beats) < 2:
        return {}

    per_beat: List[set[str]] = []
    for beat in beats:
        bare_values = {
            _canonical_quantity(token)
            for token in _quantified_tokens(beat.get("narration", ""))
            if re.fullmatch(r"[+-]?\d+", _canonical_quantity(token))
        }
        per_beat.append(bare_values)

    if not all(str(index) in values for index, values in enumerate(per_beat, start=1)):
        return {}

    result: Dict[str, set[str]] = {}
    sequence = {str(index) for index in range(1, len(beats) + 1)}
    for index in range(1, len(beats) + 1):
        result[f"beat_{index}.narration"] = {str(index)}
        result[f"beat_{index}.exact_anchors"] = {str(index)}
    # The scene-level anchor list may aggregate the same sequence markers.
    result["semantic_contract.exact_anchors"] = sequence
    result["semantic_contract.claim"] = sequence
    result["semantic_contract.visual_evidence"] = sequence
    return result


def _scene_factuality_violations(
    plan: Dict[str, Any], authorized_quantities: List[str]
) -> List[Dict[str, Any]]:
    globally_authorized = {_canonical_quantity(value) for value in authorized_quantities}
    violations: List[Dict[str, Any]] = []
    for position, scene in enumerate(plan.get("scenes", []), start=1):
        if not isinstance(scene, dict):
            continue
        contract = scene.get("semantic_contract", {})
        contract = contract if isinstance(contract, dict) else {}
        example_literals = _constructed_example_quantities(contract)
        authorized = globally_authorized | {
            _canonical_quantity(value) for value in example_literals
            if str(value).strip()
        }
        structural_literals = _beat_aligned_sequence_literals(scene)
        fields: List[tuple[str, str]] = []
        for beat_index, beat in enumerate(scene.get("narration_beats", []), start=1):
            if not isinstance(beat, dict):
                continue
            fields.append((f"beat_{beat_index}.narration", str(beat.get("narration", ""))))
            fields.append((f"beat_{beat_index}.visual_action", str(beat.get("visual_action", ""))))
            anchors = beat.get("exact_anchors", [])
            if isinstance(anchors, list):
                fields.append((f"beat_{beat_index}.exact_anchors", " ".join(map(str, anchors))))
        if contract:
            for key in ("claim", "visual_evidence", "before_state", "after_state"):
                fields.append((f"semantic_contract.{key}", str(contract.get(key, ""))))
            for key in ("state_facts", "exact_anchors"):
                values = contract.get(key, [])
                if isinstance(values, list):
                    fields.append((f"semantic_contract.{key}", " ".join(map(str, values))))
        for field, text in fields:
            unsupported = [
                token for token in _quantified_tokens(text)
                if quantity_requires_source(
                    token,
                    structural_context=(
                        field.endswith(".visual_action")
                        or _canonical_quantity(token)
                        in structural_literals.get(field, set())
                    ),
                )
                and _canonical_quantity(token) not in authorized
            ]
            if unsupported:
                violations.append({
                    "scene_id": int(scene.get("id", position)),
                    "field": field,
                    "unsupported": unsupported,
                })
    return violations


_SPOKEN_NUMBER_WORD = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|"
    r"four|five|six|seven|eight|nine))?"
)


def _qualitative_quantity_replacement(token: str) -> str:
    """Return factuality-safe wording without making another model call."""
    raw = str(token).strip()
    lowered = raw.lower()
    if "%" in raw or "percent" in lowered or "percentage" in lowered:
        return "a representative share"
    if re.search(r"[$€£₹]", raw):
        return "an illustrative amount"
    if MAGNITUDE_WORD_RE.fullmatch(raw):
        return "a representative number of"
    if re.search(r"[A-Za-z°²³]", raw):
        return "an illustrative value"
    return "several"


def _replace_rejected_quantity(text: Any, token: str) -> str:
    """Rewrite the containing phrase, not just the rejected token.

    The model repair is the primary path.  This bounded local safety path keeps
    grammar intact when a provider is unavailable; notably it never turns a
    date into phrases such as ``in several``.
    """
    value = str(text or "")
    raw = str(token or "").strip()
    if not raw:
        return value
    if re.fullmatch(r"(?:1\d{3}|20\d{2}|2100)", raw):
        preposition_rewrites = {
            "in": "historically", "by": "over time", "since": "for years",
            "from": "from that period", "during": "during that period",
            "around": "around that period",
        }
        pattern = rf"\b({'|'.join(preposition_rewrites)})\s+{re.escape(raw)}\b"
        return re.sub(
            pattern,
            lambda match: preposition_rewrites[match.group(1).lower()],
            value, flags=re.IGNORECASE,
        ) if re.search(pattern, value, flags=re.IGNORECASE) else re.sub(
            rf"(?<!\w){re.escape(raw)}(?!\w)", "that period", value,
            flags=re.IGNORECASE,
        )
    if "%" in raw or re.search(r"\b(?:percent|percentage)\b", raw, re.I):
        by_pattern = rf"\bby\s+{re.escape(raw)}\b"
        if re.search(by_pattern, value, flags=re.IGNORECASE):
            return re.sub(by_pattern, "substantially", value, flags=re.IGNORECASE)
        of_pattern = rf"(?<![\w.]){re.escape(raw)}\s+of\b"
        if re.search(of_pattern, value, flags=re.IGNORECASE):
            return re.sub(
                of_pattern, "a substantial share of", value, flags=re.IGNORECASE
            )
    replacement = _qualitative_quantity_replacement(raw)
    if MAGNITUDE_WORD_RE.fullmatch(raw):
        # The quantity scanner reports only "thousand" in "ten thousand".
        # Consume its spoken-number prefix too, otherwise a naive replacement
        # would produce the ungrammatical phrase "ten a representative...".
        pattern = rf"(?<!\w)(?:{_SPOKEN_NUMBER_WORD}\s+|\d[\d,.]*\s+)?{re.escape(raw)}(?:\s+of)?(?!\w)"
    else:
        pattern = rf"(?<![\w.]){re.escape(raw)}(?![A-Za-z0-9])"
    def replace_match(match: re.Match) -> str:
        revised = replacement
        if match.start() == 0 and match.group(0)[:1].isupper():
            revised = revised[:1].upper() + revised[1:]
        return revised

    return re.sub(pattern, replace_match, value, count=0, flags=re.IGNORECASE)


def _apply_deterministic_factuality_fallback(
    plan: Dict[str, Any], authorized_quantities: List[str],
    violations: Optional[List[Dict[str, Any]]] = None,
    _max_passes: int = 4,
) -> Dict[str, Any]:
    """Scrub only validator-rejected literals, locally and without LLM tokens.

    Quantity tokenization can reveal a second token after an adjacent token is
    rewritten (for example the final value in a comma-separated sample list).
    Iterate to a fixed point instead of declaring failure after one mutation.
    """
    violations = (
        deepcopy(violations) if violations is not None
        else _scene_factuality_violations(plan, authorized_quantities)
    )
    if not violations:
        return {"applied": False, "replacement_count": 0, "remaining": []}

    scenes_by_id = {
        int(scene.get("id", position)): scene
        for position, scene in enumerate(plan.get("scenes", []), start=1)
        if isinstance(scene, dict)
    }
    replacement_count = 0
    for violation in violations:
        scene = scenes_by_id.get(int(violation.get("scene_id", -1)))
        if not isinstance(scene, dict):
            continue
        field = str(violation.get("field", ""))
        tokens = [str(value) for value in violation.get("unsupported", [])]
        beat_match = re.fullmatch(r"beat_(\d+)\.(narration|visual_action|exact_anchors)", field)
        if beat_match:
            beat_index = int(beat_match.group(1)) - 1
            beats = scene.get("narration_beats", [])
            if not isinstance(beats, list) or not (0 <= beat_index < len(beats)):
                continue
            target = beats[beat_index]
            key = beat_match.group(2)
        elif field.startswith("semantic_contract."):
            target = scene.get("semantic_contract", {})
            key = field.split(".", 1)[1]
        else:
            continue
        if not isinstance(target, dict):
            continue
        current = target.get(key, "")
        if isinstance(current, list):
            updated = []
            for item in current:
                text = str(item)
                for token in tokens:
                    revised = _replace_rejected_quantity(text, token)
                    replacement_count += int(revised != text)
                    text = revised
                updated.append(text)
            target[key] = updated
        else:
            text = str(current)
            for token in tokens:
                revised = _replace_rejected_quantity(text, token)
                replacement_count += int(revised != text)
                text = revised
            target[key] = text

    for scene in scenes_by_id.values():
        beats = [
            beat for beat in scene.get("narration_beats", [])
            if isinstance(beat, dict)
        ]
        scene["narration"] = " ".join(
            str(beat.get("narration", "")).strip() for beat in beats
        ).strip()
        scene["visual_prompt"] = " Then, ".join(
            str(beat.get("visual_action", "")).strip() for beat in beats
        ).strip()

    _synchronize_semantic_contracts_after_factual_repair(plan, authorized_quantities)
    remaining = _scene_factuality_violations(plan, authorized_quantities)
    pass_count = 1
    if remaining and replacement_count and _max_passes > 1:
        next_result = _apply_deterministic_factuality_fallback(
            plan, authorized_quantities, violations=remaining,
            _max_passes=_max_passes - 1,
        )
        replacement_count += int(next_result.get("replacement_count", 0))
        remaining = next_result.get("remaining", remaining)
        pass_count += int(next_result.get("pass_count", 1))
    return {
        "applied": bool(replacement_count),
        "replacement_count": replacement_count,
        "remaining": remaining,
        "pass_count": pass_count,
    }


def _verify_model_knowledge_quantities(
    plan: Dict[str, Any], violations: List[Dict[str, Any]], job_id: Optional[str],
) -> Dict[str, Any]:
    """Use an independent structured review before trusting model-only facts."""
    if not violations or os.environ.get(
        "DIRECTOR_ALLOW_MODEL_KNOWLEDGE", "true"
    ).strip().lower() not in {"1", "true", "yes", "on"}:
        return {"approved": [], "reviewed": False}
    payload = {
        "policy": (
            "Approve only stable, widely established quantities you are highly confident are correct. "
            "Reject estimates, current/live values, percentages without a clear basis, and uncertain facts."
        ),
        "violations": violations,
        "scenes": [
            {
                "id": scene.get("id"),
                "narration_beats": scene.get("narration_beats", []),
                "semantic_contract": scene.get("semantic_contract", {}),
            }
            for scene in plan.get("scenes", []) if isinstance(scene, dict)
        ],
        "required_schema": {"approved": ["exact token from violations"], "rejected": ["exact token"]},
    }
    try:
        result = _call_json(
            "You are a conservative factual claim verifier. Return JSON only.",
            payload, max_tokens=3000, stage="director_fact_verify", job_id=job_id,
            response_validator=lambda value: (
                None if isinstance(value.get("approved"), list)
                else (_ for _ in ()).throw(ValueError("Fact verifier must return approved list."))
            ),
        )
        requested = {
            _canonical_quantity(token)
            for violation in violations for token in violation.get("unsupported", [])
        }
        approved = []
        for token in result.get("approved", []):
            if _canonical_quantity(token) in requested:
                approved.append(str(token))
        return {"approved": list(dict.fromkeys(approved)), "reviewed": True,
                "rejected": result.get("rejected", [])}
    except Exception as error:  # bounded verifier; normal repair remains available
        return {"approved": [], "reviewed": True, "error": str(error)}


def _apply_visual_sequence_contract(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Build a deterministic, topic-independent sequence from scene semantics."""
    scenes = [scene for scene in plan.get("scenes", []) if isinstance(scene, dict)]
    if not scenes:
        return plan
    frequencies: Dict[str, int] = {}
    display: Dict[str, str] = {}
    scene_entities: List[List[str]] = []
    for scene in scenes:
        contract = scene.get("semantic_contract", {})
        values = contract.get("required_entities", []) if isinstance(contract, dict) else []
        entities: List[str] = []
        for raw in values if isinstance(values, list) else []:
            value = str(raw).strip()
            key = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
            if not key or key in {re.sub(r"[^a-z0-9]+", " ", item.lower()).strip() for item in entities}:
                continue
            entities.append(value[:120])
            frequencies[key] = frequencies.get(key, 0) + 1
            display.setdefault(key, value[:120])
        scene_entities.append(entities[:10])

    linked_count = sum(bool(str(scene.get("builds_on", "")).strip()) for scene in scenes[1:])
    connected = len(scenes) >= 3 and linked_count >= max(1, (len(scenes) - 1) // 2)
    ranked = sorted(frequencies, key=lambda key: (-frequencies[key], key))
    declared_keep = {
        re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
        for scene in scenes
        for value in (
            scene.get("visual_state", {}).get("keep", [])
            if isinstance(scene.get("visual_state"), dict)
            and isinstance(scene.get("visual_state", {}).get("keep"), list)
            else []
        )
        if str(value).strip()
    }
    # The terminology ledger is also an explicit identity declaration. If a
    # defined term is present in multiple scene entity contracts, preserve it
    # even when the model forgot to repeat visual_state.keep. This remains
    # topic independent and does not infer continuity from mere word reuse.
    ledger_terms = []
    for row in plan.get("terminology_ledger", []) if isinstance(plan.get("terminology_ledger"), list) else []:
        raw = row.get("term") if isinstance(row, dict) else row
        key = re.sub(r"[^a-z0-9]+", " ", str(raw or "").lower()).strip()
        if key:
            ledger_terms.append((key, str(raw).strip()))
    for ledger_key, ledger_display in ledger_terms:
        ledger_stem = (
            ledger_key[:-1]
            if ledger_key.endswith("s") and not ledger_key.endswith("ss")
            else ledger_key
        )
        matching_scenes = sum(
            any(
                re.search(rf"\b{re.escape(ledger_stem)}s?\b", entity.casefold())
                for entity in entities
            )
            for entities in scene_entities
        )
        if matching_scenes >= 2:
            frequencies.setdefault(ledger_key, matching_scenes)
            display.setdefault(ledger_key, ledger_display)
            declared_keep.add(ledger_key)
    ranked = sorted(frequencies, key=lambda key: (-frequencies[key], key))
    stable_subjects = [
        display[key] for key in ranked
        if frequencies[key] >= 2
        and key in declared_keep
    ][:2]
    # A shared composition requires both repeated semantic identity and an
    # explicit director declaration that the object persists. No vocabulary
    # list or topic classifier participates in this decision.
    connected = connected and bool(stable_subjects)
    structure_text = " ".join(
        str(value) for value in [
            plan.get("learning_goal", ""),
            plan.get("explanation_strategy", {}).get("mental_model", "")
            if isinstance(plan.get("explanation_strategy"), dict) else "",
            plan.get("explanation_strategy", {}).get("progression", "")
            if isinstance(plan.get("explanation_strategy"), dict) else "",
        ]
    ).lower()
    if re.search(r"\b(cycle|loop|recurs|repeat|return)\w*\b", structure_text):
        structure = "cycle"
    elif re.search(r"\b(process|journey|stage|step|flow|progress)\w*\b", structure_text):
        structure = "process"
    else:
        structure = "connected_explanation" if connected else "independent_scenes"

    blueprint = {
        "version": 1,
        "mode": "connected" if connected else "independent",
        "structure": structure,
        "group": "master_sequence" if connected else "",
        "stable_subjects": stable_subjects,
        "scene_count": len(scenes),
    }
    for index, scene in enumerate(scenes):
        contract = scene.setdefault("semantic_contract", {})
        if not isinstance(contract, dict):
            contract = {}
            scene["semantic_contract"] = contract
        focus = scene_entities[index]
        contract["visual_sequence"] = {
            **blueprint,
            "scene_position": index + 1,
            "focus_entities": focus,
            "previous_focus_entities": scene_entities[index - 1] if index else [],
            "instruction": (
                "Reuse one master composition and stable semantic object IDs; "
                "keep stable subjects visible, emphasize only this scene's focus, "
                "and show the narrated state change without rebuilding a slide."
                if connected else
                "Use a self-contained composition for this scene."
            ),
        }
        if connected:
            scene["visual_state"] = {
                "mode": "new" if index == 0 else "continue",
                "group": "master_sequence",
                "keep": stable_subjects if index else [],
            }
        else:
            # The deterministic contract is authoritative.  Previously an
            # LLM-authored `continue` flag survived even when this analysis
            # classified the sequence as independent, allowing unrelated
            # geometry to leak across scene boundaries.
            scene["visual_state"] = {
                "mode": "new",
                "group": f"scene_{index + 1}",
                "keep": [],
            }
    plan["visual_sequence_blueprint"] = blueprint
    return plan


_DANGLING_NARRATION_END = re.compile(
    r"\b(?:and|or|but|with|for|to|of|as|including|because|while)\s*[.!?]*$",
    flags=re.I,
)
_PEDAGOGICAL_STOP_WORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "is", "are",
    "it", "this", "that", "then", "show", "shows", "showing", "use", "from",
}
_CONCEPTUAL_CATEGORY_PATTERNS = (
    re.compile(r"\b(?:inertia|straight[- ]line tendency)\b.{0,45}\b(?:outward|push(?:es|ing)?|force)\b", re.I),
    re.compile(r"\b(?:motion|speed|velocity)\b.{0,45}\bpush(?:es|ing)?\b.{0,20}\boutward\b", re.I),
    re.compile(r"\b(?:speed|velocity|motion)\b.{0,35}\b(?:balances?|matches?|equals?|cancels?)\b.{0,35}\b(?:gravity|gravitational pull|force)\b", re.I),
    re.compile(r"\b(?:gravity|gravitational pull|force)\b.{0,45}\b(?:balances?|matches?|equals?|cancels?)\b.{0,45}\b(?:speed|velocity|motion|inertia)\b", re.I),
    re.compile(r"\bacceleration\b.{0,45}\bmatches?\b.{0,25}\b(?:pull|force)\b", re.I),
    re.compile(r"\bopposing arrows?\b.{0,80}\b(?:inertia|motion|tendency)\b", re.I),
    re.compile(r"\b(?:gravity|centripetal)\b.{0,60}\b(?:balance scale|equal[- ]length arrows?|two arrows?)\b", re.I),
    re.compile(r"\b(?:balanced?\s+scale|balance\s+scale)\b.{0,90}\b(?:gravity|gravitational pull|velocity|motion|inertia)\b", re.I),
    re.compile(r"\b(?:gravity|force|pull|acceleration)\b\s+(?:is\s+|gets\s+|becomes\s+)?(?:re)?directed\b", re.I),
    re.compile(r"\b(?:motion|speed|velocity)\b.{0,50}\b(?:redirects?|bends?|changes?\s+(?:the\s+)?direction\s+of)\b.{0,25}\b(?:gravity|force|pull|acceleration)\b", re.I),
    re.compile(r"\b(?:string|tether|track|rail|constraint|constrained)\b.{0,90}\b(?:free[- ]?fall|falls?\s+toward)\b", re.I),
    re.compile(r"\b(?:free[- ]?fall|falls?\s+toward)\b.{0,90}\b(?:string|tether|track|rail|constraint|constrained)\b", re.I),
    re.compile(r"\b(?:shared|common)\s+cent(?:er|re)\s+of\s+mass\b.{0,80}\binside\s+(?:of\s+)?both\b", re.I),
)


def _has_conceptual_category_conflict(value: str) -> bool:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    return any(pattern.search(compact) for pattern in _CONCEPTUAL_CATEGORY_PATTERNS)


def _pedagogical_quality_violations(
    plan: Dict[str, Any], *, include_review_triggers: bool = True
) -> List[Dict[str, Any]]:
    """Find topic-independent script defects before TTS makes them expensive."""
    violations: List[Dict[str, Any]] = []
    for scene in plan.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        scene_id = int(scene.get("id", 0) or 0)
        rows = scene.get("narration_beats") if isinstance(scene.get("narration_beats"), list) else []
        contract = scene.get("semantic_contract") if isinstance(scene.get("semantic_contract"), dict) else {}
        contract_context = " ".join(str(contract.get(key, "")) for key in (
            "claim", "visual_evidence", "before_state", "after_state"
        ))
        for index, beat in enumerate(rows):
            if not isinstance(beat, dict):
                continue
            narration = re.sub(r"\s+", " ", str(beat.get("narration", ""))).strip()
            action = re.sub(r"\s+", " ", str(beat.get("visual_action", ""))).strip()
            reasons: List[str] = []
            if narration and _DANGLING_NARRATION_END.search(narration):
                reasons.append("dangling_ending")
            if re.search(r"\band\s+for\s+the\b", narration, flags=re.I):
                reasons.append("broken_coordination")
            sentence_lengths = [
                len(re.findall(r"\b[\w'-]+\b", sentence))
                for sentence in re.split(r"(?<=[.!?])\s+", narration)
                if sentence.strip()
            ]
            if sentence_lengths and max(sentence_lengths) > 28:
                reasons.append("run_on_sentence")
            if include_review_triggers and re.search(
                r"\b(?:overfits?|underfits?|accuracy|faster|slower|better|worse|"
                r"improves?|decreases?|increases?|similar|causes?|drives?|creates?|"
                r"defines?|influences?|affects?|results?\s+in|leads?\s+to|"
                r"balances?|matches?|equals?|opposes?|cancels?)\b",
                narration,
                flags=re.I,
            ):
                # These may be valid, but they express an outcome/comparison
                # that deserves the bounded source-and-model review below.
                reasons.append("outcome_claim_requires_integrity_review")
            if include_review_triggers and re.search(
                r"\b(?:a\s+)?(?:few|several|many)\s+"
                r"(?:milliseconds?|seconds?|minutes?|hours?|days?|weeks?|months?|years?)\b",
                narration,
                flags=re.I,
            ):
                reasons.append("timing_claim_requires_integrity_review")
            if narration.count(",") >= 4:
                reasons.append("dense_spoken_list")
            if _has_conceptual_category_conflict(
                " ".join((narration, action, contract_context))
            ):
                reasons.append("conceptual_category_conflict")
            content_words = [
                token for token in re.findall(r"[a-z]{6,}", narration.casefold())
                if token not in _PEDAGOGICAL_STOP_WORDS
            ]
            if any(content_words.count(token) >= 3 for token in set(content_words)):
                reasons.append("repetitive_wording")
            narration_terms = {
                token for token in re.findall(r"[a-z0-9]+", narration.casefold())
                if len(token) >= 3 and token not in _PEDAGOGICAL_STOP_WORDS
            }
            action_terms = {
                token for token in re.findall(r"[a-z0-9]+", action.casefold())
                if len(token) >= 3 and token not in _PEDAGOGICAL_STOP_WORDS
            }
            grounded_terms = bool(narration_terms.intersection(action_terms)) or any(
                min(len(narration_term), len(action_term)) >= 4
                and (
                    narration_term.startswith(action_term)
                    or action_term.startswith(narration_term)
                    or len(os.path.commonprefix((narration_term, action_term))) >= 5
                )
                for narration_term in narration_terms
                for action_term in action_terms
            )
            if narration_terms and action_terms and not grounded_terms:
                reasons.append("visual_action_not_grounded_in_narration")
            if reasons:
                violations.append({
                    "scene_id": scene_id,
                    "beat_index": index,
                    "beat_id": beat.get("beat_id"),
                    "reasons": reasons,
                })
    return violations


def _review_pedagogical_integrity(
    plan: Dict[str, Any], source_text: str, job_id: Optional[str]
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Repair grammar and narration/visual mismatch in one bounded review."""
    violations = _pedagogical_quality_violations(plan)
    if not violations or os.environ.get(
        "DIRECTOR_PEDAGOGICAL_REVIEW", "true"
    ).strip().lower() not in {"1", "true", "yes", "on"}:
        return plan, {"status": "pass" if not violations else "disabled", "violations": violations}
    scene_ids = {
        int(scene.get("id", 0) or 0) for scene in plan.get("scenes", [])
        if isinstance(scene, dict)
    }

    def validate_review(value: Dict[str, Any]) -> None:
        rows = value.get("scenes")
        if not isinstance(rows, list):
            raise ValueError("Pedagogical review must return a scenes list.")
        returned = {int(row.get("id", 0) or 0) for row in rows if isinstance(row, dict)}
        if not returned.issubset(scene_ids):
            raise ValueError("Pedagogical review returned an unknown scene id.")
        required = {int(item.get("scene_id", 0) or 0) for item in violations}
        if not required.issubset(returned):
            raise ValueError("Pedagogical review omitted a scene that requires correction.")
        conceptual_scene_ids = {
            int(item.get("scene_id", 0) or 0) for item in violations
            if "conceptual_category_conflict" in item.get("reasons", [])
        }
        for scene_id in conceptual_scene_ids:
            replacement = next(
                (row for row in rows if isinstance(row, dict) and int(row.get("id", 0) or 0) == scene_id),
                {},
            )
            if not isinstance(replacement.get("semantic_contract"), dict):
                raise ValueError(
                    "A conceptual correction must return its synchronized semantic_contract."
                )
            if not str(replacement.get("visual_prompt", "")).strip():
                raise ValueError(
                    "A conceptual correction must return its synchronized visual_prompt."
                )
        candidate = deepcopy(plan)
        replacements = {
            int(row.get("id", 0) or 0): row for row in rows if isinstance(row, dict)
        }
        for scene in candidate.get("scenes", []):
            replacement = replacements.get(int(scene.get("id", 0) or 0))
            if replacement:
                for field in (
                    "narration", "narration_beats", "visual_prompt",
                    "semantic_contract", "learner_takeaway", "visual_state",
                ):
                    if field in replacement:
                        scene[field] = deepcopy(replacement[field])
        remaining = _pedagogical_quality_violations(
            candidate, include_review_triggers=False
        )
        if remaining:
            raise ValueError(f"Pedagogical review left structural defects: {remaining[:6]}")

    payload = {
        "task": (
            "Return JSON with a scenes list containing only corrected scenes. Preserve scene ids and beat ids. "
            "Make every narration sentence complete, natural, concise, and understandable to the declared audience. "
            "Keep sentences speakable, avoid dense consequence lists and repeated wording, and define specialized terms "
            "in plain language before relying on them. Preserve one coherent mental model across scenes. "
            "Audit conceptual categories and causal direction, using your subject knowledge as well as the supplied "
            "material. Do not describe motion, inertia, tendency, probability, absence, or a coordinate-frame effect "
            "as a physical force. Do not say quantities balance, cancel, match, or equal unless they are comparable "
            "and that relation is true in the stated model. Define every equation symbol before the equation is used. "
            "A force or acceleration changes velocity; velocity does not redirect a force. Counterfactuals must name "
            "the changed initial condition directly. An analogy must preserve the causal mechanism and state its "
            "limitation; do not describe motion enforced by a string, track, rail, or other constraint as free fall. "
            "Do not attach a qualitative time such as 'a few seconds' to an outcome unless the source or a sound "
            "derivation establishes it. "
            "For every corrected scene, return narration_beats, visual_prompt, learner_takeaway, and its fully "
            "synchronized semantic_contract. Also "
            "correct learner_takeaway and visual_state when they preserve the same misconception. Keep each corrected "
            "scene close to its original spoken word count so the duration contract remains valid. "
            "Make each visual_action depict exactly the claim spoken in its beat. Do not invent measurements, examples, "
            "experimental outcomes, comparisons, or causal claims; when the supplied material does not establish an "
            "outcome, explain the mechanism without asserting that outcome. Treat real people, places, systems, events, "
            "and documented examples as real examples, never constructed examples. Avoid claims that one factor alone "
            "creates or defines an outcome when the supplied material supports only contribution or influence. Preserve "
            "the lesson progression and meaning."
        ),
        "source_material": str(source_text or "")[:12000],
        "violations": violations,
        "scenes": plan.get("scenes", []),
    }
    try:
        reviewed = _call_json(
            "You are a rigorous educational script editor and visual continuity reviewer. Return JSON only.",
            payload,
            max_tokens=max(2400, min(8000, len(json.dumps(payload)) // 2)),
            stage="director_pedagogical_review",
            job_id=job_id,
            response_validator=validate_review,
        )
        replacements = {
            int(row.get("id", 0) or 0): row
            for row in reviewed.get("scenes", []) if isinstance(row, dict)
        }
        result = deepcopy(plan)
        for scene in result.get("scenes", []):
            replacement = replacements.get(int(scene.get("id", 0) or 0))
            if not replacement:
                continue
            for field in (
                "narration", "narration_beats", "visual_prompt",
                "semantic_contract", "learner_takeaway", "visual_state",
            ):
                if field in replacement:
                    scene[field] = deepcopy(replacement[field])
        remaining = _pedagogical_quality_violations(
            result, include_review_triggers=False
        )
        if remaining:
            raise ValueError(f"Review left pedagogical violations: {remaining[:6]}")
        return result, {
            "status": "repaired", "violations": violations,
            "corrected_scene_ids": sorted(replacements),
        }
    except Exception as error:  # deterministic grammar/alignment safety net
        result = deepcopy(plan)
        by_scene = {
            int(scene.get("id", 0) or 0): scene
            for scene in result.get("scenes", []) if isinstance(scene, dict)
        }
        for violation in violations:
            scene = by_scene.get(int(violation.get("scene_id", 0) or 0))
            if not scene:
                continue
            rows = scene.get("narration_beats") if isinstance(scene.get("narration_beats"), list) else []
            index = int(violation.get("beat_index", -1))
            if not (0 <= index < len(rows)) or not isinstance(rows[index], dict):
                continue
            beat = rows[index]
            narration = re.sub(r"\s+", " ", str(beat.get("narration", ""))).strip()
            narration = re.sub(r"\s+and\s+for\s+the\s+", ". For the ", narration, flags=re.I)
            narration = _DANGLING_NARRATION_END.sub("", narration).rstrip(" ,;:-.!?")
            if "run_on_sentence" in violation.get("reasons", []):
                repaired_sentences = []
                for sentence in re.split(r"(?<=[.!?])\s+", narration):
                    words = sentence.strip().split()
                    if len(words) <= 28:
                        repaired_sentences.append(sentence.strip())
                        continue
                    for start in range(0, len(words), 22):
                        chunk = " ".join(words[start:start + 22]).strip(" ,;:-.!?")
                        if chunk:
                            repaired_sentences.append(chunk[0].upper() + chunk[1:] + ".")
                narration = " ".join(repaired_sentences)
            if narration:
                beat["narration"] = narration[0].upper() + narration[1:] + "."
            if "visual_action_not_grounded_in_narration" in violation.get("reasons", []):
                beat["visual_action"] = (
                    "Depict the concrete objects and change described in this beat: "
                    + str(beat.get("narration", ""))[:260]
                )
            scene["narration"] = " ".join(
                str(row.get("narration", "")).strip()
                for row in rows if isinstance(row, dict)
            ).strip()
            # Never retain a stale visual prompt after narration repair. It is
            # safer and more faithful to compile the already paired beat
            # actions than to render an earlier misconception that the model
            # omitted from its replacement object.
            scene["visual_prompt"] = " ".join(
                str(row.get("visual_action", "")).strip()
                for row in rows if isinstance(row, dict)
                and str(row.get("visual_action", "")).strip()
            )
        remaining = _pedagogical_quality_violations(
            result, include_review_triggers=False
        )
        return result, {
            "status": "repaired_local" if not remaining else "review_failed",
            "violations": violations, "remaining": remaining,
            "error": str(error)[:1000],
        }


def _synchronize_semantic_contracts_after_factual_repair(
    plan: Dict[str, Any], authorized_quantities: List[str]
) -> None:
    """Prevent stale model-created anchors from reintroducing repaired claims."""
    for scene in plan.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        beats = [
            beat for beat in scene.get("narration_beats", [])
            if isinstance(beat, dict)
        ]
        contract = scene.get("semantic_contract")
        if not isinstance(contract, dict):
            contract = {}
            scene["semantic_contract"] = contract
        example_literals = _constructed_example_quantities(contract)
        authorized = {
            _canonical_quantity(value)
            for value in [*authorized_quantities, *example_literals]
            if str(value).strip()
        }
        structural_literals = _beat_aligned_sequence_literals(scene)

        def supported(text: Any, field: str) -> bool:
            return all(
                _canonical_quantity(token) in authorized
                or not quantity_requires_source(
                    token,
                    structural_context=(
                        field.endswith(".visual_action")
                        or _canonical_quantity(token)
                        in structural_literals.get(field, set())
                    ),
                )
                for token in _quantified_tokens(text)
            )
        narration = " ".join(str(beat.get("narration", "")).strip() for beat in beats).strip()
        actions = " Then, ".join(str(beat.get("visual_action", "")).strip() for beat in beats).strip()
        if not supported(contract.get("claim", ""), "semantic_contract.claim"):
            contract["claim"] = narration[:600]
        if not supported(contract.get("visual_evidence", ""), "semantic_contract.visual_evidence"):
            contract["visual_evidence"] = actions[:600]
        for key in ("before_state", "after_state", "inherited_state"):
            if not supported(contract.get(key, ""), f"semantic_contract.{key}"):
                contract[key] = ""
        for key in ("state_facts", "exact_anchors"):
            values = contract.get(key, [])
            if isinstance(values, list):
                contract[key] = [
                    value for value in values
                    if supported(value, f"semantic_contract.{key}")
                ]
        for beat_index, beat in enumerate(beats, start=1):
            anchors = beat.get("exact_anchors", [])
            if isinstance(anchors, list):
                beat["exact_anchors"] = [
                    value for value in anchors
                    if supported(value, f"beat_{beat_index}.exact_anchors")
                ]

def _require_client() -> None:
    if client is None:
        raise RuntimeError(missing_key_message(DIRECTOR_LLM))


def _extract_json_object(text: str) -> str:
    if not text:
        raise ValueError("LLM returned empty content.")
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```$", "", s)
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        pass

    start = s.find("{")
    if start < 0:
        raise ValueError("No JSON object found in LLM output.")
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    raise ValueError("Unclosed JSON object in LLM output.")


def _call_json(
    system_prompt: str,
    user_payload: Any,
    max_tokens: Optional[int] = None,
    stage: str = "director",
    job_id: Optional[str] = None,
    response_validator: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:

    _require_client()

    if isinstance(user_payload, str):
        payload_text = user_payload
    else:
        payload_text = json.dumps(
            user_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    if max_tokens is None:
        max_tokens = int(
            os.environ.get(
                "DIRECTOR_MAX_TOKENS",
                "20000",
            )
        )

    safe_max_tokens = max(
        512,
        min(
            int(max_tokens),
            32768,
        ),
    )

    model_lower = DIRECTOR_MODEL.lower()

    last_error = None

    for attempt in range(1, 4):

        if attempt == 1:

            extra_instruction = """
Return exactly ONE complete valid JSON object.

Do not use markdown.
Do not add commentary outside JSON.

Keep teaching_beat concise.
Keep visual_prompt concise.

Narration may be detailed enough to satisfy the requested
video duration.

Do not duplicate narration inside visual_prompt.

Make sure the entire JSON object is completed before stopping.
""".strip()

        elif attempt == 2:

            extra_instruction = """
RETRY: your previous response was invalid or incomplete.

Return ONE COMPLETE JSON object.

Preserve all educational content and all required scenes.

Reduce unnecessary JSON size:
- shorter teaching beats
- shorter visual prompts
- concise metadata
- no duplicated descriptions

Do NOT remove required teaching content merely to reduce JSON.

Complete the final closing brace.
""".strip()

        else:

            extra_instruction = """
EMERGENCY COMPACT JSON MODE.

Return exactly one JSON object.

Follow the exact required schema and IDs in the user payload. Do not switch to
a different compact schema. Preserve narration_beats and visual_action fields
when the payload requires them. Keep metadata concise so the spoken narration
word contracts can still be satisfied.

Do not output any additional commentary.

The JSON MUST be complete.
""".strip()

        if last_error is not None:
            extra_instruction += (
                "\n\nTHE PREVIOUS RESPONSE FAILED THIS LOCAL CONTRACT:\n"
                + str(last_error)[:1600]
                + "\nCorrect that exact defect. The response will be rejected again "
                "unless every stated scene and word budget passes."
            )

        effective_system = (
            system_prompt
            + "\n\n"
            + extra_instruction
        )

        kwargs = {
            "model": DIRECTOR_MODEL,

            "messages": [
                {
                    "role": "system",
                    "content": effective_system,
                },
                {
                    "role": "user",
                    "content": payload_text,
                },
            ],

            # Set temperature only.
            # Do not simultaneously override top_p.
            "temperature": 0.2,

            "max_tokens": safe_max_tokens,

            "stream": False,

            "_stage": stage,

            "_job_id": job_id,
        }

        kwargs["max_tokens"] = fit_completion_token_budget(
            DIRECTOR_LLM,
            kwargs["messages"],
            safe_max_tokens,
        )

        # ===============================================
        # NEMOTRON
        #
        # Disable thinking for structured JSON calls.
        # We want generation tokens spent on final JSON,
        # not a reasoning trace.
        # ===============================================

        thinking_status = "default"

        if LLM_PROVIDER == "nvidia" and "deepseek-v4" in model_lower:
            kwargs["extra_body"] = {
                "reasoning_effort": "none",
                "chat_template_kwargs": {
                    "thinking": False,
                    "reasoning_effort": "none",
                },
            }
            thinking_status = "disabled"

        elif LLM_PROVIDER in {"groq", "openrouter", "cerebras"}:
            kwargs["response_format"] = {"type": "json_object"}
            if model_lower.startswith("openai/gpt-oss-") or model_lower.startswith("gpt-oss-"):
                if LLM_PROVIDER == "groq":
                    kwargs["extra_body"] = {
                        "reasoning_effort": "low",
                        "include_reasoning": False,
                    }
                elif LLM_PROVIDER == "openrouter":
                    # OpenRouter's normalized reasoning control. Keep a small
                    # reasoning allowance for plan quality but exclude it from
                    # the returned JSON so it cannot corrupt parsing.
                    kwargs["extra_body"] = {
                        "reasoning": {
                            "effort": "low",
                            "exclude": True,
                        }
                    }
                else:
                    # Cerebras accepts the OpenAI-compatible GPT-OSS reasoning
                    # control and automatically hides reasoning in JSON mode.
                    kwargs["extra_body"] = {"reasoning_effort": "low"}
                thinking_status = "low (hidden)"
            elif model_lower.startswith("qwen/qwen3.6-"):
                kwargs["extra_body"] = {
                    "reasoning_effort": "none",
                    "include_reasoning": False,
                }
                thinking_status = "disabled"

        elif "nemotron-3" in model_lower:

            thinking_enabled = (
                os.environ.get(
                    "NEMOTRON_THINKING",
                    "false",
                ).strip().lower()
                in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
            )

            kwargs["extra_body"] = {
                "chat_template_kwargs": {
                    "enable_thinking": (
                        thinking_enabled
                    )
                }
            }
            thinking_status = (
                "enabled"
                if thinking_enabled
                else "disabled"
            )

        print("\n")
        print("=" * 80)
        print(
            f"DIRECTOR ATTEMPT {attempt}/3"
        )
        print("=" * 80)

        print(
            "provider:",
            LLM_PROVIDER,
        )

        print(
            "model:",
            DIRECTOR_MODEL,
        )

        print(
            "max_tokens:",
            kwargs["max_tokens"],
        )

        print(
            "thinking:",
            thinking_status,
        )

        print("=" * 80)

        try:

            from llm_provider import pooled_create

            response = pooled_create(
                client,
                DIRECTOR_LLM,
                kwargs,
                max_key_failovers=-1,
            )

            if not response.choices:
                raise RuntimeError(
                    "Director returned no choices."
                )

            choice = response.choices[0]

            message = choice.message

            content = (
                message.content
                or ""
            )

            finish_reason = getattr(
                choice,
                "finish_reason",
                None,
            )

            reasoning_content = getattr(
                message,
                "reasoning_content",
                None,
            )

            if reasoning_content:

                print(
                    "\n========== DIRECTOR REASONING =========="
                )

                print(
                    reasoning_content
                )

                print(
                    "========================================\n"
                )

            print(
                "\n========== DIRECTOR RAW OUTPUT =========="
            )

            print(
                content
            )

            print(
                "=========================================\n"
            )

            print(
                "finish_reason:",
                finish_reason,
            )

            print(
                "characters:",
                len(content),
            )

            if finish_reason == "length":

                last_error = RuntimeError(
                    "Director reached max_tokens."
                )

                print(
                    "Director output was truncated."
                )

                continue

            if not content.strip():

                last_error = RuntimeError(
                    "Director returned empty output."
                )

                continue

            try:

                json_text = (
                    _extract_json_object(
                        content
                    )
                )

                result = json.loads(
                    json_text
                )

                if not isinstance(
                    result,
                    dict,
                ):
                    raise ValueError(
                        "Director JSON root "
                        "must be an object."
                    )

                # JSON mode guarantees syntax, not application semantics. Some
                # providers return a perfectly valid {"error": ...} object when
                # they decline an oversized structured request. Treat that as a
                # failed attempt so the compact retry modes are actually used.
                if response_validator is not None:
                    response_validator(result)

                print(
                    f"Director attempt "
                    f"{attempt} succeeded."
                )

                return result

            except Exception as parse_error:

                last_error = (
                    parse_error
                )

                print(
                    "Director JSON parse failed:"
                )

                print(
                    repr(
                        parse_error
                    )
                )

                continue

        except Exception as request_error:

            last_error = (
                request_error
            )

            print(
                "Director request failed:"
            )

            print(
                repr(
                    request_error
                )
            )

            continue

    raise RuntimeError(
        "Director failed after 3 attempts. "
        f"Last error: {last_error}"
    )

def narration_word_count(plan: Dict[str, Any]) -> int:
    return sum(
        len(str(scene.get("narration", "")).split())
        for scene in plan.get("scenes", [])
        if isinstance(scene, dict)
    )


def desired_scene_count(target_duration_seconds: float) -> int:
    # Planning one very small scene per ~14 seconds makes long-form JSON grow
    # mostly through repeated schema overhead. Render-time beat compilation can
    # still advance the visual within a scene; the plan therefore only needs
    # enough scenes to avoid an overlong static hold.
    planning_seconds = max(
        12.0,
        float(
            os.environ.get(
                "PLANNING_SCENE_SECONDS_TARGET",
                os.environ.get("MAX_STATIC_SCENE_SECONDS", os.environ.get("MAX_SCENE_HOLD_SECONDS", "20")),
            )
        ),
    )
    value = int((float(target_duration_seconds) + planning_seconds - 1) // planning_seconds)
    return max(MIN_SCENES, min(MAX_SCENES, int(value)))


def _validate_director_plan_response(plan: Dict[str, Any]) -> None:
    """Reject syntactically-valid provider refusals at the LLM boundary."""
    scenes = plan.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        provider_error = str(plan.get("error", "")).strip()
        detail = f" Provider message: {provider_error}" if provider_error else ""
        raise ValueError(
            "Director response did not contain a non-empty scenes array."
            + detail
        )


def _validate_director_scene_contract(
    plan: Dict[str, Any], scene_word_budgets: List[Dict[str, int]],
    *, enforce_words: bool = True,
) -> None:
    """Reject plans that collapse static visual capacity or omit paired beats."""
    _validate_director_plan_response(plan)
    scenes = [scene for scene in plan.get("scenes", []) if isinstance(scene, dict)]
    if len(scenes) < len(scene_word_budgets):
        raise ValueError(
            f"Director returned {len(scenes)} scenes; static visual capacity requires "
            f"at least {len(scene_word_budgets)}."
        )
    for index, budget in enumerate(scene_word_budgets):
        scene = scenes[index]
        beats = [beat for beat in scene.get("narration_beats", []) if isinstance(beat, dict)]
        required = int(budget.get("required_narration_beats", 1))
        paired = [
            beat for beat in beats
            if str(beat.get("narration", "")).strip()
            and str(beat.get("visual_action", "")).strip()
        ]
        if len(paired) < required:
            raise ValueError(
                f"Scene {index + 1} returned {len(paired)} paired narration/visual beats; "
                f"the capacity contract requires {required}."
            )
        actions = {" ".join(str(beat.get("visual_action", "")).lower().split()) for beat in paired}
        if len(actions) != len(paired):
            raise ValueError(f"Scene {index + 1} duplicated one or more visual actions.")
        actual_words = sum(len(str(beat.get("narration", "")).split()) for beat in paired)
        minimum_words = int(
            budget.get("hard_minimum_words", budget.get("minimum_words", 0)) or 0
        )
        maximum_words = int(
            budget.get("hard_maximum_words", budget.get("maximum_words", 10**9))
            or 10**9
        )
        scene_id = int(budget.get("scene_id", index + 1))
        if enforce_words and (actual_words < minimum_words or actual_words > maximum_words):
            raise ValueError(
                f"Scene {scene_id} narration has {actual_words} words; its hard "
                f"required range is {minimum_words}-{maximum_words}. Expand the "
                "explanation with mechanism, intuition, or a concrete consequence "
                "without filler, or compress it without dropping semantic evidence."
            )


def _validate_initial_director_candidate(
    plan: Dict[str, Any], scene_word_budgets: List[Dict[str, int]],
) -> None:
    """Accept a usable draft while reserving strict depth checks for repair.

    A provider response that is one word or one semantic beat short is valuable
    input to the scene-local duration controller. Rejecting it here causes three
    full-plan retries and discards all of its correct scenes. Initial candidates
    therefore have to contain every requested scene and at least one paired
    narration/visual beat per scene. The strict word and visual-capacity contract
    is still enforced after targeted repair, before TTS or visual generation.
    """
    draft_budgets = [
        {**budget, "required_narration_beats": 1}
        for budget in scene_word_budgets
    ]
    _validate_director_scene_contract(
        plan, draft_budgets, enforce_words=False,
    )


def _should_use_hierarchical_planning(
    target_duration_seconds: float, scene_count: int,
) -> bool:
    """Route capacity-heavy requests through bounded completions.

    The legacy enable flag previously allowed a process-level environment value
    to send a five-minute request back to one oversized completion. Long duration
    is now sufficient by itself. An explicit force-monolithic flag remains only
    for controlled diagnostics and tests.
    """
    force_monolithic = os.environ.get(
        "DIRECTOR_FORCE_MONOLITHIC", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if force_monolithic:
        return False
    configured_threshold = max(
        8, int(os.environ.get("DIRECTOR_HIERARCHICAL_SCENE_THRESHOLD", "12"))
    )
    return float(target_duration_seconds) >= 120.0 or int(scene_count) >= configured_threshold


def _ensure_duration_scene_capacity(
    plan: Dict[str, Any], target_duration_seconds: float,
) -> Dict[str, Any]:
    """Prevent long requests from collapsing into a few minute-long slides.

    The director can satisfy a narration word contract while ignoring the
    requested scene count.  Repartition its already paired narration/visual
    beats locally, so no topic-specific templates or extra LLM calls are
    required and every derived scene retains a matching visual instruction.
    """
    result = deepcopy(plan)
    scenes = [scene for scene in result.get("scenes", []) if isinstance(scene, dict)]
    max_hold = max(
        12.0,
        float(os.environ.get("MAX_STATIC_SCENE_SECONDS", os.environ.get("MAX_SCENE_HOLD_SECONDS", "20"))),
    )
    minimum = max(MIN_SCENES, min(MAX_SCENES, int((float(target_duration_seconds) + max_hold - 1) // max_hold)))
    if len(scenes) >= minimum:
        result["scene_capacity"] = {
            "minimum_scene_count": minimum, "actual_scene_count": len(scenes),
            "max_scene_hold_seconds": max_hold, "expanded": False,
        }
        return result

    units: List[Dict[str, Any]] = []
    for source_index, scene in enumerate(scenes):
        beats = [beat for beat in scene.get("narration_beats", []) if isinstance(beat, dict)]
        if not beats and str(scene.get("narration", "")).strip():
            beats = [{
                "beat_id": f"s{source_index + 1}_core", "required": True,
                "priority": 5, "narration": str(scene.get("narration", "")).strip(),
                "visual_action": str(scene.get("visual_prompt", "Visualize this explanation.")).strip(),
                "exact_anchors": [],
            }]
        for beat in beats:
            units.append({"source": source_index, "beat": deepcopy(beat)})

    # A valid long-form plan normally has several semantic beats per scene.
    # If it does not, split only spoken sentences/word runs and retain the
    # paired visual action; the duration repair can then deepen each unit.
    while units and len(units) < minimum:
        index = max(
            range(len(units)),
            key=lambda value: len(str(units[value]["beat"].get("narration", "")).split()),
        )
        unit = units[index]
        words = str(unit["beat"].get("narration", "")).split()
        if len(words) < 12:
            break
        midpoint = len(words) // 2
        first, second = deepcopy(unit), deepcopy(unit)
        first["beat"]["narration"] = " ".join(words[:midpoint])
        second["beat"]["narration"] = " ".join(words[midpoint:])
        base_id = str(unit["beat"].get("beat_id", f"beat_{index + 1}"))
        first["beat"]["beat_id"] = f"{base_id}_a"
        second["beat"]["beat_id"] = f"{base_id}_b"
        units[index:index + 1] = [first, second]

    if len(units) < minimum:
        result["scene_capacity"] = {
            "minimum_scene_count": minimum, "actual_scene_count": len(scenes),
            "max_scene_hold_seconds": max_hold, "expanded": False,
            "reason": "insufficient_semantic_beats",
        }
        return result

    base, remainder = divmod(len(units), minimum)
    expanded, cursor = [], 0
    for scene_index in range(minimum):
        count = base + (1 if scene_index < remainder else 0)
        group = units[cursor:cursor + count]
        cursor += count
        owner = deepcopy(scenes[group[0]["source"]])
        owner["id"] = scene_index + 1
        owner["narration_beats"] = [item["beat"] for item in group]
        owner["narration"] = " ".join(str(item["beat"].get("narration", "")).strip() for item in group).strip()
        owner["visual_prompt"] = " Then, ".join(str(item["beat"].get("visual_action", "")).strip() for item in group).strip()
        owner["builds_on"] = "" if scene_index == 0 else str(scene_index)
        owner["visual_state"] = {"mode": "new", "group": f"scene_{scene_index + 1}", "keep": []}
        expanded.append(owner)
    result["scenes"] = expanded
    result["scene_capacity"] = {
        "minimum_scene_count": minimum, "actual_scene_count": len(expanded),
        "original_scene_count": len(scenes), "max_scene_hold_seconds": max_hold,
        "expanded": True, "strategy": "paired_semantic_beat_repartition",
    }
    return result


def target_word_budget(target_duration_seconds: float) -> int:
    # Use measured raw-voice timing when available. This changes only the
    # numeric budget supplied to the existing director call—no extra LLM call
    # and no extra completion-token allowance.
    try:
        from duration_calibrator import estimate_words

        return max(45, estimate_words(float(target_duration_seconds)))
    except Exception:
        return max(45, round(float(target_duration_seconds) / 60.0 * DIRECTOR_WPM))


def _validate_plan(plan: Dict[str, Any], target_duration_seconds: float) -> Dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("Director plan must be a JSON object.")

    scenes = plan.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("Director plan must contain scenes.")

    cleaned: List[Dict[str, Any]] = []
    previous_visual_group: Optional[str] = None
    known_visual_groups = set()
    for i, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        raw_beats = scene.get("narration_beats", [])
        valid_beats = [beat for beat in raw_beats if isinstance(beat, dict)] if isinstance(raw_beats, list) else []
        narration = str(scene.get("narration", "")).strip()
        visual_prompt = str(scene.get("visual_prompt", "")).strip()
        if not narration and valid_beats:
            narration = " ".join(str(beat.get("narration", "")).strip() for beat in valid_beats).strip()
        if not visual_prompt and valid_beats:
            visual_prompt = " Then, ".join(str(beat.get("visual_action", "")).strip() for beat in valid_beats).strip()
        if not narration or not visual_prompt:
            continue
        raw_state = scene.get("visual_state")
        if not isinstance(raw_state, dict):
            raw_state = {}
        # Backward compatibility for models that omit the new optional field:
        # only explicit continuation language triggers reuse. This is generic
        # discourse analysis, not a topic-specific template.
        prompt_lower = visual_prompt.lower().strip()
        explicit_continuation = bool(
            re.search(
                r"(?:\bfrom (?:the )?(?:previous|prior) scene\b|"
                r"\b(?:same|existing|previous) (?:visual|diagram|canvas|objects?)\b|"
                r"^(?:add|update|extend|overlay|highlight|continue|keep|reuse|modify|replace|move|swap)\b)",
                prompt_lower,
            )
        )
        default_mode = "continue" if explicit_continuation and previous_visual_group else "new"
        mode = str(raw_state.get("mode", default_mode)).strip().lower()
        if mode not in {"new", "continue"} or i == 1:
            mode = "new"
        raw_group = str(raw_state.get("group", "")).strip()
        if mode == "continue" and not raw_group and previous_visual_group:
            raw_group = previous_visual_group
        if not raw_group:
            raw_group = f"scene_{i}"
        group = re.sub(
            r"[^a-z0-9_-]+", "_", raw_group.lower()
        ).strip("_")[:48] or f"scene_{i}"
        # A continuation cannot start a brand-new group: that guarantees the
        # coordinator has no base and silently breaks visual state. Repair the
        # group to the immediately preceding visual sequence. Exact known
        # groups are still allowed, so a scene may intentionally return to an
        # earlier established diagram.
        if mode == "continue" and group not in known_visual_groups:
            if previous_visual_group:
                group = previous_visual_group
            else:
                mode = "new"
        keep = []
        generic_role_names = {
            "input", "output", "decision", "process", "result", "step",
            "state", "start", "end", "value", "data", "action", "logic", "system",
        }
        for value in raw_state.get("keep", []) if isinstance(raw_state.get("keep"), list) else []:
            value = str(value).strip()
            normalized_value = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
            if normalized_value in generic_role_names:
                continue
            if value and value.lower() not in {x.lower() for x in keep}:
                keep.append(value[:60])
            if len(keep) >= 10:
                break
        if mode == "continue" and not keep:
            mode = "new"
            group = f"scene_{i}"
        try:
            importance = max(0.0, min(1.0, float(scene.get("importance", 0.5))))
        except (TypeError, ValueError):
            importance = 0.5
        cleaned.append(
            {
                "id": int(scene.get("id", i)) if str(scene.get("id", i)).isdigit() else i,
                "teaching_beat": str(scene.get("teaching_beat", f"Beat {i}")).strip()[:300],
                "narration": narration,
                "visual_prompt": visual_prompt,
                "narration_beats": valid_beats,
                "importance": importance,
                "teaching_role": str(scene.get("teaching_role", "explain")).strip()[:80],
                "learner_takeaway": str(scene.get("learner_takeaway", "")).strip()[:500],
                "builds_on": str(scene.get("builds_on", "")).strip()[:300],
                "semantic_contract": scene.get("semantic_contract")
                if isinstance(scene.get("semantic_contract"), dict) else {},
                "visual_state": {
                    "mode": mode,
                    "group": group,
                    "keep": keep,
                },
            }
        )
        previous_visual_group = group
        known_visual_groups.add(group)

    if not cleaned:
        raise ValueError("Director plan contained no usable scenes.")

    for i, scene in enumerate(cleaned, start=1):
        scene["id"] = i

    plan["scenes"] = cleaned
    plan["target_duration_seconds"] = float(target_duration_seconds)
    plan["must_cover"] = [str(x).strip() for x in plan.get("must_cover", []) if str(x).strip()]
    plan["out_of_scope"] = [str(x).strip() for x in plan.get("out_of_scope", []) if str(x).strip()]
    plan["title"] = str(plan.get("title", "Explainer")).strip() or "Explainer"
    plan["learning_goal"] = str(plan.get("learning_goal", "")).strip()
    plan["scope_summary"] = str(plan.get("scope_summary", "")).strip()
    return plan


def _duration_scene_word_contracts(total_words: int, scene_count: int) -> List[Dict[str, int]]:
    """Create exact, topic-independent scene budgets for duration repair."""
    scene_count = max(1, int(scene_count))
    base, remainder = divmod(max(scene_count, int(total_words)), scene_count)
    contracts: List[Dict[str, int]] = []
    for index in range(scene_count):
        target = base + (1 if index < remainder else 0)
        contracts.append({
            "scene_id": index + 1,
            "target_words": target,
            "minimum_words": max(8, round(target * 0.92)),
            "maximum_words": max(10, round(target * 1.08)),
        })
    return contracts


def _duration_beat_word_contracts(
    scene_target_words: int, beats: List[Dict[str, Any]],
) -> List[Dict[str, int]]:
    """Give the model countable beat budgets whose sums fit the scene budget.

    A scene-level range is ambiguous when a scene contains several beats: models
    commonly write a full scene for *each* beat.  Tight, integer per-beat targets
    remove that ambiguity while keeping the allocation topic-independent.
    """
    count = max(1, len(beats))
    base, remainder = divmod(max(count * 8, int(scene_target_words)), count)
    contracts: List[Dict[str, int]] = []
    for index in range(count):
        target = base + (1 if index < remainder else 0)
        contracts.append({
            "beat_index": index + 1,
            "target_words": target,
            "minimum_words": max(6, target - 1),
            "maximum_words": target + 1,
        })
    return contracts


def _merge_preflight_repair(
    original: Dict[str, Any],
    repair: Dict[str, Any],
    target_duration_seconds: float,
) -> Dict[str, Any]:
    """Merge only repaired narration/visual pairs into the authoritative plan."""
    repaired_scenes = repair.get("scenes") if isinstance(repair, dict) else None
    if not isinstance(repaired_scenes, list):
        raise ValueError("Duration repair did not return a scenes array.")

    repaired_by_id: Dict[int, Dict[str, Any]] = {}
    for position, scene in enumerate(repaired_scenes, start=1):
        if not isinstance(scene, dict):
            continue
        try:
            scene_id = int(scene.get("id", position))
        except (TypeError, ValueError):
            scene_id = position
        repaired_by_id[scene_id] = scene

    merged = deepcopy(original)
    changed = 0
    for position, scene in enumerate(merged.get("scenes", []), start=1):
        candidate = repaired_by_id.get(position)
        if not isinstance(candidate, dict):
            continue
        raw_beats = candidate.get("narration_beats")
        valid_beats = []
        if isinstance(raw_beats, list):
            for beat_index, beat in enumerate(raw_beats, start=1):
                if not isinstance(beat, dict):
                    continue
                narration = str(beat.get("narration", "")).strip()
                visual_action = str(beat.get("visual_action", "")).strip()
                if not narration or not visual_action:
                    continue
                try:
                    priority = max(1, min(5, int(beat.get("priority", 3))))
                except (TypeError, ValueError):
                    priority = 3
                valid_beats.append({
                    "beat_id": str(beat.get("beat_id", f"s{position}_b{beat_index}"))[:64],
                    "required": bool(beat.get("required", True)),
                    "priority": priority,
                    "narration": narration,
                    "visual_action": visual_action,
                    "exact_anchors": beat.get("exact_anchors", [])
                    if isinstance(beat.get("exact_anchors"), list) else [],
                })
        if not valid_beats:
            continue
        scene["narration_beats"] = valid_beats
        scene["narration"] = " ".join(beat["narration"] for beat in valid_beats)
        scene["visual_prompt"] = " Then, ".join(
            beat["visual_action"] for beat in valid_beats
        )
        if bool(candidate.get("constructed_example", False)):
            contract = scene.get("semantic_contract")
            if not isinstance(contract, dict):
                contract = {}
                scene["semantic_contract"] = contract
            example_literals = candidate.get("example_literals", [])
            contract["constructed_example"] = True
            contract["example_context"] = str(
                candidate.get("example_context", "hypothetical teaching example")
            ).strip()[:300]
            contract["example_literals"] = [
                str(value).strip()[:80] for value in example_literals[:40]
                if str(value).strip()
            ] if isinstance(example_literals, list) else []
        changed += 1

    if changed != len(merged.get("scenes", [])):
        raise ValueError(
            "Duration repair omitted one or more usable scene narration/visual pairs."
        )
    return _validate_plan(merged, target_duration_seconds)


def _repair_duration_in_batches(
    plan: Dict[str, Any], target_duration_seconds: float,
    scene_contracts: List[Dict[str, int]], completion_token_budget: int,
    job_id: Optional[str], repair_round: int,
) -> Dict[str, Any]:
    """Repair only scenes outside their word contracts in bounded batches."""
    scenes = [scene for scene in plan.get("scenes", []) if isinstance(scene, dict)]
    by_id = {int(scene.get("id", index)): scene for index, scene in enumerate(scenes, 1)}
    failing: List[Dict[str, int]] = []
    visual_words_per_beat = max(
        20, int(os.environ.get("VISUAL_WORDS_PER_BEAT", "32"))
    )
    for contract in scene_contracts:
        scene_id = int(contract["scene_id"])
        scene = by_id.get(scene_id, {})
        actual = sum(
            len(str(beat.get("narration", "")).split())
            for beat in scene.get("narration_beats", []) if isinstance(beat, dict)
        )
        existing_beat_count = len([
            beat for beat in scene.get("narration_beats", [])
            if isinstance(beat, dict) and str(beat.get("narration", "")).strip()
            and str(beat.get("visual_action", "")).strip()
        ])
        required_beat_count = max(
            1, min(6, (int(contract["target_words"]) + visual_words_per_beat - 1)
                       // visual_words_per_beat)
        )
        if (
            actual < int(contract["minimum_words"])
            or actual > int(contract["maximum_words"])
            or existing_beat_count < required_beat_count
        ):
            failing.append({
                **contract, "actual_words": actual,
                "existing_beat_count": existing_beat_count,
                "required_beat_count": required_beat_count,
            })
    if not failing:
        return deepcopy(plan)

    expand_batch_size = max(
        1, min(5, int(os.environ.get("DIRECTOR_DURATION_EXPAND_BATCH_SIZE", "4")))
    )
    # Compression is deliberately scene-local.  A model can reason about one
    # exact scene/beat budget reliably, and one bad scene can no longer discard
    # otherwise valid repairs from the same batch.
    rewrite_batch_size = max(
        1, min(2, int(os.environ.get("DIRECTOR_DURATION_REWRITE_BATCH_SIZE", "1")))
    )
    short = [item for item in failing if int(item["actual_words"]) < int(item["minimum_words"])]
    rewrite = [item for item in failing if item not in short]
    batches = [
        ("expand", short[start:start + expand_batch_size])
        for start in range(0, len(short), expand_batch_size)
    ] + [
        ("rewrite", rewrite[start:start + rewrite_batch_size])
        for start in range(0, len(rewrite), rewrite_batch_size)
    ]

    def repair_batch(mode: str, contracts: List[Dict[str, int]]) -> List[Dict[str, Any]]:
        expected_ids = [int(contract["scene_id"]) for contract in contracts]
        validation_budgets = []
        scene_rows = []
        for contract in contracts:
            scene = by_id[int(contract["scene_id"])]
            existing_beats = [
                beat for beat in scene.get("narration_beats", []) if isinstance(beat, dict)
            ]
            validation_budgets.append({
                **contract,
                "required_narration_beats": int(contract["required_beat_count"]),
            })
            scene_rows.append({
                "id": scene.get("id"),
                "teaching_beat": scene.get("teaching_beat"),
                "teaching_role": scene.get("teaching_role"),
                "learner_takeaway": scene.get("learner_takeaway"),
                "semantic_contract": scene.get("semantic_contract", {}),
                "narration_beats": existing_beats,
                "word_contract": contract,
                "additional_word_range": [
                    max(1, int(contract["minimum_words"]) - int(contract["actual_words"])),
                    max(1, int(contract["maximum_words"]) - int(contract["actual_words"])),
                ],
                "required_additional_beats": max(
                    1,
                    int(contract["required_beat_count"])
                    - int(contract["existing_beat_count"]),
                ),
                "beat_word_contracts": (
                    _duration_beat_word_contracts(
                        int(contract["target_words"]), existing_beats
                    ) if mode == "rewrite" else []
                ),
            })
        repair_payload = {
            "task": (
                (
                    "Generate only the missing additional narration beats for each short "
                    "scene. Do not repeat or rewrite its existing beats. Each returned scene's "
                    "narration_beats array contains additions only. Keep every addition paired "
                    "with a distinct visual_action. The combined added narration word count "
                    "must be inside additional_word_range. Deepen only the supplied claim, "
                    "entities, state facts, mechanism, and example context. Do not introduce a "
                    "new consequence, application, technical term, or adjacent topic merely to "
                    "fill time. Every added sentence must be teachable by its paired visual action."
                ) if mode == "expand" else (
                    "Rewrite each supplied scene into the required total word range and required "
                    "beat count. Each returned beat at position N must satisfy the matching "
                    "beat_word_contracts entry at position N. Preserve meaning, facts, exact "
                    "anchors, semantic evidence, and distinct visual-action pairing. Return the "
                    "complete replacement beats; do not write a scene-sized explanation in each beat."
                )
            ),
            "repair_round": repair_round,
            "scenes": scene_rows,
            "required_output": {"scenes": "same IDs with complete narration_beats"},
        }

        def validate(result: Dict[str, Any]) -> None:
            _coerce_single_scene_response(result, expected_ids)
            if mode == "rewrite":
                _validate_director_scene_contract(result, validation_budgets)
            else:
                _validate_director_plan_response(result)
            actual_ids = [
                int(scene.get("id", 0)) for scene in result.get("scenes", [])
                if isinstance(scene, dict)
            ]
            if actual_ids != expected_ids:
                raise ValueError(
                    f"Duration repair returned scene IDs {actual_ids}; expected {expected_ids}."
                )
            if mode == "expand":
                for returned, contract, original_row in zip(
                    result.get("scenes", []), contracts, scene_rows
                ):
                    additions = [
                        beat for beat in returned.get("narration_beats", [])
                        if isinstance(beat, dict)
                        and str(beat.get("narration", "")).strip()
                        and str(beat.get("visual_action", "")).strip()
                    ]
                    required_additions = int(original_row["required_additional_beats"])
                    if len(additions) < required_additions:
                        raise ValueError(
                            f"Scene {contract['scene_id']} needs {required_additions} "
                            f"additional paired beats; received {len(additions)}."
                        )
                    added_words = sum(
                        len(str(beat.get("narration", "")).split()) for beat in additions
                    )
                    minimum_add, maximum_add = original_row["additional_word_range"]
                    if not int(minimum_add) <= added_words <= int(maximum_add):
                        raise ValueError(
                            f"Scene {contract['scene_id']} additions contain {added_words} words; "
                            f"the exact missing-depth range is {minimum_add}-{maximum_add}."
                        )
                    existing_actions = {
                        " ".join(str(beat.get("visual_action", "")).lower().split())
                        for beat in original_row["narration_beats"]
                    }
                    added_actions = [
                        " ".join(str(beat.get("visual_action", "")).lower().split())
                        for beat in additions
                    ]
                    if len(set(added_actions)) != len(added_actions) or any(
                        action in existing_actions for action in added_actions
                    ):
                        raise ValueError(
                            f"Scene {contract['scene_id']} additional visual actions must be distinct."
                        )
            else:
                for returned, original_row in zip(result.get("scenes", []), scene_rows):
                    beats = [
                        beat for beat in returned.get("narration_beats", [])
                        if isinstance(beat, dict)
                    ]
                    for beat, beat_contract in zip(
                        beats, original_row["beat_word_contracts"]
                    ):
                        words = len(str(beat.get("narration", "")).split())
                        if not (
                            int(beat_contract["minimum_words"]) <= words
                            <= int(beat_contract["maximum_words"])
                        ):
                            raise ValueError(
                                f"Scene {original_row['id']} beat "
                                f"{beat_contract['beat_index']} has {words} words; its exact "
                                f"range is {beat_contract['minimum_words']}-"
                                f"{beat_contract['maximum_words']}."
                            )

        batch_target_words = sum(
            (
                int(contract["maximum_words"]) - int(contract["actual_words"])
                if mode == "expand" else int(contract["target_words"])
            )
            for contract in contracts
        )
        result = _call_json(
            PREFLIGHT_REPAIR_SYSTEM_PROMPT, repair_payload,
            # GPT-OSS occasionally spends almost the whole 1,400-token budget
            # on reasoning and emits no JSON.  This is a ceiling, not reserved
            # usage; a normal compact response still stops after a few hundred
            # tokens.
            max_tokens=min(
                completion_token_budget,
                max(
                    int(os.environ.get("DIRECTOR_DURATION_REPAIR_MIN_TOKENS", "2600")),
                    batch_target_words * 12,
                ),
            ),
            stage=f"director_duration_{mode}_{expected_ids[0]}_{expected_ids[-1]}",
            job_id=job_id, response_validator=validate,
        )
        if mode == "rewrite":
            return result["scenes"]
        expanded_scenes = []
        for returned, original_row in zip(result["scenes"], scene_rows):
            original_scene = deepcopy(by_id[int(original_row["id"])])
            existing_beats = [
                deepcopy(beat) for beat in original_scene.get("narration_beats", [])
                if isinstance(beat, dict)
            ]
            used_ids = {str(beat.get("beat_id", "")) for beat in existing_beats}
            for offset, beat in enumerate(returned.get("narration_beats", []), 1):
                addition = deepcopy(beat)
                proposed = str(addition.get("beat_id", "")).strip()
                if not proposed or proposed in used_ids:
                    proposed = f"s{original_row['id']}_depth_{len(existing_beats) + offset}"
                addition["beat_id"] = proposed
                addition["required"] = True
                used_ids.add(proposed)
                existing_beats.append(addition)
            original_scene["narration_beats"] = existing_beats
            expanded_scenes.append(original_scene)
        _validate_director_scene_contract(expanded_scenes and {"scenes": expanded_scenes}, validation_budgets)
        return expanded_scenes

    repaired_by_id: Dict[int, Dict[str, Any]] = {}
    repair_errors: List[str] = []
    workers = min(len(batches), max(1, int(os.environ.get("DIRECTOR_BATCH_WORKERS", "3"))))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(repair_batch, mode, batch) for mode, batch in batches]
        for future in as_completed(futures):
            try:
                for scene in future.result():
                    repaired_by_id[int(scene.get("id"))] = scene
            except Exception as error:  # preserve successful scene-local repairs
                repair_errors.append(str(error))

    combined_scenes = [
        repaired_by_id.get(int(scene.get("id", index)), scene)
        for index, scene in enumerate(scenes, 1)
    ]
    repaired = _merge_preflight_repair(
        plan, {"scenes": combined_scenes}, target_duration_seconds
    )
    repaired["_duration_batch_repair_calls"] = len(batches)
    if job_id:
        from run_log import log_local_transform
        log_local_transform(
            job_id, "duration_scene_batch_repair",
            candidate_id=f"duration_round_{repair_round}", before=plan, after=repaired,
            details={
                "failing_scene_ids": [int(item["scene_id"]) for item in failing],
                "batch_count": len(batches),
                "successful_scene_ids": sorted(repaired_by_id),
                "errors": repair_errors,
            }, status="repaired",
        )
    return repaired


def _balanced_batch_ranges(total: int, maximum_size: int) -> List[Tuple[int, int]]:
    """Use the fewest batches while avoiding one oversized batch + singleton."""
    total = max(0, int(total))
    maximum_size = max(1, int(maximum_size))
    if not total:
        return []
    count = max(1, math.ceil(total / maximum_size))
    base, remainder = divmod(total, count)
    ranges: List[Tuple[int, int]] = []
    start = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        ranges.append((start, start + size))
        start += size
    return ranges


def _hierarchical_director_plan(
    payload: Dict[str, Any], scene_word_budgets: List[Dict[str, int]],
    completion_token_budget: int, job_id: Optional[str],
) -> Dict[str, Any]:
    """Build long-form plans in bounded, contract-sharing completions."""
    outline_payload = {
        key: deepcopy(payload.get(key)) for key in (
            "user_request", "audience", "target_duration_seconds",
            "narration_word_budget", "duration_calibration",
            "desired_scene_count", "duration_teaching_contract",
            "source_document",
        ) if key in payload
    }
    outline_payload.update({
        "task": (
            "Create the global lesson strategy and exactly one ordered scene brief "
            "per scene. Do not write final narration yet. Establish a fact ledger, "
            "terminology ledger, narrative thread, dependencies, semantic evidence, "
            "and continuity intent that every later batch must obey. HARD SIZE "
            "CONTRACT: keep global prose concise; each brief field must be one short "
            "sentence, required_entities/state_facts/exact_anchors must contain only "
            "irreducible evidence, the fact ledger may have at most 25 atomic rows, "
            "and the terminology ledger at most 15 rows. Do not include narration."
        ),
        "scene_word_budgets": scene_word_budgets,
        "required_schema": {
            "title": "string", "learning_goal": "string",
            "audience_profile": {
                "prior_knowledge": "one short sentence",
                "learning_goal": "one short sentence",
                "language_and_tone": "one short sentence",
                "terms_to_define": ["maximum 8 essential terms"],
            },
            "explanation_strategy": {
                "central_question": "one sentence", "core_takeaway": "one sentence",
                "mental_model": "one sentence", "narrative_thread": "one sentence",
                "primary_example": "one sentence",
                "progression": ["one short phrase per scene"],
                "excluded_for_capacity": ["short phrase"],
            },
            "example_contract": {
                "constructed_example": "boolean",
                "example_context": "one short sentence",
                "example_literals": ["every model-created literal used by the primary example"],
            },
            "scope_summary": "string", "must_cover": ["string"],
            "out_of_scope": ["string"],
            "fact_ledger": [{"claim": "atomic claim", "status": "model-known, user-provided, source-provided, or qualitative"}],
            "terminology_ledger": [{"term": "stable term", "meaning": "stable meaning"}],
            "scene_briefs": [{
                "id": 1, "teaching_beat": "string", "teaching_role": "string",
                "learner_takeaway": "string", "builds_on": "string",
                "narration_objective": "mechanism/example/conclusion to explain",
                "visible_evidence": "one short concrete visual requirement",
                "required_entities": ["maximum 4 concrete entities"],
                "continuity_group": "short stable group or independent",
            }],
        },
    })

    def validate_outline(result: Dict[str, Any]) -> None:
        briefs = result.get("scene_briefs") if isinstance(result, dict) else None
        if not isinstance(briefs, list) or len(briefs) != len(scene_word_budgets):
            raise ValueError(
                f"Hierarchical outline requires exactly {len(scene_word_budgets)} scene briefs."
            )
        ids = [int(brief.get("id", 0)) for brief in briefs if isinstance(brief, dict)]
        if ids != list(range(1, len(scene_word_budgets) + 1)):
            raise ValueError("Hierarchical outline scene IDs must be complete and ordered.")
        example_contract = result.get("example_contract")
        if not isinstance(example_contract, dict):
            # Backward-compatible canonicalization: a missing declaration is
            # safely non-constructed.  Do not spend a full outline retry for
            # optional provenance metadata when no model-created literals were
            # declared.
            example_contract = {
                "constructed_example": False,
                "example_context": "",
                "example_literals": [],
            }
            result["example_contract"] = example_contract
        if bool(example_contract.get("constructed_example", False)) and (
            not str(example_contract.get("example_context", "")).strip()
            or not isinstance(example_contract.get("example_literals"), list)
            or not example_contract.get("example_literals")
        ):
            raise ValueError(
                "A constructed primary example requires context and explicit literals."
            )

    outline_system_prompt = """
You create a compact global outline for a long educational video. You do not
write narration or full scene JSON. Return exactly one valid JSON object using
only the schema requested by the user payload.

Hard rules:
- Return exactly the requested number of ordered scene_briefs and IDs.
- Every prose field is one concise sentence of at most 24 words.
- Every scene brief uses at most 4 required_entities.
- fact_ledger contains at most 25 atomic rows; terminology_ledger at most 15.
- Do not repeat the user request, explain your reasoning, or add narration_beats.
- If primary_example contains model-created values, declare them in example_contract
  with constructed_example=true. If it is qualitative or source-provided, keep the
  literal list empty and state that provenance. Never label a real-world claim as constructed.
- Keep the complete JSON comfortably below the completion limit.
""".strip()
    try:
        outline = _call_json(
            outline_system_prompt, outline_payload,
            max_tokens=min(completion_token_budget, max(2200, len(scene_word_budgets) * 140)),
            stage="director_strategy", job_id=job_id, response_validator=validate_outline,
        )
    except Exception as combined_outline_error:
        # If even a compact combined outline reaches a provider limit, split
        # strategy from scene indexing instead of falling back to one enormous
        # monolithic lesson completion.
        global_payload = {
            key: deepcopy(value) for key, value in outline_payload.items()
            if key not in {"scene_word_budgets", "required_schema"}
        }
        global_payload.update({
            "task": (
                "Return only the concise global teaching contract. Do not return "
                "scene briefs or narration. progression must contain exactly "
                f"{len(scene_word_budgets)} short ordered phrases."
            ),
            "required_schema": {
                key: deepcopy(outline_payload["required_schema"][key])
                for key in (
                    "title", "learning_goal", "audience_profile",
                    "explanation_strategy", "example_contract", "scope_summary", "must_cover",
                    "out_of_scope", "fact_ledger", "terminology_ledger",
                )
            },
        })
        global_outline = _call_json(
            outline_system_prompt, global_payload,
            max_tokens=min(completion_token_budget, 1100),
            stage="director_strategy_global", job_id=job_id,
        )
        brief_batches = [
            scene_word_budgets[start:start + 5]
            for start in range(0, len(scene_word_budgets), 5)
        ]

        def build_brief_batch(budgets: List[Dict[str, int]]) -> List[Dict[str, Any]]:
            expected_ids = [int(item["scene_id"]) for item in budgets]
            brief_payload = {
                "task": (
                    "Create only the assigned compact scene_briefs. Use the matching "
                    "progression phrases from the global contract. No narration."
                ),
                "global_contract": global_outline,
                "assigned_scene_word_budgets": budgets,
                "required_schema": {
                    "scene_briefs": outline_payload["required_schema"]["scene_briefs"]
                },
            }

            def validate_briefs(result: Dict[str, Any]) -> None:
                briefs = result.get("scene_briefs") if isinstance(result, dict) else None
                actual_ids = [
                    int(brief.get("id", 0)) for brief in briefs or []
                    if isinstance(brief, dict)
                ]
                if actual_ids != expected_ids:
                    raise ValueError(
                        f"Outline batch IDs {actual_ids} did not match {expected_ids}."
                    )

            result = _call_json(
                outline_system_prompt, brief_payload,
                max_tokens=min(completion_token_budget, 950),
                stage=f"director_outline_batch_{expected_ids[0]}_{expected_ids[-1]}",
                job_id=job_id, response_validator=validate_briefs,
            )
            return result["scene_briefs"]

        completed_briefs: Dict[int, List[Dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=min(3, len(brief_batches))) as executor:
            future_map = {
                executor.submit(build_brief_batch, budgets): int(budgets[0]["scene_id"])
                for budgets in brief_batches
            }
            for future in as_completed(future_map):
                completed_briefs[future_map[future]] = future.result()
        outline = deepcopy(global_outline)
        outline["scene_briefs"] = [
            brief for start in sorted(completed_briefs)
            for brief in completed_briefs[start]
        ]
        outline["outline_fallback"] = {
            "strategy": "global_plus_bounded_brief_batches",
            "combined_outline_error": str(combined_outline_error)[:1200],
        }
        validate_outline(outline)
    briefs = outline["scene_briefs"]
    batch_size = max(2, min(6, int(os.environ.get("DIRECTOR_SCENE_BATCH_SIZE", "5"))))
    batch_ranges = _balanced_batch_ranges(len(briefs), batch_size)
    batches = [
        (start, briefs[start:end], scene_word_budgets[start:end])
        for start, end in batch_ranges
    ]

    def expand_batch(batch: Any) -> Any:
        start, selected_briefs, selected_budgets = batch
        before = briefs[start - 1] if start > 0 else None
        after_index = start + len(selected_briefs)
        after = briefs[after_index] if after_index < len(briefs) else None
        batch_payload = {
            "task": (
                "Expand only these scene briefs into complete narration_beats and "
                "matching visual_action fields. Preserve IDs, facts, terms, semantic "
                "contracts, dependencies, word budgets, and continuity. Count only "
                "narration_beats[*].narration words for each scene before returning. "
                "Every scene must fall inside its own hard minimum/maximum range; "
                "also keep each narration beat within one word of that scene's "
                "target_words_per_beat (the final beat absorbs any remainder). "
                "A beat is one semantic unit, not another complete scene script. "
                "a concise summary below the minimum is invalid. Reach the range by "
                "deepening the mechanism, intuition, example, or consequence—not by "
                "adding filler or repeating another scene."
            ),
            "global_contract": {
                key: deepcopy(outline.get(key)) for key in (
                    "title", "learning_goal", "audience_profile", "explanation_strategy",
                    "example_contract", "scope_summary", "must_cover", "out_of_scope", "fact_ledger",
                    "terminology_ledger",
                )
            },
            "adjacent_scene_summaries": {"previous": before, "next": after},
            "scene_briefs": selected_briefs,
            "scene_word_budgets": selected_budgets,
            "required_scene_schema": payload["required_schema"]["scenes"][0],
            "factuality_policy": {
                "empirical_quantities": (
                    "Closed-world: use exact empirical quantities only when present in the "
                    "user request or source document. Otherwise explain qualitatively."
                ),
                "constructed_examples": (
                    "Model-created teaching/test values are allowed only when declared in "
                    "semantic_contract.example_literals with constructed_example=true and "
                    "a non-empty hypothetical example_context."
                ),
            },
            "example_provenance_rule": (
                "If global_contract.example_contract.constructed_example is true, every scene "
                "that uses those model-created values must copy constructed_example=true, "
                "example_context, and all used values into semantic_contract.example_literals."
            ),
        }

        expected_ids = [int(item["scene_id"]) for item in selected_budgets]

        def validate_batch(result: Dict[str, Any]) -> None:
            _coerce_single_scene_response(result, expected_ids)
            # Word-short output remains an immutable intermediate candidate.
            # The duration controller below repairs only deficient scenes;
            # structural defects still retry this whole batch immediately.
            _validate_initial_director_candidate(result, selected_budgets)
            actual_ids = [
                int(scene.get("id", 0)) for scene in result.get("scenes", [])
                if isinstance(scene, dict)
            ]
            if actual_ids != expected_ids:
                raise ValueError(
                    f"Director batch IDs {actual_ids} did not match {expected_ids}."
                )

        batch_words = sum(int(item["target_spoken_words"]) for item in selected_budgets)
        result = _call_json(
            DIRECTOR_SYSTEM_PROMPT, batch_payload,
            # A scene carries visual actions and semantic metadata in addition
            # to narration. The previous 12x ceiling truncated otherwise
            # useful multi-scene JSON at 3,500 tokens and paid for a full
            # retry. A higher ceiling does not consume tokens when the model
            # stops normally; it only lets a valid closing brace arrive.
            max_tokens=min(
                completion_token_budget,
                max(4500, round(batch_words * 18), len(selected_budgets) * 900),
            ),
            stage=f"director_scene_batch_{expected_ids[0]}_{expected_ids[-1]}",
            job_id=job_id, response_validator=validate_batch,
        )
        word_diagnostics = []
        for scene, contract in zip(result["scenes"], selected_budgets):
            actual_words = sum(
                len(str(beat.get("narration", "")).split())
                for beat in scene.get("narration_beats", []) if isinstance(beat, dict)
            )
            word_diagnostics.append({
                "scene_id": int(contract["scene_id"]),
                "actual_words": actual_words,
                "minimum_words": int(contract["hard_minimum_words"]),
                "maximum_words": int(contract["hard_maximum_words"]),
                "status": (
                    "pass" if int(contract["hard_minimum_words"]) <= actual_words
                    <= int(contract["hard_maximum_words"]) else "repair_required"
                ),
            })
        return start, result["scenes"], word_diagnostics

    completed: Dict[int, List[Dict[str, Any]]] = {}
    batch_word_diagnostics: Dict[int, List[Dict[str, Any]]] = {}
    workers = min(len(batches), max(1, int(os.environ.get("DIRECTOR_BATCH_WORKERS", "3"))))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(expand_batch, batch) for batch in batches]
        for future in as_completed(futures):
            start, scenes, word_diagnostics = future.result()
            completed[start] = scenes
            batch_word_diagnostics[start] = word_diagnostics

    merged = {
        key: deepcopy(value) for key, value in outline.items()
        if key not in {"scene_briefs"}
    }
    merged["scenes"] = [
        scene for start in sorted(completed) for scene in completed[start]
    ]
    merged["hierarchical_planning"] = {
        "enabled": True, "batch_size": batch_size,
        "actual_batch_sizes": [end - start for start, end in batch_ranges],
        "batch_count": len(batches), "shared_fact_ledger": True,
        "shared_terminology_ledger": True,
        "initial_scene_word_contracts": [
            row for start in sorted(batch_word_diagnostics)
            for row in batch_word_diagnostics[start]
        ],
    }
    _validate_director_scene_contract(
        merged,
        [{**budget, "required_narration_beats": 1} for budget in scene_word_budgets],
        enforce_words=False,
    )
    return merged


def create_video_plan(
    user_prompt: str,
    target_duration_seconds: float,
    audience: str = "general learner",
    job_id: Optional[str] = None,
    source_text: str = "",
) -> Dict[str, Any]:
    user_prompt = str(user_prompt).strip()
    if not user_prompt:
        raise ValueError("User prompt is required.")
    target_duration_seconds = float(target_duration_seconds)
    if target_duration_seconds < 20:
        raise ValueError("Video duration must be at least 20 seconds.")

    scene_count = desired_scene_count(target_duration_seconds)
    from duration_calibrator import duration_budget

    calibrated_budget = duration_budget(target_duration_seconds, scene_count)
    word_budget = int(calibrated_budget["target_words"])
    min_words = round(word_budget * 0.90)
    max_words = round(word_budget * 1.10)
    base_scene_words, remainder = divmod(word_budget, scene_count)
    scene_word_budgets = []
    for index in range(scene_count):
        target = base_scene_words + (1 if index < remainder else 0)
        # Semantic units scale continuously with the words assigned to the
        # scene. This matters when long requests hit the configured scene cap:
        # a longer scene still advances through several matched narration /
        # visual beats instead of holding one slide for a minute.
        # Supply enough independently countable semantic units that the model
        # naturally reaches the scene contract instead of returning one terse
        # summary sentence. The rule scales only from requested duration.
        visual_words_per_beat = max(
            20, int(os.environ.get("VISUAL_WORDS_PER_BEAT", "32"))
        )
        required_beats = max(1, min(6, (target + visual_words_per_beat - 1) // visual_words_per_beat))
        scene_word_budgets.append({
            "scene_id": index + 1,
            "target_spoken_words": target,
            "hard_minimum_words": max(8, round(target * 0.90)),
            "hard_maximum_words": max(10, round(target * 1.10)),
            "required_narration_beats": required_beats,
            "target_words_per_beat": max(8, round(target / required_beats)),
        })

    teaching_contract = duration_teaching_contract(
        target_duration_seconds, scene_count, word_budget
    )
    # The director emits substantially more than narration: each spoken beat
    # also carries its matching visual action, semantic contract and continuity
    # state. Scale for that complete JSON rather than narration alone. This is
    # only a ceiling, so short successful responses do not consume extra tokens.
    configured_director_tokens = max(
        512,
        int(os.environ.get("DIRECTOR_MAX_TOKENS", "20000")),
    )
    minimum_director_tokens = max(
        512,
        int(os.environ.get("DIRECTOR_MIN_TOKENS", "8000")),
    )
    tokens_per_narration_word = max(
        1.0,
        float(os.environ.get("DIRECTOR_TOKENS_PER_NARRATION_WORD", "12")),
    )
    completion_token_budget = min(
        configured_director_tokens,
        max(
            minimum_director_tokens,
            round(word_budget * tokens_per_narration_word),
        ),
    )

    payload = {
        "user_request": user_prompt,
        "audience": audience or "general learner",
        "target_duration_seconds": target_duration_seconds,
        "narration_word_budget": {
            "target": word_budget,
            "hard_required_range": [min_words, max_words],
            "counting_rule": "Count only words inside narration_beats[*].narration across every scene.",
        },
        "scene_word_budgets": scene_word_budgets,
        "duration_calibration": calibrated_budget,
        "desired_scene_count": scene_count,
        "duration_teaching_contract": teaching_contract,
        "completion_token_budget": completion_token_budget,
        "instructions": [
            "First infer an audience_profile from the complete free-form audience description. Do not classify it using a fixed audience-name table.",
            "Determine a strict scope for exactly what the user asked and what can be taught clearly within the supplied duration capacity.",
            (
                "If a SOURCE DOCUMENT is provided, anchor the narration to it: use its terminology, "
                "facts, and structure as primary source material. Quote short excerpts (<=500 tokens) "
                "verbatim in scene narration where they carry the key idea. Never invent facts that "
                "contradict the document."
                if source_text
                else ""
            ),
            "List only the irreducible must_cover ideas needed to answer the central question at this duration; do not turn must_cover into an encyclopedia index.",
            "List tempting adjacent concepts in out_of_scope when useful.",
            "Create an explanation_strategy containing the central question, core takeaway, reusable mental model, narrative thread, primary example, progression, and ideas excluded for capacity.",
            "Then create the full beginning-to-end narration across scenes. Each scene must advance that strategy and build on prior understanding.",
            "Write semantic narration_beats first. Mark indispensable explanation beats required=true and depth/examples required=false.",
            f"HARD LENGTH CONTRACT: return {min_words} to {max_words} spoken narration words in total, targeting {word_budget}. A much shorter answer is invalid.",
            "Follow every scene_word_budgets entry. Use the required number of narration beats and make their combined narration reach that scene's word range.",
            "Treat target_words_per_beat as an operational budget: each beat should be within one word of it, and the scene total remains authoritative. Do not independently write the full scene word target into every beat.",
            "Before returning JSON, count narration words scene by scene. If any scene or the total is below its hard minimum, deepen the existing explanation with mechanism, intuition, evidence, or an audience-appropriate example until it passes; never stop early with a concise summary.",
            "For longer scenes, use complete explanatory sentences: definition or claim, intuition or mechanism, and a concrete example or consequence.",
            "Keep spoken sentences mostly between 7 and 22 words. Split dense lists and long causal chains into separate beats with separate visuals.",
            "Define every specialized term in audience_profile.terms_to_define before relying on it; omit a term when the duration cannot support its mechanism.",
            "Use causal strength precisely: distinguish causes, contributes to, influences, and correlates with. Never claim that one factor alone creates or defines a broad outcome unless the source establishes that.",
            "A real-world person, place, system, event, or observed example is not a constructed example. Set constructed_example only for invented teaching inputs or hypothetical scenarios.",
            "CLOSED-WORLD FACTUALITY: use empirical quantities only when the exact quantity occurs in user_request or source_document. This includes percentages, measurements, dates, money, scaled magnitudes, and bare unitless numerals asserted as facts. target_duration_seconds and model-created exact anchors are never evidence. A bare unitless numeral may appear without source evidence as structural visual direction (cardinality, indexing, logic, mathematics, or code). Spoken enumeration is also structural only when every beat in a multi-beat scene uses its matching one-based index (beat 1 uses 1, beat 2 uses 2, and so on); copy each such marker into that beat's exact_anchors. If no evidence supports a statistic, explain the mechanism qualitatively instead.",
            "CONSTRUCTED EXAMPLES: when an algorithm, mathematical explanation, code path, hypothetical scenario, or worked demonstration needs sample values, declare them in semantic_contract.example_literals, set constructed_example=true, and explain their hypothetical purpose in example_context. Keep those same values consistent across scenes. Never use this exception for empirical statistics or real-world factual claims.",
            "When narration_beats are present, omit combined narration and visual_prompt fields; the local compiler creates them without another LLM call.",
            "For every beat, visual_action must show exactly what that beat says; never create an unpaired narration or visual beat.",
            "Place important literal labels, numbers, formulas, quotations, and code in semantic_contract.exact_anchors and copy them exactly into both narration and visual instructions.",
            "For a process or worked example, show concrete before and after states. Do not substitute a feature list or decorative icon row for visible evidence.",
            "The closing scene must resolve the central question and give a transferable takeaway, not merely list properties.",
            "Make all scene narration_beats together fit the calibrated spoken_target_seconds on the first TTS pass.",
            "Fit depth to duration without leaving the requested topic unfinished.",
            "Use more detail/examples for longer durations but stay inside the scope lock.",
            f"Aim for about {scene_count} scenes; a small deviation is allowed if teaching quality requires it.",
        ] + [i for i in [
            (
                "If a SOURCE DOCUMENT is provided below, anchor the narration to it: use its "
                "terminology, facts, and structure as the primary source. Quote short excerpts "
                "(at most ~500 tokens per scene) verbatim in scene narration where they carry the "
                "key idea. Never invent facts that contradict the document."
                if source_text
                else ""
            ),
        ] if i],
        "required_schema": {
            "title": "string",
            "learning_goal": "string",
            "audience_profile": {
                "inferred_context": "what this specific free-form description implies and what remains uncertain",
                "prior_knowledge": "likely starting knowledge stated without relying only on a label",
                "learning_goal": "what this audience should understand or be able to do",
                "language_and_tone": "appropriate vocabulary, density, tone, and example style",
                "terminology_policy": "which terms require definition and how jargon is introduced",
                "assumptions": ["explicit conservative assumption"],
                "terms_to_define": ["term that must be introduced before use"]
            },
            "explanation_strategy": {
                "central_question": "the exact question the video resolves",
                "core_takeaway": "one memorable complete answer",
                "mental_model": "a reusable relationship or intuition, not a slogan",
                "narrative_thread": "one coherent object, case, example, question, or decision carried through scenes",
                "primary_example": "a concrete example appropriate for this audience and duration",
                "progression": ["ordered knowledge step"],
                "excluded_for_capacity": ["detail intentionally omitted at this requested duration"]
            },
            "example_contract": {
                "constructed_example": "boolean",
                "example_context": "provenance of the reusable primary example",
                "example_literals": ["every model-created literal in the primary example"],
            },
            "scope_summary": "string",
            "must_cover": ["string"],
            "out_of_scope": ["string"],
            "target_duration_seconds": target_duration_seconds,
            "scenes": [
                {
                    "id": 1,
                    "teaching_beat": "what this scene teaches",
                    "teaching_role": "motivation, mental_model, mechanism, worked_example, application, misconception, synthesis, or a precise combination",
                    "learner_takeaway": "what the learner newly understands after this scene",
                    "builds_on": "the prior idea this scene depends on, empty only for the opening",
                    "semantic_contract": {
                        "claim": "the precise idea asserted by the narration",
                        "visual_evidence": "what the viewer must see to understand or verify that claim",
                        "before_state": "state before a change, when applicable",
                        "after_state": "state after a change, when applicable",
                        "required_entities": ["concrete object, condition, or visible state required as evidence"],
                        "state_facts": ["short atomic fact with subject, timing/role, relation, and literal value when applicable"],
                        "exact_anchors": ["literal label, value, formula, quotation, or code fragment that must remain identical"],
                        "constructed_example": "boolean; true only for hypothetical teaching/test values",
                        "example_context": "why these values are constructed rather than factual evidence",
                        "example_literals": ["exact reusable sample value used by narration and visuals"]
                    },
                    "narration_beats": [
                        {
                            "beat_id": "s1_core",
                            "required": True,
                            "priority": 5,
                            "narration": "spoken sentence(s) for this semantic unit",
                            "visual_action": "matching visual action for only this semantic unit",
                            "exact_anchors": ["only the literal anchors used by this beat"]
                        }
                    ],
                    "visual_state": {
                        "mode": "new or continue",
                        "group": "short stable sequence name",
                        "keep": ["recurring semantic object"]
                    },
                    "importance": 0.5
                }
            ],
        },
    }

    if source_text:
        # Constant-cost anchoring: the director sees the document ONCE (in the
        # single planning call), not per scene. Tokens scale with the plan, not
        # the document length.
        payload["source_document"] = str(source_text)[:40_000]

    use_hierarchical = _should_use_hierarchical_planning(
        target_duration_seconds, scene_count,
    )
    if use_hierarchical:
        try:
            plan = _hierarchical_director_plan(
                payload, scene_word_budgets, completion_token_budget, job_id
            )
        except Exception as hierarchical_error:
            # A monolithic retry repeats the exact failure mode long-form
            # planning was designed to avoid and can consume three large
            # completions. The hierarchical controller already has a compact
            # combined outline plus a global/batched outline fallback, with
            # provider/key failover inside every call.
            raise RuntimeError(
                "Long-form director could not complete its bounded strategy/scene "
                f"batches: {hierarchical_error}"
            ) from hierarchical_error
    else:
        plan = _call_json(
            DIRECTOR_SYSTEM_PROMPT,
            payload,
            max_tokens=completion_token_budget,
            stage="director_plan",
            job_id=job_id,
            response_validator=lambda result: _validate_initial_director_candidate(
                result, scene_word_budgets
            ),
        )
    validated = _validate_plan(plan, target_duration_seconds)
    validated = _propagate_global_example_contract(validated)
    validated = _ensure_duration_scene_capacity(validated, target_duration_seconds)
    validated["planning_token_budget"] = {
        "max_completion_tokens": completion_token_budget,
        "configured_provider": LLM_PROVIDER,
        "configured_model": DIRECTOR_MODEL,
        "tokens_per_narration_word": tokens_per_narration_word,
        "extra_llm_calls": 0,
        "scales_only_when_requested_narration_requires_it": (
            completion_token_budget > minimum_director_tokens
        ),
    }
    validated = normalize_explanation_plan(
        validated,
        audience=audience,
        target_seconds=target_duration_seconds,
        target_words=word_budget,
    )
    from narration_compiler import assert_duration_preflight, compile_duration_first_plan

    compiled = compile_duration_first_plan(validated, target_duration_seconds)
    compiled, pedagogical_review = _review_pedagogical_integrity(
        compiled, "\n".join(filter(None, (user_prompt, source_text))), job_id
    )
    if str(pedagogical_review.get("status", "")).startswith("repaired"):
        compiled = normalize_explanation_plan(
            compiled,
            audience=audience,
            target_seconds=target_duration_seconds,
            target_words=word_budget,
        )
        compiled = compile_duration_first_plan(compiled, target_duration_seconds)
    compiled["pedagogical_integrity_review"] = pedagogical_review
    repair_history: List[Dict[str, Any]] = []
    duration_scene_contracts = _duration_scene_word_contracts(
        word_budget, len(compiled.get("scenes", []))
    )
    max_repairs = max(
        0,
        min(3, int(os.environ.get("DIRECTOR_PREFLIGHT_REPAIR_ATTEMPTS", "2"))),
    )
    final_duration_budgets = [
        {
            **contract,
            "required_narration_beats": max(
                1, min(6, (
                    int(contract["target_words"])
                    + max(20, int(os.environ.get("VISUAL_WORDS_PER_BEAT", "32"))) - 1
                ) // max(20, int(os.environ.get("VISUAL_WORDS_PER_BEAT", "32")))),
            ),
        }
        for contract in duration_scene_contracts
    ]
    for repair_attempt in range(1, max_repairs + 1):
        diagnostics = dict(compiled.get("duration_preflight", {}))
        scene_contract_error = None
        try:
            _validate_director_scene_contract(compiled, final_duration_budgets)
        except ValueError as contract_error:
            scene_contract_error = str(contract_error)
            diagnostics["scene_contract_error"] = scene_contract_error
        if diagnostics.get("status") == "pass" and scene_contract_error is None:
            break
        try:
            repaired = _repair_duration_in_batches(
                compiled, target_duration_seconds, duration_scene_contracts,
                completion_token_budget, job_id, repair_attempt,
            )
            repaired = normalize_explanation_plan(
                repaired,
                audience=audience,
                target_seconds=target_duration_seconds,
                target_words=word_budget,
            )
            compiled = compile_duration_first_plan(repaired, target_duration_seconds)
            repair_history.append({
                "attempt": repair_attempt,
                "before": diagnostics,
                "after": dict(compiled.get("duration_preflight", {})),
                "llm_batch_calls": int(repaired.get("_duration_batch_repair_calls", 0)),
            })
        except Exception as repair_error:  # noqa: BLE001 - bounded safety path
            repair_history.append({
                "attempt": repair_attempt,
                "before": diagnostics,
                "error": str(repair_error),
            })

    # Duration repair may create enough paired semantic beats to safely split
    # a long plan that was initially too terse. Re-run the token-free capacity
    # guard before any TTS or visual work starts.
    scene_count_before_capacity = len(compiled.get("scenes", []))
    compiled = _ensure_duration_scene_capacity(compiled, target_duration_seconds)
    if len(compiled.get("scenes", [])) != scene_count_before_capacity:
        compiled = normalize_explanation_plan(
            compiled,
            audience=audience,
            target_seconds=target_duration_seconds,
            target_words=word_budget,
        )
        compiled = compile_duration_first_plan(compiled, target_duration_seconds)

    compiled["duration_contract_repair"] = {
        "attempted": bool(repair_history),
        "attempt_count": len(repair_history),
        "history": repair_history,
        "final_status": compiled.get("duration_preflight", {}).get("status"),
    }
    compiled["pedagogical_integrity_review"] = pedagogical_review
    # NarrationDurationError carries duration_preflight into the downloadable
    # failure evaluation, so retain the complete repair trail there as well.
    compiled.setdefault("duration_preflight", {})["automatic_repair"] = deepcopy(
        compiled["duration_contract_repair"]
    )
    compiled.setdefault("planning_token_budget", {})["extra_llm_calls"] = sum(
        int(item.get("llm_batch_calls", 0)) for item in repair_history
    ) + int(
        bool(pedagogical_review.get("violations"))
        and pedagogical_review.get("status") != "disabled"
    )
    # Do not spend fact-verifier or visual tokens on a script that is still
    # outside its spoken-duration contract. A valid duration plan is the input
    # contract for every later quality stage.
    assert_duration_preflight(compiled)
    _validate_director_scene_contract(compiled, final_duration_budgets)
    source_quantities = _authorized_quantities(user_prompt, source_text)
    authorized_quantities = list(source_quantities)
    factuality_before = _scene_factuality_violations(compiled, authorized_quantities)
    initial_factuality_violations = deepcopy(factuality_before)
    local_fallback_events: List[Dict[str, Any]] = []
    # Unsupported scale words are normally rhetorical depth ("millions of
    # items"), not facts whose exact magnitude is essential. Qualitize those
    # sentence fragments locally before paying for verifier + repair calls.
    # Numeric values, dates, percentages, measurements and money still go
    # through the configured independent verifier.
    qualitative_magnitude_violations = [
        violation for violation in factuality_before
        if violation.get("unsupported")
        and all(
            MAGNITUDE_WORD_RE.fullmatch(str(token).strip())
            for token in violation.get("unsupported", [])
        )
    ]
    if qualitative_magnitude_violations:
        local_result = _apply_deterministic_factuality_fallback(
            compiled, authorized_quantities,
            violations=qualitative_magnitude_violations,
        )
        local_fallback_events.append({
            "stage": "pre_verifier_qualitative_magnitude", **local_result,
        })
        factuality_before = _scene_factuality_violations(compiled, authorized_quantities)
    model_knowledge = _verify_model_knowledge_quantities(
        compiled, factuality_before, job_id
    )
    authorized_quantities = list(dict.fromkeys([
        *authorized_quantities, *model_knowledge.get("approved", [])
    ]))
    factuality_before = _scene_factuality_violations(compiled, authorized_quantities)
    local_fallback_enabled = os.environ.get(
        "DIRECTOR_FACTUALITY_LOCAL_FALLBACK", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}
    local_before_model = os.environ.get(
        "DIRECTOR_FACTUALITY_LOCAL_BEFORE_MODEL", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if factuality_before and local_fallback_enabled and local_before_model:
        local_result = _apply_deterministic_factuality_fallback(
            compiled, authorized_quantities
        )
        local_fallback_events.append({"stage": "before_model_repair", **local_result})
        factuality_before = local_result["remaining"]
    factuality_history: List[Dict[str, Any]] = []
    max_factuality_repairs = max(
        0, min(2, int(os.environ.get("DIRECTOR_FACTUALITY_REPAIR_ATTEMPTS", "1")))
    )
    for attempt in range(1, max_factuality_repairs + 1):
        if not factuality_before:
            break
        factuality_candidate_before = deepcopy(compiled)
        affected_scene_ids = sorted({
            int(item.get("scene_id")) for item in factuality_before
            if str(item.get("scene_id", "")).isdigit()
        })
        factuality_contracts = [
            {
                **contract,
                "required_narration_beats": max(
                    1,
                    len(next(
                        (
                            scene.get("narration_beats", [])
                            for scene in compiled.get("scenes", [])
                            if isinstance(scene, dict)
                            and int(scene.get("id", 0)) == int(contract["scene_id"])
                        ),
                        [],
                    )),
                ),
            }
            for contract in _duration_scene_word_contracts(
                word_budget, len(compiled.get("scenes", []))
            )
            if int(contract["scene_id"]) in affected_scene_ids
        ]
        repair_payload = {
            "repair_attempt": attempt,
            "authorized_source_text": {
                "user_request": user_prompt,
                "source_document": str(source_text)[:40_000] if source_text else "",
                "authorized_quantities": authorized_quantities,
            },
            "violations": factuality_before,
            "total_narration_word_contract": {
                "target_words": word_budget,
                "minimum_words": max(1, round(word_budget * 0.92)),
                "maximum_words": max(1, round(word_budget * 1.08)),
            },
            "scene_word_contracts": _duration_scene_word_contracts(
                word_budget, len(compiled.get("scenes", []))
            ),
            "scenes": [
                {
                    "id": scene.get("id"),
                    "teaching_beat": scene.get("teaching_beat"),
                    "teaching_role": scene.get("teaching_role"),
                    "learner_takeaway": scene.get("learner_takeaway"),
                    "semantic_contract": scene.get("semantic_contract", {}),
                    "narration_beats": scene.get("narration_beats", []),
                }
                for scene in compiled.get("scenes", [])
                if isinstance(scene, dict)
                and int(scene.get("id", 0)) in affected_scene_ids
            ],
        }
        try:
            expected_factuality_ids = [
                int(contract["scene_id"]) for contract in factuality_contracts
            ]

            def validate_factuality_repair(result: Dict[str, Any]) -> None:
                # This stage owns factual provenance, not duration.  Rejecting
                # an otherwise correct fact repair because it is a few words
                # long discards the useful candidate and repeats the same call.
                # Duration is reconciled immediately after the merge below.
                _validate_director_plan_response(result)
                actual_ids = [
                    int(scene.get("id", 0)) for scene in result.get("scenes", [])
                    if isinstance(scene, dict)
                ]
                if actual_ids != expected_factuality_ids:
                    raise ValueError(
                        f"Factuality repair scene IDs {actual_ids} did not match "
                        f"{expected_factuality_ids}."
                    )
                for scene in result.get("scenes", []):
                    beats = [
                        beat for beat in scene.get("narration_beats", [])
                        if isinstance(beat, dict)
                        and str(beat.get("narration", "")).strip()
                        and str(beat.get("visual_action", "")).strip()
                    ]
                    if not beats:
                        raise ValueError(
                            f"Factuality repair scene {scene.get('id')} returned no "
                            "paired narration/visual beats."
                        )

            repaired_json = _call_json(
                FACTUALITY_REPAIR_SYSTEM_PROMPT,
                repair_payload,
                max_tokens=min(
                    completion_token_budget,
                    max(1800, sum(int(item["target_words"]) for item in factuality_contracts) * 10),
                ),
                stage="director_factuality_repair",
                job_id=job_id,
                response_validator=validate_factuality_repair,
            )
            returned_by_id = {
                int(scene.get("id")): scene
                for scene in repaired_json.get("scenes", []) if isinstance(scene, dict)
            }
            combined_factuality_scenes = [
                returned_by_id.get(int(scene.get("id", index)), scene)
                for index, scene in enumerate(compiled.get("scenes", []), 1)
                if isinstance(scene, dict)
            ]
            repaired = _merge_preflight_repair(
                compiled, {"scenes": combined_factuality_scenes}, target_duration_seconds
            )
            _synchronize_semantic_contracts_after_factual_repair(
                repaired, authorized_quantities
            )
            repaired = normalize_explanation_plan(
                repaired,
                audience=audience,
                target_seconds=target_duration_seconds,
                target_words=word_budget,
            )
            compiled = compile_duration_first_plan(repaired, target_duration_seconds)
            factuality_after = _scene_factuality_violations(
                compiled, authorized_quantities
            )
            factuality_history.append({
                "attempt": attempt,
                "violations_before": factuality_before,
                "violations_after": factuality_after,
            })
            if job_id:
                from run_log import log_local_transform
                log_local_transform(
                    job_id, "factuality_sentence_repair", candidate_id=f"repair_{attempt}",
                    before=factuality_candidate_before, after=compiled,
                    details={
                        "violations_before": factuality_before,
                        "violations_after": factuality_after,
                        "model_knowledge_review": model_knowledge,
                    }, status="repaired",
                )
            factuality_before = factuality_after
        except Exception as factuality_error:  # noqa: BLE001 - bounded safety path
            factuality_history.append({
                "attempt": attempt,
                "violations_before": factuality_before,
                "error": str(factuality_error),
            })

    _synchronize_semantic_contracts_after_factual_repair(
        compiled, authorized_quantities
    )
    factuality_final = _scene_factuality_violations(
        compiled, authorized_quantities
    )
    if factuality_final and local_fallback_enabled:
        factuality_candidate_before = deepcopy(compiled)
        fallback_result = _apply_deterministic_factuality_fallback(
            compiled, authorized_quantities
        )
        local_fallback_events.append({"stage": "after_model_repair", **fallback_result})
        factuality_final = fallback_result["remaining"]
        if job_id:
            from run_log import log_local_transform
            log_local_transform(
                job_id, "factuality_local_sentence_fallback",
                candidate_id="deterministic_sentence_rewrite",
                before=factuality_candidate_before, after=compiled,
                details=fallback_result, status="repaired",
            )

    # Fact repair is allowed to rewrite a complete sentence, so it can move a
    # previously valid scene outside its duration range.  Reconcile the two
    # contracts to a fixed point instead of letting the final assertion reject
    # a script that an earlier stage itself changed.
    post_factuality_duration_history: List[Dict[str, Any]] = []
    for attempt in range(1, max_repairs + 1):
        duration_diagnostics = dict(compiled.get("duration_preflight", {}))
        scene_contract_error = None
        try:
            _validate_director_scene_contract(compiled, final_duration_budgets)
        except ValueError as error:
            scene_contract_error = str(error)
        if duration_diagnostics.get("status") == "pass" and scene_contract_error is None:
            break
        before_reconcile = deepcopy(compiled)
        try:
            repaired = _repair_duration_in_batches(
                compiled, target_duration_seconds, duration_scene_contracts,
                completion_token_budget, job_id, max_repairs + attempt,
            )
            repaired = normalize_explanation_plan(
                repaired, audience=audience,
                target_seconds=target_duration_seconds,
                target_words=word_budget,
            )
            compiled = compile_duration_first_plan(repaired, target_duration_seconds)
            _synchronize_semantic_contracts_after_factual_repair(
                compiled, authorized_quantities
            )
            cleanup = _apply_deterministic_factuality_fallback(
                compiled, authorized_quantities
            ) if local_fallback_enabled else {
                "applied": False, "replacement_count": 0,
                "remaining": _scene_factuality_violations(
                    compiled, authorized_quantities
                ),
            }
            if cleanup.get("applied"):
                local_fallback_events.append({
                    "stage": "after_duration_reconciliation", **cleanup,
                })
                compiled = compile_duration_first_plan(
                    normalize_explanation_plan(
                        compiled, audience=audience,
                        target_seconds=target_duration_seconds,
                        target_words=word_budget,
                    ),
                    target_duration_seconds,
                )
            post_factuality_duration_history.append({
                "attempt": attempt,
                "before": duration_diagnostics,
                "scene_contract_error": scene_contract_error,
                "after": dict(compiled.get("duration_preflight", {})),
                "factuality_cleanup": cleanup,
                "llm_batch_calls": int(repaired.get("_duration_batch_repair_calls", 0)),
            })
            if job_id:
                from run_log import log_local_transform
                log_local_transform(
                    job_id, "post_factuality_duration_reconciliation",
                    candidate_id=f"reconcile_{attempt}",
                    before=before_reconcile, after=compiled,
                    details=post_factuality_duration_history[-1], status="repaired",
                )
        except Exception as error:  # bounded; final hard gates remain authoritative
            post_factuality_duration_history.append({
                "attempt": attempt,
                "before": duration_diagnostics,
                "scene_contract_error": scene_contract_error,
                "error": str(error),
            })

    factuality_final = _scene_factuality_violations(
        compiled, authorized_quantities
    )
    compiled.setdefault("duration_contract_repair", {})[
        "post_factuality_history"
    ] = post_factuality_duration_history
    compiled.setdefault("duration_contract_repair", {})[
        "final_status"
    ] = compiled.get("duration_preflight", {}).get("status")
    local_fallback = {
        "enabled": local_fallback_enabled,
        "applied": any(event.get("applied") for event in local_fallback_events),
        "replacement_count": sum(
            int(event.get("replacement_count", 0)) for event in local_fallback_events
        ),
        "remaining": factuality_final,
        "extra_llm_calls": 0,
        "events": local_fallback_events,
    }
    compiled["factuality_contract"] = {
        "version": 2,
        "policy": "source_locked_empirical_quantities_with_structural_visual_literals",
        "source_authorized_quantities": source_quantities,
        "authorized_quantities": authorized_quantities,
        "model_knowledge_review": model_knowledge,
        "structural_sequence_quantities_by_scene": {
            str(scene.get("id")): sorted({
                value
                for field, values in _beat_aligned_sequence_literals(scene).items()
                if field.endswith(".narration")
                for value in values
            }, key=lambda value: int(value))
            for scene in compiled.get("scenes", [])
            if isinstance(scene, dict)
            and _beat_aligned_sequence_literals(scene)
        },
        "constructed_example_quantities_by_scene": {
            str(scene.get("id")): _constructed_example_quantities(
                scene.get("semantic_contract", {})
                if isinstance(scene.get("semantic_contract"), dict) else {}
            )
            for scene in compiled.get("scenes", []) if isinstance(scene, dict)
            and _constructed_example_quantities(
                scene.get("semantic_contract", {})
                if isinstance(scene.get("semantic_contract"), dict) else {}
            )
        },
        "initial_violation_count": len(initial_factuality_violations),
        "repair_attempt_count": len(factuality_history),
        "local_fallback": local_fallback,
        "final_violation_count": len(factuality_final),
        "final_status": "pass" if not factuality_final else "fail",
        "history": factuality_history,
    }
    for scene in compiled.get("scenes", []):
        if isinstance(scene, dict) and isinstance(scene.get("semantic_contract"), dict):
            contract = scene["semantic_contract"]
            examples = _constructed_example_quantities(contract)
            contract["authorized_quantities"] = list(dict.fromkeys(
                [*authorized_quantities, *[str(value) for value in examples if str(value).strip()]]
            ))
    compiled.setdefault("planning_token_budget", {})["extra_llm_calls"] += len(
        factuality_history
    ) + sum(
        int(item.get("llm_batch_calls", 0))
        for item in post_factuality_duration_history
    )
    if factuality_final:
        raise ValueError(
            "Factuality preflight rejected unsupported quantitative claims: "
            + json.dumps(factuality_final[:8], ensure_ascii=False)
        )

    # Quantitative repair and duration reconciliation can rewrite narration
    # after the first pedagogy pass. Re-run the bounded reviewer only when the
    # final text contains a known category conflict, then synchronize the
    # compiled narration and visual contracts one last time before rendering.
    final_conceptual_violations = [
        item for item in _pedagogical_quality_violations(compiled)
        if (
            "conceptual_category_conflict" in item.get("reasons", [])
            or "timing_claim_requires_integrity_review" in item.get("reasons", [])
        )
    ]
    if final_conceptual_violations:
        reviewed, final_pedagogical_review = _review_pedagogical_integrity(
            compiled, "\n".join(filter(None, (user_prompt, source_text))), job_id
        )
        # The reviewer is bounded and may fail during a provider incident or
        # reject a structurally harmless sentence. Keep the locally repaired,
        # beat-synchronized plan renderable and expose the remaining findings
        # in diagnostics instead of terminating the whole video job.
        compiled = compile_duration_first_plan(
            normalize_explanation_plan(
                reviewed, audience=audience,
                target_seconds=target_duration_seconds,
                target_words=word_budget,
            ),
            target_duration_seconds,
        )
        compiled["pedagogical_integrity_review"] = {
            "status": final_pedagogical_review.get("status"),
            "initial": pedagogical_review,
            "final": final_pedagogical_review,
        }
        compiled.setdefault("planning_token_budget", {})["extra_llm_calls"] = (
            int(compiled.get("planning_token_budget", {}).get("extra_llm_calls", 0)) + 1
        )
    compiled = _apply_visual_sequence_contract(compiled)
    assert_duration_preflight(compiled)
    return compiled

def _unwrap_revised_plan(data: Any) -> Dict[str, Any]:
    """
    Accept several common LLM response shapes.

    Valid examples:

    {"scenes": [...]}

    {"plan": {"scenes": [...]}}

    {"revised_plan": {"scenes": [...]}}

    {"result": {"scenes": [...]}}

    {"revised_scenes": [...]}

    The revision LLM is not trusted to follow the exact
    root-level schema every time.
    """

    if not isinstance(data, dict):
        return {}

    # Best case: already a complete-ish plan.
    if isinstance(data.get("scenes"), list):
        return data

    # Sometimes the model uses another key.
    if isinstance(data.get("revised_scenes"), list):
        copied = deepcopy(data)
        copied["scenes"] = copied["revised_scenes"]
        return copied

    # Common wrapper keys.
    wrapper_keys = [
        "revised_plan",
        "plan",
        "video_plan",
        "result",
        "output",
        "data",
        "revision",
    ]

    for key in wrapper_keys:
        value = data.get(key)

        if isinstance(value, dict):
            nested = _unwrap_revised_plan(value)

            if isinstance(nested.get("scenes"), list):
                return nested

    return {}


def _scene_id_value(
    scene: Dict[str, Any],
    fallback: int,
) -> int:
    """
    Safely resolve scene ID.
    """

    try:
        return int(scene.get("id", fallback))
    except (TypeError, ValueError):
        return fallback


def _merge_revision_with_original(
    original_plan: Dict[str, Any],
    revision_data: Dict[str, Any],
    target_duration_seconds: float,
) -> Dict[str, Any]:
    """
    IMPORTANT DESIGN RULE:

    The existing plan is authoritative.

    The duration revision may change:
      - narration
      - visual_prompt
      - teaching_beat

    It may NOT accidentally:
      - delete scenes
      - add unrelated scenes
      - change must_cover
      - change out_of_scope
      - change the learning goal
      - change topic scope

    Missing revised scenes automatically fall back to the
    corresponding original scene.
    """

    original = _validate_plan(
        deepcopy(original_plan),
        target_duration_seconds,
    )

    candidate = _unwrap_revised_plan(revision_data)

    candidate_scenes = candidate.get("scenes")

    if not isinstance(candidate_scenes, list):
        raise ValueError(
            "Revision response did not contain any usable scenes."
        )

    # Build lookup by scene ID.
    revised_by_id: Dict[int, Dict[str, Any]] = {}

    for position, scene in enumerate(
        candidate_scenes,
        start=1,
    ):
        if not isinstance(scene, dict):
            continue

        scene_id = _scene_id_value(
            scene,
            position,
        )

        revised_by_id[scene_id] = scene

    if not revised_by_id:
        raise ValueError(
            "Revision response contained no usable scene objects."
        )

    merged = deepcopy(original)

    merged_scenes: List[Dict[str, Any]] = []

    for position, original_scene in enumerate(
        original["scenes"],
        start=1,
    ):
        original_id = _scene_id_value(
            original_scene,
            position,
        )

        revised_scene = revised_by_id.get(
            original_id,
            {},
        )

        merged_scene = deepcopy(original_scene)

        # ---------------------------------------------
        # Narration
        # ---------------------------------------------

        revised_narration = str(
            revised_scene.get(
                "narration",
                "",
            )
        ).strip()

        if revised_narration:
            merged_scene["narration"] = (
                revised_narration
            )

        # ---------------------------------------------
        # Visual prompt
        #
        # Revision may update it when narration changed,
        # but omission should NOT delete the original.
        # ---------------------------------------------

        revised_visual = str(
            revised_scene.get(
                "visual_prompt",
                "",
            )
        ).strip()

        if revised_visual:
            merged_scene["visual_prompt"] = (
                revised_visual
            )

        # ---------------------------------------------
        # Teaching beat
        # ---------------------------------------------

        revised_beat = str(
            revised_scene.get(
                "teaching_beat",
                "",
            )
        ).strip()

        if revised_beat:
            merged_scene["teaching_beat"] = (
                revised_beat
            )

        # IDs stay locked to original sequence.
        merged_scene["id"] = position

        merged_scenes.append(
            merged_scene
        )

    merged["scenes"] = merged_scenes

    # =================================================
    # SCOPE LOCK
    #
    # These ALWAYS come from the valid original plan.
    # =================================================

    merged["title"] = original["title"]

    merged["learning_goal"] = (
        original["learning_goal"]
    )

    merged["scope_summary"] = (
        original["scope_summary"]
    )

    merged["must_cover"] = deepcopy(
        original["must_cover"]
    )

    merged["out_of_scope"] = deepcopy(
        original["out_of_scope"]
    )

    merged["target_duration_seconds"] = float(
        target_duration_seconds
    )

    return _validate_plan(
        merged,
        target_duration_seconds,
    )


def _narration_signature(
    plan: Dict[str, Any],
) -> str:
    """
    Used to detect whether a duration revision actually
    changed the narration.
    """

    scenes = plan.get("scenes", [])

    pieces: List[str] = []

    for scene in scenes:
        if not isinstance(scene, dict):
            continue

        pieces.append(
            str(
                scene.get(
                    "narration",
                    "",
                )
            ).strip()
        )

    return "\n".join(pieces).strip()

def revise_plan_for_duration(
    plan: Dict[str, Any],
    actual_duration_seconds: float,
    target_duration_seconds: float,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Duration-aware script revision.

    This function MUST NOT destroy an already valid plan
    merely because the revision LLM returned malformed or
    incomplete JSON.

    Strategy:
    1. Validate and preserve original plan.
    2. Ask LLM to revise narration.
    3. Unwrap common response formats.
    4. Merge revision ON TOP OF original scenes.
    5. Retry once with stricter instructions if needed.
    6. If both attempts fail, return original plan.
    """

    actual = max(
        0.1,
        float(actual_duration_seconds),
    )

    target = max(
        0.1,
        float(target_duration_seconds),
    )

    # =================================================
    # Original plan is our safety anchor.
    # =================================================

    original = _validate_plan(
        deepcopy(plan),
        target,
    )

    current_words = narration_word_count(
        original
    )

    ratio = target / actual

    revised_word_target = max(
        35,
        round(current_words * ratio),
    )

    direction = (
        "shorten"
        if actual > target
        else "lengthen"
    )

    delta = abs(
        actual - target
    )

    print(
        "\n========== DURATION REVISION =========="
    )
    print(
        "actual:",
        round(actual, 2),
        "target:",
        round(target, 2),
    )
    print(
        "direction:",
        direction,
    )
    print(
        "current words:",
        current_words,
    )
    print(
        "target words:",
        revised_word_target,
    )
    print(
        "=======================================\n"
    )

    base_payload = {
        "action": direction,

        "actual_duration_seconds": round(
            actual,
            3,
        ),

        "target_duration_seconds": round(
            target,
            3,
        ),

        "difference_seconds": round(
            delta,
            3,
        ),

        "current_narration_words": (
            current_words
        ),

        "new_narration_word_target": (
            revised_word_target
        ),

        "plan": deepcopy(original),

        "instructions": [
            (
                "Return the COMPLETE revised video plan "
                "as JSON."
            ),
            (
                "The root object MUST contain a scenes "
                "array."
            ),
            (
                "Keep EXACTLY the same scene count and "
                "scene IDs."
            ),
            (
                "Keep exactly the same topic, learning "
                "goal, must_cover and out_of_scope."
            ),
            (
                "Revise narration across the whole "
                "explanation rather than chopping the "
                "ending."
            ),
            (
                "Every scene MUST contain non-empty "
                "narration."
            ),
            (
                "Every scene MUST contain non-empty "
                "visual_prompt."
            ),
            (
                "Keep the conclusion complete."
            ),
            (
                "Do not add adjacent topics merely to "
                "fill time."
            ),
            (
                "If shortening, remove redundancy and "
                "compress examples."
            ),
            (
                "If lengthening, deepen existing ideas, "
                "examples and intuition only."
            ),
        ],

        "required_output_shape": {
            "title": original["title"],
            "learning_goal": (
                original["learning_goal"]
            ),
            "scope_summary": (
                original["scope_summary"]
            ),
            "must_cover": (
                original["must_cover"]
            ),
            "out_of_scope": (
                original["out_of_scope"]
            ),
            "target_duration_seconds": target,
            "scenes": [
                {
                    "id": 1,
                    "teaching_beat": "string",
                    "narration": (
                        "complete revised narration"
                    ),
                    "visual_prompt": (
                        "visual instruction"
                    ),
                }
            ],
        },
    }

    # =================================================
    # Try revision twice.
    # =================================================

    for attempt in range(1, 3):

        payload = deepcopy(
            base_payload
        )

        if attempt == 2:
            payload[
                "CRITICAL_RETRY_INSTRUCTION"
            ] = (
                "Your previous response could not be "
                "used. Return ONE JSON object whose ROOT "
                "contains a non-empty 'scenes' array. "
                "Do not wrap it in commentary. Do not "
                "omit scenes. Return every original "
                "scene ID."
            )

        try:
            print(
                f"Duration revision attempt "
                f"{attempt}/2"
            )

            response = _call_json(
                REVISION_SYSTEM_PROMPT,
                payload,
                max_tokens=12000,
                stage="director_revision",
                job_id=job_id,
            )

            print(
                "Revision root keys:",
                list(response.keys())
                if isinstance(response, dict)
                else type(response),
            )

            revised = (
                _merge_revision_with_original(
                    original,
                    response,
                    target,
                )
            )

            # -----------------------------------------
            # Check whether the LLM actually changed
            # anything.
            # -----------------------------------------

            before = _narration_signature(
                original
            )

            after = _narration_signature(
                revised
            )

            if before == after:
                print(
                    "Duration revision returned "
                    "unchanged narration."
                )

                # It is still structurally valid.
                # Return it and let app.py decide not
                # to waste another TTS pass.
                return revised

            print(
                "Duration revision succeeded."
            )

            return revised

        except Exception as error:
            print(
                f"Duration revision attempt "
                f"{attempt}/2 failed:"
            )
            print(
                repr(error)
            )

            continue

    # =================================================
    # ABSOLUTE SAFETY NET
    #
    # A duration revision failure should NEVER kill a
    # valid video job.
    # =================================================

    print(
        "\n========== DURATION REVISION FALLBACK =========="
    )
    print(
        "Both revision attempts failed."
    )
    print(
        "Keeping the last valid script and allowing "
        "audio tempo correction to handle the remaining "
        "duration difference."
    )
    print(
        "================================================\n"
    )

    return original
