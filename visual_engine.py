import os
import re
import json
import html
import math
import hashlib
import unicodedata
import threading
import copy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from freeform_repair import repair_freeform_plan
from storyboard_engine import build_storyboard_scene, has_storyboard
from llm_provider import (
    create_llm_client,
    fit_completion_token_budget,
    get_llm_config,
    missing_key_message,
)
from explanation_quality import assess_visual_anchor_coverage
from quantity_policy import (
    canonical_quantity,
    quantified_tokens,
    quantity_requires_source,
)
from visual_quality import (
    CandidateRegistry, canonicalize_visual_plan, semantic_fallback_plan,
    validate_chart_spec,
)


# =========================================================
# CONFIG
# =========================================================

PLANNER_LLM = get_llm_config("planner")
LLM_PROVIDER = PLANNER_LLM.provider
PLANNER_MODEL = PLANNER_LLM.model


def _requires_bounded_visual_contract(provider: str, model: str = "") -> bool:
    """Return whether concurrent visual calls need compact output discipline."""
    del model  # Provider capacity is stable across model-version renames.
    return str(provider or "").strip().casefold() == "groq"


def _safe_diagnostic_print(value: Any) -> None:
    """Diagnostics must never discard a valid model response.

    Windows services and redirected consoles can default to a legacy code page
    that cannot encode characters such as arrows or mathematical notation.
    Fall back to escaped text instead of letting ``print`` raise inside the
    visual planner's success path.
    """
    try:
        print(value)
    except UnicodeEncodeError:
        encoding = getattr(__import__("sys").stdout, "encoding", None) or "ascii"
        escaped = str(value).encode(encoding, errors="backslashreplace").decode(
            encoding, errors="replace"
        )
        print(escaped)


def materialize_static_visual_state(
    plan: Dict[str, Any], *, through_beat_id: Optional[str] = None
) -> Dict[str, Any]:
    """Flatten declared animation metadata into a representative final still.

    Static rendering otherwise freezes elements at their authored starting
    coordinates/opacity, which can leave tables empty or labels detached from
    objects that were meant to move.  This compiler applies only explicit
    final values already present in the plan; it does not animate or invent
    geometry.
    """
    result = copy.deepcopy(plan)
    elements = result.get("elements")
    animations = result.get("animations")
    if not isinstance(elements, list) or not isinstance(animations, list):
        return result
    by_id = {
        str(element.get("id")): element
        for element in elements if isinstance(element, dict) and element.get("id")
    }
    applied: List[Dict[str, Any]] = []

    def targets(animation: Dict[str, Any]) -> List[str]:
        values = animation.get("target_ids")
        if not isinstance(values, list):
            values = []
        single = animation.get("target_id") or animation.get("element_id")
        if single and str(single) not in {str(value) for value in values}:
            values = [*values, single]
        return [str(value) for value in values if str(value) in by_id]

    beat_states = result.get("beat_states")

    def apply_action(
        animation: Dict[str, Any], action: Dict[str, Any], *, final: bool = True
    ) -> None:
        action_type = str(
            action.get("action") or action.get("type") or animation.get("action")
            or animation.get("type") or ""
        ).strip().lower()
        action_targets = targets(action) or targets(animation)
        explicit = action.get("to") if isinstance(action.get("to"), dict) else None
        if explicit is None and isinstance(animation.get("to"), dict):
            explicit = animation.get("to")
        properties = action.get("properties") if isinstance(action.get("properties"), dict) else None
        if properties is None and isinstance(animation.get("properties"), dict):
            properties = animation.get("properties")
        property_name = action.get("property")
        property_value = action.get("value")
        if property_value is None and not isinstance(action.get("to"), dict):
            property_value = action.get("to") if final else action.get("from")
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        for target_id in action_targets:
            element = by_id[target_id]
            changed: Dict[str, Any] = {}
            for source in (explicit, properties):
                if not source:
                    continue
                for key, value in source.items():
                    if key in {"x", "y", "x1", "y1", "x2", "y2", "width", "height", "radius", "rx", "ry", "points", "opacity", "rotation", "fill", "stroke", "stroke_width"}:
                        element[key] = copy.deepcopy(value)
                        changed[key] = copy.deepcopy(value)
            if property_name in {"x", "y", "x1", "y1", "x2", "y2", "width", "height", "radius", "rx", "ry", "points", "opacity", "rotation", "fill", "stroke", "stroke_width"}:
                element[str(property_name)] = copy.deepcopy(property_value)
                changed[str(property_name)] = copy.deepcopy(property_value)
            if action_type in {"fade_in", "fadein", "show", "reveal"}:
                opacity = params.get("to_opacity", 1) if final else params.get("from_opacity", 0)
                element["opacity"] = opacity
                changed["opacity"] = opacity
            elif action_type in {"fade_out", "fadeout", "hide"}:
                element["opacity"] = 0
                changed["opacity"] = 0
            elif action_type == "fade" and explicit and "opacity" in explicit:
                element["opacity"] = explicit["opacity"]
                changed["opacity"] = explicit["opacity"]
            if changed:
                applied.append({"element_id": target_id, "values": changed})

    def order_key(animation: Dict[str, Any]) -> float:
        try:
            return float(animation.get("end_fraction", 1.0) or 1.0)
        except (TypeError, ValueError):
            return 1.0

    ordered = sorted(
        (animation for animation in animations if isinstance(animation, dict)),
        key=order_key,
    )
    beat_order: List[str] = []
    for animation in ordered:
        beat_id = str(animation.get("beat_id", "")).strip()
        if beat_id and beat_id not in beat_order:
            beat_order.append(beat_id)
    cutoff = beat_order.index(through_beat_id) if through_beat_id in beat_order else None
    for animation in ordered:
        beat_id = str(animation.get("beat_id", "")).strip()
        final = cutoff is None or not beat_id or beat_order.index(beat_id) <= cutoff
        nested = animation.get("actions")
        if isinstance(nested, list):
            for action in nested:
                if isinstance(action, dict):
                    apply_action(animation, action, final=final)
        else:
            apply_action(animation, animation, final=final)

    if isinstance(beat_states, list):
        states = [state for state in beat_states if isinstance(state, dict)]
        state_order = [str(state.get("beat_id", "")).strip() for state in states]
        state_cutoff = (
            state_order.index(through_beat_id)
            if through_beat_id in state_order else len(state_order) - 1
        )
        all_reveals = {
            str(target)
            for state in states
            for target in state.get("reveal_ids", [])
            if isinstance(state.get("reveal_ids"), list)
        }
        revealed = {
            str(target)
            for index, state in enumerate(states) if index <= state_cutoff
            for target in state.get("reveal_ids", [])
            if isinstance(state.get("reveal_ids"), list)
        }
        focus = set()
        if 0 <= state_cutoff < len(states):
            focus = {str(value) for value in states[state_cutoff].get("target_ids", [])}
        for target_id in all_reveals:
            if target_id in by_id:
                by_id[target_id]["opacity"] = 1 if target_id in revealed else 0
        for target_id in focus:
            if target_id in by_id:
                by_id[target_id]["__beat_focus"] = True
    result["_static_state"] = {
        "strategy": "declared_final_state",
        "applied_count": len(applied),
        "changes": applied,
    }
    return result


def compile_static_beat_states(
    plan: Dict[str, Any], beats: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Create cumulative stills from existing beat metadata, with zero LLM calls."""
    beat_ids = [
        str(beat.get("beat_id", "")).strip()
        for beat in beats if isinstance(beat, dict)
    ]
    declared = {
        str(item.get("beat_id", "")).strip()
        for key in ("animations", "beat_states")
        for item in plan.get(key, [])
        if isinstance(plan.get(key), list) and isinstance(item, dict)
    }
    if len(beat_ids) < 2 or not any(beat_id in declared for beat_id in beat_ids):
        return []
    return [
        materialize_static_visual_state(plan, through_beat_id=beat_id)
        for beat_id in beat_ids
    ]


def _resolve_planner_config():
    """Resolve a planner model without mutating process-wide environment."""
    from job_runtime import current_runtime

    runtime = current_runtime()
    requested_model = runtime.planner_model if runtime else PLANNER_LLM.model
    if requested_model and requested_model != PLANNER_LLM.model:
        try:
            from llm_provider import LLMConfig

            return LLMConfig(
                provider=PLANNER_LLM.provider,
                model=requested_model,
                api_key=PLANNER_LLM.api_key,
                base_url=PLANNER_LLM.base_url,
                default_headers=(
                    dict(PLANNER_LLM.default_headers)
                    if PLANNER_LLM.default_headers
                    else {}
                ),
            )
        except (TypeError, AttributeError):
            pass
    return PLANNER_LLM

# off | auto | always
# auto = only spend a second LLM call when the first plan has obvious layout/
# communication problems such as overflow, a tall text-heavy poster, etc.
PLAN_REVIEW_MODE = os.environ.get("PLAN_REVIEW_MODE", "off").strip().lower()
if PLAN_REVIEW_MODE not in {"off", "auto", "always"}:
    PLAN_REVIEW_MODE = "off"

VISUAL_JSON_LLM_REPAIR_ENABLED = os.environ.get(
    "VISUAL_JSON_LLM_REPAIR_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}

ICONIFY_BASE = "https://api.iconify.design"
ICONIFY_PREFIXES = os.environ.get(
    "ICONIFY_PREFIXES",
    "tabler,mdi,ph,lucide",
)

CACHE_DIR = Path(os.environ.get("ICON_CACHE_DIR", "icon_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# The visual coordinate system must match the encoded video.  Previously the
# planner and SVG renderer used a 1024x768 (4:3) canvas which was later fitted
# into a 1280x720 (16:9) video.  That permanently sacrificed 25% of the frame's
# horizontal area.  VIDEO_WIDTH/VIDEO_HEIGHT are now the single source of
# truth for free-form visuals, deterministic diagrams, rasterization and video.
CANVAS_WIDTH = max(320, int(os.environ.get("VIDEO_WIDTH", "1280")))
CANVAS_HEIGHT = max(180, int(os.environ.get("VIDEO_HEIGHT", "720")))
CANVAS_MARGIN = max(
    24,
    int(os.environ.get("CANVAS_MARGIN", str(round(CANVAS_HEIGHT * 0.0583)))),
)
ICONIFY_STRICT_FAMILY = os.environ.get(
    "ICONIFY_STRICT_FAMILY", "true"
).strip().lower() in {"1", "true", "yes", "on"}

# ---- style skins -----------------------------------------------------------
# Pick a palette without extra tokens: the prompt simply names the skin.
SKIN_COLORS = {
    # skin_name: (accent_shape_fill, text_color, stroke_color, bg_tint)
    "default": ("#e3f2fd", "#111111", "#222222", "#ffffff"),
    "ocean": ("#b3e0ff", "#0a2e4d", "#0d3b66", "#f2faff"),
    "forest": ("#cde8d0", "#14331a", "#1f4a28", "#f7fbf5"),
    "sunset": ("#ffe0b2", "#3e2723", "#795548", "#fffaf4"),
    "mono": ("#eeeeee", "#111111", "#333333", "#ffffff"),
}
def _resolve_skin():
    """Resolve the active skin from the isolated job context when present."""
    from job_runtime import current_runtime

    runtime = current_runtime()
    name = runtime.style if runtime else str(os.environ.get("STYLE_SKIN", "default")).strip().lower() or "default"
    # ``clean-diagrams`` is a visual direction, not a second colour palette.
    # Keep the established high-contrast default skin while preserving the
    # preset name in the immutable runtime for planner guidance and QA.
    if name == "clean-diagrams":
        name = "default"
    if name not in SKIN_COLORS:
        name = "default"
    return name, *SKIN_COLORS[name]

SKIN_NAME = _resolve_skin()[0]
SKIN_ACCENT, SKIN_TEXT, SKIN_STROKE, SKIN_BG = _resolve_skin()[1:]

MAX_ELEMENTS = 64

VISUAL_GENERATION_RETRIES = max(
    3,
    int(os.environ.get("VISUAL_GENERATION_RETRIES", "3"))
)

COMPACT_ELEMENT_LIMIT = max(
    8,
    min(32, int(os.environ.get("COMPACT_ELEMENT_LIMIT", "24")))
)

SEMANTIC_STORYBOARD_ENABLED = os.environ.get(
    "SEMANTIC_STORYBOARD_ENABLED", "1"
).strip().lower() in {"1", "true", "yes", "on"}

# hybrid (default): restore the expressive free-form element planner and use
# semantic storyboards only when the model explicitly chooses a structured
# diagram. semantic remains available for controlled experiments; elements is
# the exact pre-issue-3 behavior.
VISUAL_PLANNING_MODE = os.environ.get("VISUAL_PLANNING_MODE", "hybrid").strip().lower()
if VISUAL_PLANNING_MODE not in {"hybrid", "elements", "semantic"}:
    VISUAL_PLANNING_MODE = "hybrid"

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "text-to-visual-svg/2.0"})
_ICON_SEARCH_CACHE: Dict[Tuple[str, str], Optional[str]] = {}
_ICON_NEGATIVE_SEARCH_CACHE: set[Tuple[str, str]] = set()
_ICON_SEARCH_LOCK = threading.Lock()
_ICON_QUERY_INDEX = CACHE_DIR / "query_index.json"
try:
    _saved_icon_queries = json.loads(_ICON_QUERY_INDEX.read_text(encoding="utf-8"))
    if isinstance(_saved_icon_queries, dict):
        for _saved_key, _saved_value in _saved_icon_queries.items():
            if "|" in str(_saved_key) and isinstance(_saved_value, str):
                _saved_family, _saved_query = str(_saved_key).split("|", 1)
                _ICON_SEARCH_CACHE[(_saved_family, _saved_query)] = _saved_value
except (OSError, ValueError, TypeError):
    pass


def _remember_icon_search(cache_key: Tuple[str, str], icon_name: str) -> None:
    """Persist query resolution so later deployments avoid the search call."""
    with _ICON_SEARCH_LOCK:
        _ICON_SEARCH_CACHE[cache_key] = icon_name
        serializable = {
            f"{family}|{query}": value
            for (family, query), value in _ICON_SEARCH_CACHE.items()
            if isinstance(value, str) and value
        }
        temporary = _ICON_QUERY_INDEX.with_name(
            f"{_ICON_QUERY_INDEX.stem}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(json.dumps(serializable, sort_keys=True), encoding="utf-8")
            temporary.replace(_ICON_QUERY_INDEX)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

# The client is provider/key scoped. The model remains explicit per request.
client = None

def _planner_client():
    global client
    cfg = _resolve_planner_config()
    if (
        client is not None
        and str(client.base_url).rstrip("/") == str(cfg.base_url).rstrip("/")
        and client.api_key == cfg.api_key
    ):
        return client
    c = create_llm_client(cfg)
    if c is not None:
        client = c
    return c


# =========================================================
# GENERIC VISUAL DSL
# =========================================================

ALLOWED_ELEMENT_TYPES = {
    "text",
    "rect",
    "circle",
    "ellipse",
    "line",
    "arrow",
    "path",
    "polygon",
    "polyline",
    "icon",
}


# =========================================================
# NVIDIA PROMPTS
# =========================================================

RICH_VISUAL_PLANNER_PROMPT = """
You are a senior visual communication designer.

Convert ANY user content into ONE clear, expressive 2D visual composition. The
input may be physical, abstract, technical, educational, mathematical,
procedural, algorithmic, comparative, or narrative. Do not use hardcoded topic
templates. Design the scene that best communicates this particular content.

OUTPUT CANVAS
- Width: {CANVAS_WIDTH}; height: {CANVAS_HEIGHT}.
- Keep a safe margin of about {CANVAS_MARGIN}px and fit every element.
- Use the full 16:9 frame when useful; avoid tall poster layouts.

AVAILABLE ELEMENTS
- text, rect, circle, ellipse, line, arrow, path, polygon, polyline, icon
- Use icons for recognizable real objects when useful.
- Icon queries must be one or two concrete nouns naming the visible object;
  put emotion/action in pose, nearby shapes, or labels rather than the query.
- Use shapes, paths, arrows and short text for states, transformations,
  spatial relationships, mathematics, processes and technical explanations.
- Do not turn every object into a card or generic node. Preserve its visual
  form and arrange objects in a meaningful scene.

INFORMATION DESIGN
1. Preserve exact values, ordering, states, directions and relationships.
2. Prefer a visual explanation over prose or stacked text boxes.
3. Use short readable labels; most labels should be <= 28 characters.
4. Use arrows only for real direction, force, motion or transition.
5. Avoid overlaps, duplicated labels, crossed connectors and empty layouts.
6. Keep related elements grouped and make the main subject visually dominant.
   Every container must visibly contain or frame meaningful content; never
   create a large empty panel as a promise of content.
7. For physical/spatial explanations, compose an actual spatial illustration,
   not a generic flowchart.
8. Give every meaningful element a stable descriptive id. Continued scenes
   must reuse the requested semantic id prefixes.
9. Prefer 8-24 useful elements. Never add decoration merely to fill space.
10. The renderer adds the white background; do not add a canvas-sized rect.
11. The meaningful foreground should normally occupy 35-80% of the frame and
    should not be stranded in a corner. A contextual field/background does not
    count as foreground. Make the action readable at video scale.
12. Put true environmental backgrounds on layer 0. Keep main objects on layer
    2 and labels on layer 3 so local framing can distinguish context from idea.
13. Use a clear type hierarchy and high-contrast labels. Avoid pale text on a
    white background and avoid more than two competing accent colors.
14. Place each object label inside or immediately beside its object. Use a
    shared semantic id root for the object and label (for example item_box and
    item_label) so local validation can keep them attached.
15. Opaque callouts, bubbles and panels belong behind the icons or objects they
    annotate. Never paint a callout over the subject it refers to.
16. In a process or analogy, assign exactly one role to each visible stage.
    Do not show an artifact as the input and then repeat the same artifact as a
    later transformation stage. Read the labels from left to right and verify
    that every arrow forms a logically valid sentence.
17. Multiline text and code must fit inside its named container with padding.
    Prefer explicit line breaks, concise representative lines, and readable
    scale over showing a large unreadable block.
18. Use an icon only when its visible meaning is unambiguous. If a concept is
    better communicated by a labeled shape or concrete object, use that instead
   of an approximate icon whose ordinary meaning is different.
19. Empirical quantitative content is closed-world. Draw a percentage,
    measurement, date, money amount, scaled magnitude, or factual quantity only
    when INTERNAL SEMANTIC CONSTRAINTS explicitly authorizes it. Bare unitless
    literals may be used for structural visual meaning such as cardinality,
    indexing, logic, mathematics, or code. Never invent a statistic or scale
    value to make the scene look informative.
    A structural numeral present in exact literals is authoritative visible
    notation: render that numeral exactly. An icon may support its meaning but
    must never replace, rename, or conceal the required numeral.
20. INTERNAL SEMANTIC CONSTRAINTS assigns opaque references such as E1 and F1.
    Every required entity and state fact must be bound to at least one
    meaningful element using `semantic_refs`, for example
    {"id":"subject","type":"circle","semantic_refs":["E1","F1"],...}.
    References describe meaning; never display them as text. Attach a reference
    only when that element visibly supplies the corresponding evidence.
21. When an analogy and the main system must appear together, give them two
    clearly separated spatial regions and keep each label with its own region.
    Never overlay the analogy's objects on the main system's recurring objects.

COORDINATE CONVENTIONS
- rect: x/y top-left, width/height
- circle: x/y center, radius
- ellipse: x/y center, rx/ry
- line/arrow: x1/y1/x2/y2
- text: x/y baseline, text, optional font_size/font_weight/align
- icon: x/y center, width/height/query
- path: SVG d; polygon/polyline: SVG points
- common optional fields: id, layer, fill, stroke, stroke_width, opacity,
  rotation, dash, semantic_refs
- For line/arrow connectors, include optional from_id and to_id referencing
  the exact semantic ids of their endpoint objects. Local code then attaches
  the connector precisely without changing your composition.

LAYERS: 0 supporting geometry, 1 environment/connectors, 2 main objects,
3 labels/highlights. Use skin {SKIN_NAME}: accent {SKIN_ACCENT}, text
{SKIN_TEXT}, stroke {SKIN_STROKE}; keep strong contrast.

HYBRID STRUCTURED SHORTCUT
Only when the scene is genuinely best explained as an abstract flow, cycle,
comparison, timeline, hierarchy or concept map, you MAY return a compact
`storyboard` instead of `elements`. Never choose it for a physical scene,
spatial rule, geometry demonstration, object interaction or illustration.

Return valid JSON only. Default schema:
{{
  "canvas": {{"width": {CANVAS_WIDTH}, "height": {CANVAS_HEIGHT}}},
  "elements": [ ... ],
  "animations": [ ... optional ... ],
  "camera": {{ ... optional ... }}
}}
""".strip()


SEMANTIC_STORYBOARD_PROMPT = """
You are a senior visual communication designer.

Convert ANY user content into ONE clear 2D visual composition. The input may be
physical, abstract, technical, educational, mathematical, procedural,
algorithmic, comparative, or narrative.

Do NOT classify using hardcoded topic names. Describe visual meaning, not SVG
geometry. A deterministic local compositor will choose all coordinates, sizes,
typography, colors, layers and connector endpoints.

INFORMATION DESIGN RULES
1. Preserve the exact meaning of the input.
2. Preserve important literal values exactly: numbers, short labels, states,
   ordering, directions, and relationships.
3. If showing a sequence/transformation, every displayed state must be logically
   consistent with the previous state and the user's description.
4. Do not invent intermediate values just to fill space.
5. Show only the key states needed to understand the idea.
6. Prefer visual structure over prose. Do not turn the response into stacked
   paragraphs or a poster made of text boxes.
7. Use short labels. Most labels should be <= 28 characters.
8. A title is optional; use it only if it helps.
9. Use arrows for direction/transition, not long explanatory sentences.
10. Use stable semantic ids for recurring objects across continued scenes.
11. Choose the layout by communication structure, not by subject name.
12. For repeated values/items, draw each item as its own simple shape when that
    improves understanding.
13. Prefer 3-8 semantic items; maximum 10.
14. Never output x, y, width, height, font size, color, layer, SVG path, or
    individual drawing elements. Those are local compositor responsibilities.
15. Do NOT reduce the scene to a collection of cards. Preserve visual form:
    physical/recognizable objects should normally be icons, circular objects
    should be circles/rings, axes or scales can be bars, and standalone values
    can be pills. Use cards for genuinely abstract concepts or states.
16. When the content has visually depictable objects, use at least two useful
    non-card representations unless doing so would be semantically incorrect.

STABLE SEMANTIC IDS
- Give every meaningful item a short descriptive id such as repository,
  branch_main, or commit_node.
- Never use random ids or hashes.
- When the scene instruction says it CONTINUES a visual state, use the exact
  requested semantic object names as id prefixes. This lets the local renderer
  preserve recurring objects without another model call.

STORYBOARD LAYOUTS
- flow: ordered stages or transformations
- cycle: repeating loop
- comparison: two groups; give every item a shared group name
- timeline: events ordered in time
- hierarchy: parent/child structure
- grid: related items without directional dependency
- focus: one emphasized core item with surrounding ideas

ITEM FIELDS
- id: stable semantic identifier
- label: short visible label
- detail: optional short second line
- kind: concept|object|state|value|event|group
- representation: card|circle|ring|icon|label|pill|bar
  Choose this from visual form. Never use card as a universal default.
- size: small|medium|large; use large only for the main visual subject
- group: optional semantic grouping, required for comparison
- emphasis: true only for the one or two most important items
- icon: optional 1-3 word object query; omit for abstract ideas
- semantic_refs: opaque E/F references from INTERNAL SEMANTIC CONSTRAINTS that
  this item visibly represents; never show these references as labels

RELATION FIELDS
- from/to: item ids
- label: optional short connector label
- kind: arrow|association. Association is an undirected visual link.

Before returning JSON, silently check:
- factual/logical fidelity
- no accidental duplicated/lost values
- every relation references existing item ids
- minimal unnecessary text

Return VALID JSON only, exactly:
{{
  "storyboard": {{
    "title": "optional short title",
    "layout": "flow|cycle|comparison|timeline|hierarchy|grid|focus",
    "items": [
      {{"id": "stable_id", "label": "short label", "detail": "optional", "kind": "object", "representation": "icon", "icon": "short icon query", "size": "medium", "group": "optional", "emphasis": false}}
    ],
    "relations": [{{"from": "stable_id", "to": "another_id", "label": "optional", "kind": "arrow"}}]
  }}
}}
""".strip()

PLAN_REVIEW_PROMPT = f"""
You are a senior visual-design reviewer and semantic verifier.

You will receive:
1. the original user content
2. an initial generic visual JSON plan
3. a list of automatically detected quality issues

Return a REPAIRED JSON visual plan only.

Do not introduce topic-specific templates. Keep using the same generic element
vocabulary: {', '.join(sorted(ALLOWED_ELEMENT_TYPES))}.

REVIEW GOALS
- Preserve the original content exactly enough to teach/communicate it.
- Correct any wrong numbers, ordering, state transitions, relationships, or
  duplicated/lost values in the initial plan.
- Make the visual compact and understandable at a glance.
- Keep everything inside {CANVAS_WIDTH} x {CANVAS_HEIGHT}, with margins.
- If the plan is a tall text-heavy poster, redesign it as a compact diagram/grid.
- Prefer shapes/arrows/short labels instead of explanatory sentences.
- Do not invent decorative people/icons.
- Use icons only when a real/conceptual object benefits from an icon.
- For state changes, show a small number of meaningful states and ensure each
  state is logically correct.
- Avoid overlapping elements.
- Do NOT add a full-canvas background rectangle.
- The supplied semantic constraints are authoritative. Show every required
  entity and the visible evidence for the claim. Do not display an empirical
  quantity outside authorized_quantities, even if it seems generally true.
  Bare unitless structural literals are allowed, but must not be reframed as
  real-world measurements or statistics.

Return valid JSON only:
{{
  "canvas": {{"width": {CANVAS_WIDTH}, "height": {CANVAS_HEIGHT}}},
  "elements": [ ... ]
}}
""".strip()


# =========================================================
# NVIDIA JSON HELPERS
# =========================================================

def _extract_json_object(text: str) -> Optional[str]:
    if not text:
        return None

    stripped = text.strip()

    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        json.loads(stripped)
        return stripped
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(stripped)):
        ch = stripped[i]

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
                return stripped[start : i + 1]

    return None


def _parse_plan_content(content: str) -> Dict[str, Any]:
    candidate = _extract_json_object(content)
    if not candidate:
        raise ValueError("No JSON object found in visual-model output.")
    return json.loads(candidate)


def _call_nvidia(
    messages: List[Dict[str, str]],
    max_tokens: Optional[int] = None,
    stage: str = "visual_plan",
    job_id: Optional[str] = None,
):
    job_planner = _resolve_planner_config()
    if _planner_client() is None:
        raise RuntimeError(missing_key_message(job_planner))

    if max_tokens is None:
        max_tokens = int(
            os.environ.get(
                "VISUAL_MAX_TOKENS",
                "10000",
            )
        )

    safe_max_tokens = max(
        512,
        min(
            int(max_tokens),
            32768,
        ),
    )
    model_lower = (
        job_planner.model.lower()
    )
    kwargs = {
        "model": job_planner.model,
        "messages": messages,
        "temperature": 0.15,
        "max_tokens": safe_max_tokens,
        # Normal plans return in a few seconds. Fail a stalled request quickly
        # so the key pool can move scene 1 to another key.
        "timeout": max(
            3.0,
            float(os.environ.get("VISUAL_REQUEST_TIMEOUT_SECONDS", "12")),
        ),
        "stream": False,
        "_stage": stage,
        "_job_id": job_id,
    }

    kwargs["max_tokens"] = fit_completion_token_budget(
        job_planner,
        messages,
        safe_max_tokens,
    )


    thinking_status = "default"

    if job_planner.provider == "nvidia" and "deepseek-v4" in model_lower:
        kwargs["extra_body"] = {
            "reasoning_effort": "none",
            "chat_template_kwargs": {
                "thinking": False,
                "reasoning_effort": "none",
            },
        }
        thinking_status = "disabled"

    elif job_planner.provider in {"groq", "openrouter", "cerebras"}:
        kwargs["response_format"] = {"type": "json_object"}
        if model_lower.startswith("openai/gpt-oss-") or model_lower.startswith("gpt-oss-"):
            if job_planner.provider == "groq":
                kwargs["extra_body"] = {
                    "reasoning_effort": "low",
                    "include_reasoning": False,
                }
            elif job_planner.provider == "openrouter":
                kwargs["extra_body"] = {
                    "reasoning": {"effort": "low", "exclude": True},
                }
            else:
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
    print(
        "========== VISUAL MODEL =========="
    )

    print(
        "provider:",
        job_planner.provider,
    )

    print(
        "model:",
        job_planner.model,
    )

    print(
        "max_tokens:",
        kwargs["max_tokens"],
    )

    print(
        "thinking:",
        thinking_status,
    )

    print(
        "==================================\n"
    )

    from llm_provider import pooled_create
    return pooled_create(
        _planner_client(),
        job_planner,
        kwargs,
        # Exhaust every distinct healthy configured key before falling back to
        # a deterministic visual. A logical scene call never retries the same
        # failed key within this pass.
        max_key_failovers=-1,
    )

def _repair_invalid_json(
    content: str,
    first_error: Exception,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    repair_prompt = f"""
Rewrite the content below as ONE valid JSON object only.

Required top-level format:
{{
  "canvas": {{"width": {CANVAS_WIDTH}, "height": {CANVAS_HEIGHT}}},
  "elements": [ ... ]
}}

Allowed element types:
{', '.join(sorted(ALLOWED_ELEMENT_TYPES))}

Do not explain. Do not use markdown.
Do not change the intended visual meaning more than necessary to make valid JSON.

Invalid content:
{content[:14000]}
""".strip()

    repair_response = _call_nvidia(
        [
            {"role": "system", "content": "Return valid JSON only."},
            {"role": "user", "content": repair_prompt},
        ],
        max_tokens=12000,
        stage="visual_json_repair",
        job_id=job_id,
    )

    if not repair_response.choices:
        raise RuntimeError(
            f"Visual model returned invalid JSON and repair returned no choices. "
            f"First error: {first_error}"
        )

    repaired = repair_response.choices[0].message.content
    if not repaired:
        raise RuntimeError(
            f"Visual model returned invalid JSON and repair was empty. "
            f"First error: {first_error}"
        )

    print("\n========== VISUAL JSON REPAIR ==========")
    _safe_diagnostic_print(repaired)
    print("========================================\n")

    return _parse_plan_content(repaired)


# =========================================================
# RAW PLAN QUALITY CHECKS (GENERIC, NOT TOPIC-SPECIFIC)
# =========================================================

def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _raw_text_box(e: Dict[str, Any]) -> Tuple[float, float, float, float]:
    text = str(e.get("text", ""))
    font_size = max(8.0, _number(e.get("font_size"), 18.0))
    # Use the exact same line breaking model as the SVG renderer. Treating a
    # multiline code block as one giant line can shrink an otherwise valid
    # composition to a tiny fraction of the canvas.
    lines = _wrap_text(
        text,
        max_chars=max(8, int(_number(e.get("__max_chars"), 30))),
        max_lines=max(1, int(_number(e.get("__max_lines"), 12))),
    )
    width = max(font_size, max((len(line) for line in lines), default=1) * font_size * 0.54)
    line_height = font_size * 1.18
    height = font_size + max(0, len(lines) - 1) * line_height
    x = _number(e.get("x"), 0)
    y = _number(e.get("y"), 0)
    align = str(e.get("align", "left")).lower()

    if align == "center":
        left = x - width / 2
    elif align == "right":
        left = x - width
    else:
        left = x

    top = y - font_size
    return left, top, left + width, top + height + font_size * 0.15


def _parse_points_pairs(points: Any) -> List[Tuple[float, float]]:
    if not isinstance(points, str):
        return []
    nums = re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", points)
    if len(nums) < 2:
        return []
    values = [float(n) for n in nums]
    return list(zip(values[0::2], values[1::2]))


def _svg_path_sample_points(path_data: Any) -> List[Tuple[float, float]]:
    """Return geometry points from common SVG path commands.

    Treating every number pair as an x/y coordinate incorrectly interprets
    arc radii and flags as positions. One such arc expanded a valid 720px
    composition to 1080px and shrank the whole scene. This lightweight parser
    tracks endpoints and control points and samples elliptical arcs, which is
    sufficient for stable layout bounds without adding another dependency.
    """
    tokens = re.findall(
        r"[AaCcHhLlMmQqSsTtVvZz]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?",
        str(path_data or ""),
    )
    if not tokens:
        return []
    arity = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4,
             "Q": 4, "T": 2, "A": 7, "Z": 0}
    result: List[Tuple[float, float]] = []
    current = (0.0, 0.0)
    subpath = current
    command = ""
    index = 0

    def absolute_pair(x: float, y: float, relative: bool) -> Tuple[float, float]:
        return (current[0] + x, current[1] + y) if relative else (x, y)

    def sample_arc(start: Tuple[float, float], values: List[float], relative: bool) -> List[Tuple[float, float]]:
        rx, ry, rotation, large_arc, sweep, raw_x, raw_y = values
        end = absolute_pair(raw_x, raw_y, relative)
        rx, ry = abs(rx), abs(ry)
        if rx < 1e-6 or ry < 1e-6 or end == start:
            return [end]
        phi = math.radians(rotation % 360.0)
        cos_phi, sin_phi = math.cos(phi), math.sin(phi)
        dx, dy = (start[0] - end[0]) / 2.0, (start[1] - end[1]) / 2.0
        xp = cos_phi * dx + sin_phi * dy
        yp = -sin_phi * dx + cos_phi * dy
        radius_ratio = xp * xp / (rx * rx) + yp * yp / (ry * ry)
        if radius_ratio > 1.0:
            factor = math.sqrt(radius_ratio)
            rx, ry = rx * factor, ry * factor
        denominator = rx * rx * yp * yp + ry * ry * xp * xp
        numerator = max(0.0, rx * rx * ry * ry - denominator)
        coefficient = (-1.0 if bool(large_arc) == bool(sweep) else 1.0) * math.sqrt(
            numerator / max(1e-12, denominator)
        )
        cxp = coefficient * rx * yp / ry
        cyp = coefficient * -ry * xp / rx
        cx = cos_phi * cxp - sin_phi * cyp + (start[0] + end[0]) / 2.0
        cy = sin_phi * cxp + cos_phi * cyp + (start[1] + end[1]) / 2.0

        def angle(ux: float, uy: float, vx: float, vy: float) -> float:
            return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

        ux, uy = (xp - cxp) / rx, (yp - cyp) / ry
        vx, vy = (-xp - cxp) / rx, (-yp - cyp) / ry
        theta = math.atan2(uy, ux)
        delta = angle(ux, uy, vx, vy)
        if not bool(sweep) and delta > 0:
            delta -= 2 * math.pi
        elif bool(sweep) and delta < 0:
            delta += 2 * math.pi
        samples = []
        for step in range(1, 33):
            t = theta + delta * step / 32.0
            samples.append((
                cx + rx * cos_phi * math.cos(t) - ry * sin_phi * math.sin(t),
                cy + rx * sin_phi * math.cos(t) + ry * cos_phi * math.sin(t),
            ))
        return samples

    while index < len(tokens):
        if re.fullmatch(r"[A-Za-z]", tokens[index]):
            command = tokens[index]
            index += 1
            if command.upper() == "Z":
                current = subpath
                result.append(current)
                command = ""
                continue
        if not command:
            break
        upper = command.upper()
        count = arity.get(upper)
        if count is None or index + count > len(tokens):
            break
        values = [float(value) for value in tokens[index:index + count]]
        index += count
        relative = command.islower()
        start = current
        if upper in {"M", "L", "T"}:
            current = absolute_pair(values[0], values[1], relative)
            result.append(current)
            if upper == "M":
                subpath = current
                command = "l" if relative else "L"
        elif upper == "H":
            current = (current[0] + values[0], current[1]) if relative else (values[0], current[1])
            result.append(current)
        elif upper == "V":
            current = (current[0], current[1] + values[0]) if relative else (current[0], values[0])
            result.append(current)
        elif upper == "C":
            points = [absolute_pair(values[pos], values[pos + 1], relative) for pos in (0, 2, 4)]
            result.extend(points)
            current = points[-1]
        elif upper in {"S", "Q"}:
            points = [absolute_pair(values[pos], values[pos + 1], relative) for pos in (0, 2)]
            result.extend(points)
            current = points[-1]
        elif upper == "A":
            arc_points = sample_arc(start, values, relative)
            result.extend(arc_points)
            current = arc_points[-1]
    return result


def element_bounds_raw(e: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    kind = str(e.get("type", "")).lower()

    if kind == "rect":
        x = _number(e.get("x"))
        y = _number(e.get("y"))
        w = max(1.0, _number(e.get("width"), 1))
        h = max(1.0, _number(e.get("height"), 1))
        return x, y, x + w, y + h

    if kind == "circle":
        x = _number(e.get("x"))
        y = _number(e.get("y"))
        r = max(1.0, _number(e.get("radius"), 1))
        return x - r, y - r, x + r, y + r

    if kind == "ellipse":
        x = _number(e.get("x"))
        y = _number(e.get("y"))
        rx = max(1.0, _number(e.get("rx"), 1))
        ry = max(1.0, _number(e.get("ry"), 1))
        return x - rx, y - ry, x + rx, y + ry

    if kind in {"line", "arrow"}:
        x1 = _number(e.get("x1"))
        y1 = _number(e.get("y1"))
        x2 = _number(e.get("x2"))
        y2 = _number(e.get("y2"))
        stroke = max(0.0, _number(e.get("stroke_width"), 2))
        pad = stroke * 2 if kind == "arrow" else 0.5
        return min(x1, x2) - pad, min(y1, y2) - pad, max(x1, x2) + pad, max(y1, y2) + pad

    if kind == "text":
        return _raw_text_box(e)

    if kind == "icon":
        x = _number(e.get("x"))
        y = _number(e.get("y"))
        w = max(1.0, _number(e.get("width"), 1))
        h = max(1.0, _number(e.get("height"), 1))
        return x - w / 2, y - h / 2, x + w / 2, y + h / 2

    if kind in {"polygon", "polyline"}:
        pts = _parse_points_pairs(e.get("points"))
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return min(xs), min(ys), max(xs), max(ys)

    if kind == "path":
        points = _svg_path_sample_points(e.get("d"))
        if points:
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            return min(xs), min(ys), max(xs), max(ys)

    return None


def plan_bounds_raw(plan: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    elements = plan.get("elements")
    if not isinstance(elements, list):
        return None

    bounds = [element_bounds_raw(e) for e in elements if isinstance(e, dict)]
    bounds = [b for b in bounds if b is not None]
    if not bounds:
        return None

    return (
        min(b[0] for b in bounds),
        min(b[1] for b in bounds),
        max(b[2] for b in bounds),
        max(b[3] for b in bounds),
    )


def _background_element(element: Dict[str, Any]) -> bool:
    bounds = element_bounds_raw(element)
    if not bounds:
        return False
    area = max(0.0, bounds[2] - bounds[0]) * max(0.0, bounds[3] - bounds[1])
    element_id = re.sub(r"[^a-z0-9]+", "_", str(element.get("id", "")).lower())
    tokens = set(element_id.strip("_").split("_"))
    named = bool(tokens & {"bg", "background", "canvas", "field", "pitch"})
    return area >= CANVAS_WIDTH * CANVAS_HEIGHT * 0.55 and (
        int(_number(element.get("layer"), 2)) <= 0 or named
    )


def foreground_plan_bounds_raw(plan: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    """Bounds of meaningful foreground, excluding only clear large backgrounds."""
    elements = plan.get("elements")
    if not isinstance(elements, list):
        return None
    bounds = [
        element_bounds_raw(element) for element in elements
        if isinstance(element, dict) and not _background_element(element)
    ]
    bounds = [value for value in bounds if value is not None]
    if not bounds:
        return None
    return (
        min(value[0] for value in bounds), min(value[1] for value in bounds),
        max(value[2] for value in bounds), max(value[3] for value in bounds),
    )


def _canonical_visible_quantity(value: Any) -> str:
    return canonical_quantity(value)


def _visible_quantities(text: Any) -> List[str]:
    return quantified_tokens(text)


def _semantic_visual_quality_issues(
    plan: Dict[str, Any], constraints: Optional[Dict[str, Any]]
) -> List[str]:
    if not isinstance(constraints, dict):
        constraints = {}
    issues: List[str] = []
    coverage = assess_visual_anchor_coverage(
        plan, {"id": None, "semantic_contract": constraints}
    )
    missing_entities = coverage.get("missing_required_entities", [])
    missing_facts = coverage.get("missing_state_facts", [])
    if missing_entities:
        issues.append(
            "missing_required_entities:" + " | ".join(map(str, missing_entities[:6]))
        )
    if missing_facts:
        issues.append(
            "missing_visible_evidence:" + " | ".join(map(str, missing_facts[:4]))
        )
    if coverage.get("missing_anchors"):
        issues.append(
            "missing_exact_anchors:" + " | ".join(map(str, coverage["missing_anchors"][:6]))
        )

    authorized = {
        _canonical_visible_quantity(value)
        for value in constraints.get("authorized_quantities", [])
        if str(value).strip()
    }
    for field in ("state_facts", "exact_anchors", "example_literals"):
        for value in constraints.get(field, []) if isinstance(constraints.get(field), list) else []:
            authorized.update(_canonical_visible_quantity(token) for token in _visible_quantities(value))
    authorized.update(
        _canonical_visible_quantity(token)
        for token in _visible_quantities(constraints.get("claim", ""))
    )
    semantic_contract_text = " ".join(
        str(value)
        for key in (
            "claim", "visual_evidence", "before_state", "after_state",
            "required_entities", "state_facts", "exact_anchors",
        )
        for value in (
            constraints.get(key, [])
            if isinstance(constraints.get(key), list)
            else [constraints.get(key, "")]
        )
    )
    if re.search(r"\bzero\b", semantic_contract_text, flags=re.I):
        # “0” and “zero” express the same explicitly authorized state. This
        # prevents a faithful compact label from being rejected as an invented
        # quantity while empirical numbers remain source-locked.
        authorized.add("0")
    unsupported_tokens = set()
    for element in plan.get("elements", []):
        if not isinstance(element, dict) or str(element.get("type", "")).lower() != "text":
            continue
        element_text = str(element.get("text", ""))
        element_id = str(element.get("id", ""))
        structural_label = bool(
            re.fullmatch(r"\s*(?:step|stage|layer|scene|item)\s+\d+\s*", element_text, flags=re.I)
            or (
                re.fullmatch(r"\s*\d+\s*", element_text)
                and re.search(r"(?:index|step|stage|layer|scene|item)", element_id, flags=re.I)
            )
        )
        for token in _visible_quantities(element_text):
            if (
                quantity_requires_source(token, structural_context=structural_label)
                and _canonical_visible_quantity(token) not in authorized
            ):
                unsupported_tokens.add(token)
    unsupported = sorted(unsupported_tokens)
    if unsupported:
        issues.append("unsupported_visible_quantities:" + " | ".join(unsupported[:10]))

    meaningful = [
        element for element in plan.get("elements", [])
        if isinstance(element, dict) and not _background_element(element)
    ]
    non_text = [
        element for element in meaningful
        if str(element.get("type", "")).lower() != "text"
    ]
    visible_text_chars = sum(
        len(str(element.get("text", "")).strip())
        for element in meaningful
        if str(element.get("type", "")).lower() == "text"
    )
    # Information-dense code, paths, formulas and quoted literals can form a
    # strong visual with one container and a few text objects. Count their
    # readable information instead of demanding arbitrary extra shapes.
    semantic_units = len(non_text) + min(3, visible_text_chars // 18)
    if len(meaningful) < 3 or semantic_units < 3:
        issues.append(
            f"insufficient_semantic_foreground:{len(meaningful)}_elements_{len(non_text)}_visual_{semantic_units}_units"
        )

    storyboard_meta = plan.get("_storyboard")
    if (
        isinstance(storyboard_meta, dict)
        and bool(storyboard_meta.get("card_only"))
        and int(storyboard_meta.get("item_count", 0) or 0) >= 2
    ):
        issues.append(
            "generic_concept_cards:no_drawn_objects_or_relationship_evidence"
        )

    # A row of unexplained icons joined by arrows is renderable, but it is not
    # an explanation.  The Clean diagrams preset promises labelled line art,
    # and beginner visuals need enough plain-language scaffolding to decode
    # symbols without already knowing the subject.
    from job_runtime import current_runtime
    runtime = current_runtime()
    if runtime is not None:
        element_types = [
            str(element.get("type", "")).strip().lower()
            for element in meaningful
        ]
        connector_count = sum(
            bool(
                kind in {"arrow", "line"}
                or (
                kind in {"path", "polyline"}
                and (
                    element.get("from_id")
                    or element.get("to_id")
                    or bool(re.search(
                        r"(^|_)(arrow|connector|edge|link)(_|$)",
                        str(element.get("id", "")).casefold(),
                    ))
                )
            )
            )
            for element, kind in zip(meaningful, element_types)
        )
        symbol_count = sum(
            kind in {"icon", "circle", "ellipse", "polygon"}
            for kind in element_types
        )
        explanatory_labels = [
            str(element.get("text", "")).strip()
            for element in meaningful
            if str(element.get("type", "")).strip().lower() == "text"
            and re.search(r"[A-Za-z]{3}", str(element.get("text", "")))
        ]
        audience_is_beginner = any(
            marker in runtime.audience.casefold()
            for marker in ("beginner", "new to", "novice", "no prior", "first time")
        )
        clean_diagrams = runtime.style == "clean-diagrams"
        if clean_diagrams and symbol_count >= 3 and len(explanatory_labels) < 2:
            issues.append(
                "unexplained_symbol_sequence:clean_diagrams_requires_plain_language_labels"
            )
        if (clean_diagrams or audience_is_beginner) and connector_count >= 2 and len(explanatory_labels) < 3:
            issues.append(
                "connector_only_explanation:relationships_need_visible_beginner_labels"
            )
    return sorted(set(issues))


def _evaluate_pre_render_quality_gate(
    plan: Dict[str, Any], scene: Dict[str, Any]
) -> Dict[str, Any]:
    """Final local gate after continuity/layout repair and before SVG render."""
    constraints = scene.get("semantic_contract")
    constraints = constraints if isinstance(constraints, dict) else {}
    issues = _semantic_visual_quality_issues(plan, constraints)
    repair = plan.get("_freeform_repair")
    repair = repair if isinstance(repair, dict) else {}
    diagnostic_counts = {
        "unresolved_text_overlap_count": int(repair.get("unresolved_text_overlap_count", 0)),
        "unresolved_cross_type_collision_count": int(repair.get("unresolved_cross_type_collision_count", 0)),
        "foreground_occlusion_count": int(repair.get("foreground_occlusion_count", 0)),
        "unresolved_detached_label_count": int(repair.get("unresolved_detached_label_count", 0)),
        "empty_container_count": int(repair.get("empty_container_count", 0)),
        "low_density_container_count": int(repair.get("low_density_container_count", 0)),
    }
    for name, count in diagnostic_counts.items():
        if count > 0:
            issues.append(f"{name}:{count}")
    legibility = plan.get("_legibility") if isinstance(plan.get("_legibility"), dict) else {}
    below_minimum = list(legibility.get("text_below_minimum_ids", []) or [])
    if below_minimum:
        issues.append("text_below_minimum:" + " | ".join(map(str, below_minimum[:12])))
    continuity = plan.get("_continuity") if isinstance(plan.get("_continuity"), dict) else {}
    continuity_reason = str(continuity.get("reason", ""))
    if continuity_reason.startswith("continuity_rejected_") or continuity_reason == "continuity_base_unavailable":
        issues.append("continuity_incompatible:" + continuity_reason)
    bounds = plan_bounds_raw(plan)
    layout = plan.get("layout") if isinstance(plan.get("layout"), dict) else {}
    if bounds:
        scale = float(layout.get("scale", 1.0) or 1.0)
        tx = float(layout.get("translate_x", 0.0) or 0.0)
        ty = float(layout.get("translate_y", 0.0) or 0.0)
        x1, y1, x2, y2 = bounds
        transformed = (x1 * scale + tx, y1 * scale + ty, x2 * scale + tx, y2 * scale + ty)
        if (
            transformed[0] < -0.5 or transformed[1] < -0.5
            or transformed[2] > CANVAS_WIDTH + 0.5
            or transformed[3] > CANVAS_HEIGHT + 0.5
        ):
            issues.append("content_clipping:transformed_bounds_outside_canvas")
    issues = sorted(set(issues))
    blocking_prefixes = (
        "unsupported_visible_quantities:",
        "missing_required_entities:",
        "missing_visible_evidence:",
        "missing_exact_anchors:",
        "insufficient_semantic_foreground:",
        "unexplained_symbol_sequence:",
        "connector_only_explanation:",
        "generic_concept_cards:",
        "unresolved_text_overlap_count:",
        "unresolved_cross_type_collision_count:",
        "foreground_occlusion_count:",
        "unresolved_detached_label_count:",
        "empty_container_count:",
        "text_below_minimum:",
        # Clipping remains diagnostic because large environmental shapes often
        # cross a canvas edge deliberately (a horizon, floor, wave, or field).
        # Layout repair still attempts to fix it, but this coarse aggregate
        # bound must not discard an otherwise usable composition.
    )
    blocking = [issue for issue in issues if issue.startswith(blocking_prefixes)]
    prior = plan.get("_quality_gate") if isinstance(plan.get("_quality_gate"), dict) else {}
    return {
        **prior,
        "version": 1,
        "stage": "post_continuity_pre_render",
        "status": "pass" if not issues else ("fail" if blocking else "partial"),
        "issues": issues,
        "blocking_issues": blocking,
        "local_repair_diagnostics": diagnostic_counts,
    }


def evaluate_pre_render_quality_gate(
    plan: Dict[str, Any], scene: Dict[str, Any]
) -> Dict[str, Any]:
    """Run diagnostics without allowing observability code to stop rendering.

    A quality check may reject a candidate, but an unexpected diagnostic bug
    is recorded as a non-blocking partial result so a valid scene can still be
    rendered. Rendering failures remain visible at the scene recovery layer.
    """
    try:
        return _evaluate_pre_render_quality_gate(plan, scene)
    except Exception as exc:  # noqa: BLE001 - fail open only for diagnostics
        prior = plan.get("_quality_gate") if isinstance(plan.get("_quality_gate"), dict) else {}
        return {
            **prior,
            "version": 1,
            "stage": "post_continuity_pre_render",
            "status": "partial",
            "issues": [f"quality_diagnostic_error:{type(exc).__name__}"],
            "blocking_issues": [],
            "diagnostic_error": str(exc)[:1000],
        }


def raw_plan_quality_issues(
    plan: Dict[str, Any], semantic_constraints: Optional[Dict[str, Any]] = None
) -> List[str]:
    issues: List[str] = []
    elements = plan.get("elements")

    if not isinstance(elements, list) or not elements:
        return ["missing_or_empty_elements"]

    if len(elements) > MAX_ELEMENTS:
        issues.append("too_many_elements")

    text_elements = [
        e for e in elements
        if isinstance(e, dict) and str(e.get("type", "")).lower() == "text"
    ]
    total_text_chars = sum(len(str(e.get("text", ""))) for e in text_elements)
    long_texts = sum(len(str(e.get("text", ""))) > 34 for e in text_elements)

    if len(text_elements) >= 5 and total_text_chars > 180:
        issues.append("text_heavy")
    if long_texts >= 3:
        issues.append("too_many_long_labels")

    bounds = plan_bounds_raw(plan)
    if bounds:
        min_x, min_y, max_x, max_y = bounds
        content_w = max(1.0, max_x - min_x)
        content_h = max(1.0, max_y - min_y)

        if min_x < -10 or min_y < -10 or max_x > CANVAS_WIDTH + 10 or max_y > CANVAS_HEIGHT + 10:
            issues.append("content_outside_canvas")

        # Generic signal for the exact failure you observed: one narrow column
        # stretched far beyond the canvas. This does not know the topic.
        if content_h > CANVAS_HEIGHT * 0.92 and content_w < CANVAS_WIDTH * 0.48:
            issues.append("narrow_vertical_poster")

    # Too many rectangles with long text often means the model wrote a poster
    # instead of designing a visual explanation.
    rect_count = sum(
        1 for e in elements
        if isinstance(e, dict) and str(e.get("type", "")).lower() == "rect"
    )
    if rect_count >= 6 and len(text_elements) >= rect_count * 0.7 and total_text_chars > 140:
        issues.append("stacked_text_boxes")

    issues.extend(_semantic_visual_quality_issues(plan, semantic_constraints))
    return sorted(set(issues))


def review_visual_plan(
    original_text: str,
    initial_plan: Dict[str, Any],
    issues: List[str],
    job_id: Optional[str] = None,
    semantic_constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    review_input = {
        "original_content": original_text,
        "detected_quality_issues": issues,
        "initial_plan": initial_plan,
        "semantic_constraints": semantic_constraints or {},
    }

    response = _call_nvidia(
        [
            {"role": "system", "content": PLAN_REVIEW_PROMPT},
            {"role": "user", "content": json.dumps(review_input, ensure_ascii=False)},
        ],
        max_tokens=12000,
        stage="visual_quality_review",
        job_id=job_id,
    )

    if not response.choices:
        return initial_plan

    content = response.choices[0].message.content
    print("\n========== NVIDIA PLAN REVIEW ==========")
    print("issues:", issues)
    print("finish_reason:", response.choices[0].finish_reason)
    _safe_diagnostic_print(content)
    print("========================================\n")

    if not content:
        return initial_plan

    try:
        return _parse_plan_content(content)
    except Exception as exc:
        print("Plan review JSON parse failed; using initial plan:", exc)
        return initial_plan
def _compact_visual_system_prompt(
    element_limit: int = COMPACT_ELEMENT_LIMIT,
) -> str:
    return f"""
You are a visual communication designer.

Convert the user's scene description into ONE compact SVG visual plan.

CANVAS
- {CANVAS_WIDTH} x {CANVAS_HEIGHT}
- Keep everything safely inside the canvas.
- Use a compact composition.

ALLOWED TYPES
{', '.join(sorted(ALLOWED_ELEMENT_TYPES))}

HARD CONSTRAINTS
- Return JSON only.
- Maximum {element_limit} elements TOTAL.
- Prefer {"6-9" if element_limit <= 10 else "8-18"} elements.
- Maximum 6 text elements.
- Do NOT create long lists.
- Do NOT draw every item when a collection is large.
- For long arrays/lists, show only representative values and use "..." where needed.
- Do NOT create 15, 20, or 30 repeated rectangles.
- Do NOT create a text-heavy poster.
- Do NOT explain every edge case visually.
- Show ONLY what is needed to understand this particular scene.
- Prefer one strong diagram over many tiny objects.
- Draw recognizable objects, states, paths, or data marks. Never return a
  grid of cards that merely names the requested visuals.
- Make the main subject visually dominant and use roughly 35-80% of the
  frame for meaningful foreground. Avoid tiny subjects, large unused areas,
  and tall poster-like arrangements.
- For physical or spatial explanations, draw the actual spatial situation;
  do not translate it into a generic node-and-arrow flowchart.
- Every concrete-object entity reference E# must be attached to a non-text
  element that depicts it. When the required entity is explicitly textual
  (question text, label, title, caption, formula, value, or annotation), its
  E# belongs on that visible text element. Every state-fact reference F# must be attached to the
  arrow, path, changed object, or other mark that proves the relationship.
- Use labelled arrows or an explicit before/after state when the narration
  describes a change or causal relationship.
- Use short labels.
- Avoid text longer than 30 characters when possible.
- Avoid overlaps.
- If comparing an analogy with the main system, place them in separate visual
  regions. Never stack two diagrams on the same coordinates.
- Preserve important literal values from the user's scene.
- Never add a full-canvas background rectangle.

Return exactly:

{{
  "canvas": {{
    "width": {CANVAS_WIDTH},
    "height": {CANVAS_HEIGHT}
  }},
  "elements": [
    ...
  ]
}}
""".strip()


def _fallback_text_lines(
    text: str,
    max_chars: int = 48,
    max_lines: int = 5,
) -> List[str]:
    """
    Turn an arbitrary scene prompt into a few safe display lines.
    This does NOT call the LLM.
    """

    clean = re.sub(r"\s+", " ", str(text or "")).strip()

    if not clean:
        return ["Main idea"]

    words = clean.split()
    lines: List[str] = []
    current = ""

    for word in words:
        candidate = word if not current else f"{current} {word}"

        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            lines.append(current)

        current = word

        if len(lines) >= max_lines:
            break

    if current and len(lines) < max_lines:
        lines.append(current)

    if not lines:
        lines = ["Main idea"]

    # Indicate that the original prompt was longer.
    reconstructed = " ".join(lines)
    if len(reconstructed) < len(clean) and lines:
        last = lines[-1]
        if len(last) > max_chars - 3:
            last = last[: max_chars - 3]
        lines[-1] = last.rstrip(" .") + "..."

    return lines[:max_lines]


def create_fallback_visual_plan(text: str) -> Dict[str, Any]:
    """
    Deterministic emergency visual.

    This exists so that ONE broken LLM response can never crash
    an otherwise valid video-generation job.
    """

    # Backward-compatible public name. The old instruction-card
    # implementation below is unreachable by design; callers now receive the
    # same generalized semantic compiler used by the active candidate path.
    return semantic_fallback_plan(text, None)

    lines = _fallback_text_lines(text)

    # Proportional fallback geometry keeps the emergency scene usable for any
    # configured 16:9 output size instead of silently reverting to 1024x768.
    card_x = CANVAS_WIDTH * 0.14
    card_y = CANVAS_HEIGHT * 0.25
    card_w = CANVAS_WIDTH * 0.72
    card_h = CANVAS_HEIGHT * 0.52
    center_y = card_y + card_h * 0.50

    elements: List[Dict[str, Any]] = [
        {
            "type": "text",
            "id": "fallback_title",
            "layer": 3,
            "x": CANVAS_WIDTH / 2,
            "y": CANVAS_HEIGHT * 0.16,
            "text": "Main idea",
            "font_size": max(30, round(CANVAS_HEIGHT * 0.047)),
            "font_weight": "bold",
            "align": "center",
            "fill": "#111111",
        },
        {
            "type": "rect",
            "id": "fallback_card",
            "layer": 1,
            "x": card_x,
            "y": card_y,
            "width": card_w,
            "height": card_h,
            "rx": 24,
            "fill": "#f7f7f7",
            "stroke": "#222222",
            "stroke_width": 2,
        },
        {
            "type": "circle",
            "id": "fallback_dot",
            "layer": 2,
            "x": card_x + card_w * 0.13,
            "y": center_y,
            "radius": max(28, CANVAS_HEIGHT * 0.05),
            "fill": "#ffffff",
            "stroke": "#222222",
            "stroke_width": 3,
        },
        {
            "type": "arrow",
            "id": "fallback_arrow",
            "layer": 2,
            "x1": card_x + card_w * 0.19,
            "y1": center_y,
            "x2": card_x + card_w * 0.29,
            "y2": center_y,
            "stroke": "#222222",
            "stroke_width": 3,
        },
    ]

    start_y = card_y + card_h * 0.27

    for index, line in enumerate(lines):
        elements.append(
            {
                "type": "text",
                "id": f"fallback_line_{index}",
                "layer": 3,
                "x": card_x + card_w * 0.64,
                "y": start_y + index * (CANVAS_HEIGHT * 0.078),
                "text": line,
                "font_size": max(20, round(CANVAS_HEIGHT * 0.032)),
                "font_weight": "normal",
                "align": "center",
                "fill": "#111111",
            }
        )

    raw_plan = {
        "canvas": {
            "width": CANVAS_WIDTH,
            "height": CANVAS_HEIGHT,
        },
        "elements": elements,
    }

    return normalize_visual_plan(raw_plan)

def _doc_excerpts_block(doc_excerpts: Optional[str]) -> str:
    """Fold a short document excerpt into the planner prompt (constant cost)."""
    if doc_excerpts:
        excerpt = str(doc_excerpts).strip()
        if excerpt:
            return (
                "SOURCE DOCUMENT EXCERPT for this scene (use its exact terminology "
                "and notation in element labels and text):\n" + excerpt
            )
    return ""


def _semantic_constraints_block(constraints: Optional[Dict[str, Any]]) -> str:
    if not isinstance(constraints, dict) or not constraints:
        return ""
    constraint_reference_map: Dict[str, str] = {}
    if isinstance(constraints.get("required_entities"), list):
        constraint_reference_map.update({
            f"E{index + 1}": str(value)[:120]
            for index, value in enumerate(constraints.get("required_entities", [])[:10])
            if str(value).strip()
        })
    if isinstance(constraints.get("state_facts"), list):
        constraint_reference_map.update({
            f"F{index + 1}": str(value)[:180]
            for index, value in enumerate(constraints.get("state_facts", [])[:10])
            if str(value).strip()
        })
    safe = {
        "claim": str(constraints.get("claim", ""))[:600],
        "visual_evidence": str(constraints.get("visual_evidence", ""))[:600],
        "before_state": str(constraints.get("before_state", ""))[:300],
        "after_state": str(constraints.get("after_state", ""))[:300],
        "inherited_state": str(constraints.get("inherited_state", ""))[:300],
        "required_entities": [
            str(value)[:120] for value in constraints.get("required_entities", [])[:10]
            if str(value).strip()
        ] if isinstance(constraints.get("required_entities"), list) else [],
        "state_facts": [
            str(value)[:180] for value in constraints.get("state_facts", [])[:10]
            if str(value).strip()
        ] if isinstance(constraints.get("state_facts"), list) else [],
        "exact_literals": [
            str(value)[:160] for value in constraints.get("exact_anchors", [])[:20]
            if str(value).strip()
        ] if isinstance(constraints.get("exact_anchors"), list) else [],
        "authorized_quantities": [
            str(value)[:80] for value in constraints.get("authorized_quantities", [])[:30]
            if str(value).strip()
        ] if isinstance(constraints.get("authorized_quantities"), list) else [],
        "constructed_example": bool(constraints.get("constructed_example", False)),
        "example_context": str(constraints.get("example_context", ""))[:300],
        "example_literals": [
            str(value)[:80] for value in constraints.get("example_literals", [])[:40]
            if str(value).strip()
        ] if isinstance(constraints.get("example_literals"), list) else [],
        "visual_sequence": constraints.get("visual_sequence", {})
        if isinstance(constraints.get("visual_sequence"), dict) else {},
        "beat_visual_requirements": [
            {
                "beat_id": str(row.get("beat_id", ""))[:64],
                "visual_action": str(row.get("visual_action", ""))[:240],
            }
            for row in constraints.get("beat_visual_requirements", [])[:8]
            if isinstance(row, dict) and str(row.get("visual_action", "")).strip()
        ] if isinstance(constraints.get("beat_visual_requirements"), list) else [],
        "constraint_reference_map": constraint_reference_map,
    }
    return (
        "INTERNAL SEMANTIC CONSTRAINTS — NEVER DRAW THIS BLOCK OR AN ANCHOR/"
        "CONSTRAINT LEGEND. These values constrain correctness only. Show a literal "
        "only where it naturally belongs in the requested illustration; do not add "
        "footer text, pipe-separated lists, metadata, instructions, or placeholder "
        "containers. The claim must be supported by visible evidence. Preserve "
        "inherited state, show every required entity, and make every state fact "
        "unambiguous in the correct before/decision/after position. Never place an "
        "after-value in an input position or show a success branch whose stated "
        "condition is false. Treat authorized_quantities as a closed list for "
        "empirical quantities: no other percentage, measurement, date, money "
        "amount, or scaled magnitude may be visible. Bare unitless literals are "
        "permitted only for structural meaning such as cardinality, indexing, "
        "logic, mathematics, or code, never as invented statistics. Preserve "
        "every such numeral found in exact_literals as visible text; an icon "
        "cannot substitute for it. When "
        "visual_sequence.mode is connected, reuse its master "
        "composition, group and stable subjects, while emphasizing only this "
        "scene's focus_entities.\n"
        "For every entry in constraint_reference_map, attach its opaque key to "
        "the semantic_refs array of one or more elements that visibly prove it. "
        "Never draw the opaque keys.\n"
        + json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    )


def beat_visual_timeline_instruction(scene: Dict[str, Any]) -> str:
    """Compact scene-specific beat map; no extra LLM call or topic rules."""
    beats = [
        beat for beat in scene.get("narration_beats", [])
        if isinstance(beat, dict)
        and str(beat.get("narration", "")).strip()
        and str(beat.get("visual_action", "")).strip()
    ]
    if not beats:
        return ""
    timeline = []
    weights = [max(1, len(str(beat.get("narration", "")).split())) for beat in beats]
    total_weight = sum(weights)
    elapsed_weight = 0
    for index, beat in enumerate(beats):
        start_fraction = elapsed_weight / total_weight
        elapsed_weight += weights[index]
        timeline.append({
            "beat_id": str(beat.get("beat_id", f"beat_{index + 1}"))[:64],
            "start_fraction": round(start_fraction, 4),
            "end_fraction": round(elapsed_weight / total_weight, 4),
            "spoken_narration": str(beat.get("narration", ""))[:500],
            "visual_action": str(beat.get("visual_action", ""))[:500],
        })
    from animated_scene import fast_svg_animation_available
    static_only = not fast_svg_animation_available()
    output_rule = (
        "The runtime will render static beat states. Do not emit animations. "
        "Instead add a compact top-level beat_states array with exactly one row "
        "per beat: {beat_id,target_ids,reveal_ids}. target_ids names the visible "
        "elements emphasized for that beat; reveal_ids names only elements first "
        "shown at that beat. Reuse element IDs and keep each list short."
        if static_only else
        "When emitting animations, keep the same order and include beat_id on each animation."
    )
    return (
        "NARRATION-LOCKED VISUAL TIMELINE. Build one composition whose visual "
        "reveals/emphasis follow these ordered beat windows. Every beat must "
        "depict the meaning of spoken_narration with the corresponding "
        "visual_action. Keep facts and objects hidden until the beat that says "
        "them. Every beat must have a visible subject and completed visual state; do not show an "
        "unrelated static slide for the whole narration. " + output_rule + "\n"
        + json.dumps(timeline, ensure_ascii=False, separators=(",", ":"))
    )


def create_visual_plan(
    text: str,
    anim_prompt: Optional[str] = None,
    job_id: Optional[str] = None,
    doc_excerpts: Optional[str] = None,
    semantic_constraints: Optional[Dict[str, Any]] = None,
    scene_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Robust visual generation.

    Strategy:
    1. Try normal planner.
    2. If malformed, try JSON repair.
    3. If repair also fails, retry using a much smaller visual prompt.
    4. After all attempts fail, return deterministic fallback.

    IMPORTANT:
    This function should practically never raise due to bad LLM JSON.
    """

    original_text = str(text or "").strip()

    if not original_text:
        return semantic_fallback_plan("Main idea", semantic_constraints)

    failures: List[str] = []
    constraints_block = _semantic_constraints_block(semantic_constraints)
    candidate_registry = CandidateRegistry(
        semantic_constraints if isinstance(semantic_constraints, dict) else {}
    )
    from job_runtime import current_runtime
    runtime = current_runtime()
    quality_mode = runtime.quality_mode if runtime else "balanced"
    planner_config = _resolve_planner_config()
    # Groq capacity is accounted in tokens across concurrent scenes and model
    # version names change frequently.  A provider-level bounded contract is
    # safer than an exact model-name exception (which missed qwen3.8).
    constrained_output_model = _requires_bounded_visual_contract(
        planner_config.provider, planner_config.model
    )
    scene_element_limit = 10 if constrained_output_model else COMPACT_ELEMENT_LIMIT
    visual_direction = ""
    if runtime is not None:
        visual_direction = (
            "AUDIENCE AND VISUAL PRESET:\n"
            f"- Intended audience: {runtime.audience}. Make every visual readable at that knowledge level.\n"
        )
        if runtime.style == "clean-diagrams":
            visual_direction += (
                "- CLEAN DIAGRAMS means high-contrast, labelled explanatory line art—not a bare chain "
                "of symbols or icons.\n"
                "- Each scene must contain a short plain-language claim/title and visible labels for "
                "the important objects or states.\n"
                "- Icons may support meaning but must never replace labels or concrete visual evidence.\n"
                "- Caption important transitions beside their arrows; arrows alone must not carry the explanation.\n"
                "- Prefer a concrete object, worked state, or before/after mechanism over a generic node graph.\n"
                "- Use the structured diagram shortcut only when the scene is genuinely a flow, cycle, "
                "timeline, comparison, or hierarchy."
            )
        if any(
            marker in runtime.audience.casefold()
            for marker in ("beginner", "new to", "novice", "no prior", "first time")
        ):
            visual_direction += (
                "\n- BEGINNER MODE: introduce recognizable concrete objects, label unfamiliar symbols, "
                "and show what changes and why in plain language. Never assume the learner can infer "
                "meaning from notation plus arrows alone."
            )
    attempt_limit = VISUAL_GENERATION_RETRIES
    if quality_mode == "fast":
        attempt_limit = min(attempt_limit, 2)
    elif quality_mode == "rich":
        attempt_limit = max(attempt_limit, 3)

    semantic_required = VISUAL_PLANNING_MODE == "semantic"
    if semantic_required:
        hard_limits = """
ADDITIONAL HARD LIMITS FOR THIS VIDEO SCENE:
- Use no more than 10 semantic storyboard items.
- Prefer 3-8 items and no more than 12 relations.
- Keep labels short and do not output drawing coordinates or elements.
- This is ONE video scene, not an entire lesson poster.
""".strip()
    else:
        hard_limits = f"""
ADDITIONAL HARD LIMITS FOR THIS VIDEO SCENE:
- Use no more than {scene_element_limit} elements; prefer {"6-9" if constrained_output_model else "8-18"}.
- Maximum 6 text elements unless exact labels genuinely require more.
- Never expand large arrays/lists into dozens of repeated shapes.
- Preserve visual form; do not replace an illustration with generic cards.
- This is ONE video scene, not an entire lesson poster.
""".strip()

    for attempt in range(1, attempt_limit + 1):

        semantic_retry = attempt > 1 and any(
            "pre-render quality gate" in failure for failure in failures
        )
        # Every retry uses a bounded schema.  Re-sending the large prompt after
        # a semantic rejection consumed another organization's token window
        # and often failed at the same output boundary.
        compact_mode = attempt > 1

        if compact_mode and not semantic_required:
            system_prompt = _compact_visual_system_prompt(
                element_limit=scene_element_limit,
            )
            if visual_direction:
                system_prompt += "\n\n" + visual_direction
            if constraints_block:
                system_prompt += "\n\n" + constraints_block
            if anim_prompt:
                system_prompt += "\n\n" + str(anim_prompt).strip()
            if failures:
                system_prompt += (
                    "\n\nTHE PREVIOUS CANDIDATE FAILED. Correct these exact issues "
                    "while keeping the response compact:\n" + failures[-1][:1000]
                )
            doc_block = _doc_excerpts_block(doc_excerpts)
            user_prompt = f"""
Create a SMALL, SIMPLE visual for this scene:

{original_text}

{doc_block}
Do not explain everything.
Only visualize the main idea of THIS scene.
""".strip()

            max_tokens = int(os.environ.get(
                "VISUAL_COMPACT_MAX_TOKENS",
                "2200" if constrained_output_model else "3600",
            ))

        else:
            # Resolve the skin at render time: the prompt template was
            # expanded at module import with the default palette; swap in the
            # per-job skin here so the LLM paints with the chosen palette.
            skin_name, skin_accent, skin_text, skin_stroke, _skin_bg = (
                _resolve_skin()
            )
            prompt_template = (
                SEMANTIC_STORYBOARD_PROMPT
                if semantic_required
                else RICH_VISUAL_PLANNER_PROMPT
            )
            system_prompt = (
                prompt_template
                .replace("{CANVAS_WIDTH}", str(CANVAS_WIDTH))
                .replace("{CANVAS_HEIGHT}", str(CANVAS_HEIGHT))
                .replace("{CANVAS_MARGIN}", str(CANVAS_MARGIN))
                .replace("{SKIN_NAME}", skin_name)
                .replace("{SKIN_ACCENT}", skin_accent)
                .replace("{SKIN_TEXT}", skin_text)
                .replace("{SKIN_STROKE}", skin_stroke)
                + "\n\n"
                + hard_limits
            )
            if visual_direction:
                system_prompt = system_prompt + "\n\n" + visual_direction
            if anim_prompt:
                system_prompt = system_prompt + "\n\n" + str(anim_prompt).strip()
            if constraints_block:
                system_prompt = system_prompt + "\n\n" + constraints_block
            if semantic_retry and failures:
                system_prompt += (
                    "\n\nTHE PREVIOUS PLAN WAS REJECTED LOCALLY. Correct these "
                    "specific omissions in this new rich plan without adding "
                    "decorative content or unsupported facts:\n"
                    + failures[-1][:1200]
                )
            doc_block = _doc_excerpts_block(doc_excerpts)
            if compact_mode and semantic_required:
                user_prompt = (
                    "Create a small semantic storyboard for only the main idea "
                    "of this scene:\n\n" + original_text
                    + ("\n\n" + doc_block if doc_block else "")
                )
            else:
                user_prompt = original_text + ("\n\n" + doc_block if doc_block else "")
            max_tokens = int(os.environ.get(
                "VISUAL_CONSTRAINED_MAX_TOKENS", "2600"
            )) if constrained_output_model else int(
                os.environ.get("VISUAL_MAX_TOKENS", "4500")
            )

        try:
            print(
                f"\n========== VISUAL ATTEMPT {attempt}/"
                f"{attempt_limit} =========="
            )
            print("compact:", compact_mode)

            response = _call_nvidia(
                [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                max_tokens=max_tokens,
                stage="visual_plan" if not compact_mode else "visual_plan_compact",
                job_id=job_id,
            )

            if not response.choices:
                raise RuntimeError(
                    "Visual planner returned no choices."
                )

            choice = response.choices[0]
            content = choice.message.content or ""

            print("model:", _resolve_planner_config().model)
            print("finish_reason:", choice.finish_reason)
            _safe_diagnostic_print(content)
            print(
                "==========================================\n"
            )

            if not content.strip():
                raise RuntimeError(
                    "Visual planner returned empty content."
                )
            if str(choice.finish_reason or "").lower() in {"length", "max_tokens"}:
                # A token-capped JSON object is not a candidate for lossy
                # bracket repair: even if made parseable, its semantic tail is
                # missing. Move directly to the bounded compact retry.
                failures.append(
                    f"attempt {attempt}: planner output truncated at token limit"
                )
                continue

            # ---------------------------------------------
            # First try direct JSON parsing.
            # ---------------------------------------------
            try:
                parsed_plan = _parse_plan_content(content)
                raw_plan = canonicalize_visual_plan(parsed_plan)
                validate_chart_spec(raw_plan, semantic_constraints)
                if job_id:
                    from run_log import log_local_transform
                    log_local_transform(
                        job_id, "visual_schema_canonicalization", scene_id=scene_id,
                        candidate_id=f"planner_{attempt}_raw", before=parsed_plan,
                        after=raw_plan,
                        details=copy.deepcopy(raw_plan.get("_canonicalization", {})),
                    )

            except Exception as parse_error:

                print(
                    f"Visual attempt {attempt}: "
                    f"direct JSON parse failed:",
                    parse_error,
                )

                # -----------------------------------------
                # Then try ONE JSON repair.
                #
                # The crucial difference from your old
                # code: if repair fails, WE CATCH IT.
                # -----------------------------------------
                try:
                    if not VISUAL_JSON_LLM_REPAIR_ENABLED:
                        raise RuntimeError(
                            "LLM JSON repair disabled; use the bounded next planner attempt."
                        )
                    raw_plan = _repair_invalid_json(
                        content,
                        parse_error,
                        job_id=job_id,
                    )

                except Exception as repair_error:
                    message = (
                        f"attempt {attempt}: "
                        f"parse failed ({parse_error}); "
                        f"repair failed ({repair_error})"
                    )

                    failures.append(message)

                    print(
                        "Visual JSON repair also failed. "
                        "Moving to next attempt."
                    )
                    print(message)

                    # Do NOT crash.
                    continue

            # ---------------------------------------------
            # Basic structure verification before review.
            # ---------------------------------------------
            if not isinstance(raw_plan, dict):
                failures.append(
                    f"attempt {attempt}: plan was not an object"
                )
                continue

            # Hybrid issue 3: rich free-form plans are the quality-first
            # default. A semantic storyboard is compiled only when explicitly
            # returned for a genuinely structured scene.
            if has_storyboard(raw_plan):
                compiled_plan = build_storyboard_scene(raw_plan.get("storyboard"))
                if not compiled_plan:
                    failures.append(
                        f"attempt {attempt}: semantic storyboard was invalid"
                    )
                    continue
                raw_plan = compiled_plan
            elif semantic_required:
                if not has_storyboard(raw_plan):
                    failures.append(
                        f"attempt {attempt}: missing semantic storyboard"
                    )
                    continue

            elements = raw_plan.get("elements")

            if not isinstance(elements, list) or not elements:
                failures.append(
                    f"attempt {attempt}: missing elements array"
                )
                continue

            immutable_raw = copy.deepcopy(raw_plan)
            immutable_raw["_quality_gate"] = {
                "status": "fail", "blocking_issues": ["raw_candidate_not_validated"],
                "issues": ["raw_candidate_not_validated"],
            }
            candidate_registry.add(
                f"planner_{attempt}_raw", "immutable_raw_candidate", immutable_raw
            )

            # Never truncate a composition: array order is not semantic
            # priority, so slicing can silently discard required evidence.
            if len(elements) > MAX_ELEMENTS:
                message = (
                    f"attempt {attempt}: density_capacity_exceeded: "
                    f"{len(elements)} elements > {MAX_ELEMENTS}; request semantic "
                    "compression while preserving all required entities"
                )
                failures.append(message)
                print(message)
                continue

            # ---------------------------------------------
            # Review is useful, but should NEVER make a
            # successful generation fail.
            # ---------------------------------------------
            semantic_gate_issues: List[str] = []
            should_review = False
            try:
                issues = raw_plan_quality_issues(raw_plan, semantic_constraints)

                semantic_gate_issues = [
                    issue for issue in issues
                    if issue.startswith((
                        "missing_required_entities:",
                        "missing_visible_evidence:",
                        "missing_exact_anchors:",
                        "unsupported_visible_quantities:",
                        "insufficient_semantic_foreground:",
                        "unexplained_symbol_sequence:",
                        "connector_only_explanation:",
                        "generic_concept_cards:",
                    ))
                ]
                nonsemantic_review_issues = [
                    issue for issue in issues if issue not in semantic_gate_issues
                ]

                should_review = (
                    (quality_mode == "rich" and attempt == 1)
                    or
                    (PLAN_REVIEW_MODE == "always" and attempt == 1)
                    or (
                        PLAN_REVIEW_MODE == "auto"
                        and attempt == 1
                        and bool(nonsemantic_review_issues)
                    )
                )

                if should_review:
                    print(
                        "Visual quality issues:",
                        issues,
                    )

                    try:
                        reviewed_plan = review_visual_plan(
                            original_text,
                            raw_plan,
                            issues,
                            job_id=job_id,
                            semantic_constraints=semantic_constraints,
                        )

                        if (
                            isinstance(reviewed_plan, dict)
                            and isinstance(
                                reviewed_plan.get("elements"),
                                list,
                            )
                            and reviewed_plan["elements"]
                        ):
                            raw_plan = reviewed_plan

                    except Exception as review_error:
                        # Review is optional.
                        # Never discard an already valid plan.
                        print(
                            "Visual review failed; "
                            "keeping original plan:",
                            review_error,
                        )

            except Exception as quality_error:
                print(
                    "Visual quality check failed; "
                    "continuing with raw plan:",
                    quality_error,
                )

            # ---------------------------------------------
            # Finally normalize.
            # ---------------------------------------------
            try:
                if raw_plan.get("source") != "semantic_storyboard":
                    raw_plan = repair_freeform_plan(raw_plan)
                    immutable_repair = copy.deepcopy(raw_plan)
                    immutable_repair["_quality_gate"] = {
                        "status": "fail",
                        "blocking_issues": ["local_repair_candidate_not_validated"],
                        "issues": ["local_repair_candidate_not_validated"],
                    }
                    candidate_registry.add(
                        f"planner_{attempt}_local_repair", "immutable_local_repair",
                        immutable_repair,
                    )
                normalized = normalize_visual_plan(raw_plan)
                if isinstance(semantic_constraints, dict):
                    normalized["_semantic_constraints"] = copy.deepcopy(semantic_constraints)
                final_gate_issues = _semantic_visual_quality_issues(
                    normalized, semantic_constraints
                )
                blocking_gate_issues = [
                    issue for issue in final_gate_issues
                    if issue.startswith((
                        "unsupported_visible_quantities:",
                        "missing_required_entities:",
                        "missing_visible_evidence:",
                        "missing_exact_anchors:",
                        "insufficient_semantic_foreground:",
                        "unexplained_symbol_sequence:",
                        "connector_only_explanation:",
                        "generic_concept_cards:",
                    ))
                ]
                normalized["_quality_gate"] = {
                    "version": 1,
                    "stage": "pre_render",
                    "status": "pass" if not final_gate_issues else (
                        "fail" if blocking_gate_issues else "partial"
                    ),
                    "issues": final_gate_issues,
                    "blocking_issues": blocking_gate_issues,
                    "repair_attempted": bool(should_review),
                    "llm_review_attempted": bool(should_review),
                    "call_policy": {
                        "maximum_visual_planner_attempts": attempt_limit,
                        "automatic_llm_review_enabled": PLAN_REVIEW_MODE != "off",
                        "json_llm_repair_enabled": VISUAL_JSON_LLM_REPAIR_ENABLED,
                    },
                    "attempt": attempt,
                }
                candidate_registry.add(
                    f"planner_{attempt}", "canonicalized_planner", normalized
                )
                if job_id:
                    from run_log import log_local_transform
                    log_local_transform(
                        job_id, "visual_candidate_normalized", scene_id=scene_id,
                        candidate_id=f"planner_{attempt}", before=raw_plan,
                        after=normalized,
                        details={
                            "quality_gate": normalized.get("_quality_gate", {}),
                            "candidate_quality": candidate_registry.candidates[-1]["quality"],
                        }, status=(
                            "ok" if normalized["_quality_gate"]["status"] == "pass"
                            else "shadow"
                        ),
                    )
                if blocking_gate_issues and attempt < attempt_limit:
                    failures.append(
                        f"attempt {attempt}: pre-render quality gate: "
                        + "; ".join(blocking_gate_issues)
                    )
                    print("Pre-render quality gate requested a clean retry:", blocking_gate_issues)
                    continue

                if blocking_gate_issues:
                    failures.append(
                        f"attempt {attempt}: unresolved pre-render quality gate: "
                        + "; ".join(blocking_gate_issues)
                    )
                    break

                print(
                    f"Visual generation succeeded "
                    f"on attempt {attempt}."
                )

                return normalized

            except Exception as normalize_error:
                failures.append(
                    f"attempt {attempt}: normalization failed "
                    f"({normalize_error})"
                )

                print(
                    "Visual normalization failed:",
                    normalize_error,
                )

                continue

        except Exception as unexpected_error:
            failures.append(
                f"attempt {attempt}: {unexpected_error}"
            )

            print(
                f"Visual generation attempt "
                f"{attempt}/{attempt_limit} failed:"
            )
            print(unexpected_error)

            # Again: do NOT raise.
            continue

    # =====================================================
    # ABSOLUTE SAFETY NET
    # =====================================================

    print("\n========== VISUAL CANDIDATE RECOVERY ==========")
    print(
        "No candidate cleared every quality diagnostic. "
        "Selecting the best renderable rich candidate; the emergency plan "
        "is eligible only when no provider-created candidate can render."
    )

    for failure in failures:
        print(" -", failure)

    print("=====================================\n")

    fallback = semantic_fallback_plan(original_text, semantic_constraints)
    candidate_registry.add("semantic_fallback", "deterministic_fallback", fallback)
    best = candidate_registry.best()
    if best:
        selected = copy.deepcopy(best["plan"])
        selected["_candidate_selection"] = {
            "selected": best["candidate_id"], "quality": best["quality"],
            "candidate_count": len(candidate_registry.candidates),
            "failures": failures, "last_resort": True,
        }
        if job_id:
            from run_log import log_local_transform
            log_local_transform(
                job_id, "visual_candidate_selection", scene_id=scene_id,
                candidate_id=best["candidate_id"], after=selected,
                details={
                    "reason": "highest_scoring_renderable_hard_gate_preferred",
                    "candidates": [
                        {
                            "candidate_id": item["candidate_id"],
                            "stage": item["stage"], "quality": item["quality"],
                        }
                        for item in candidate_registry.candidates
                    ],
                    "failures": failures,
                }, status="selected",
            )
        return selected
    return fallback

# =========================================================
# PLAN SANITIZATION / NORMALIZATION
# =========================================================

def _clamp(value: Any, low: float, high: float, default: float) -> float:
    return max(low, min(high, _number(value, default)))


def _safe_color(value: Any, default: str) -> str:
    if not isinstance(value, str):
        return default
    value = value.strip()
    if re.fullmatch(
        r"(?:#[0-9a-fA-F]{3,8}|[a-zA-Z]{1,24}|rgba?\([0-9.,%\s]+\)|hsla?\([0-9.,%\s]+\))",
        value,
    ):
        return value
    return default


def _safe_path_d(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if re.fullmatch(r"[MmZzLlHhVvCcSsQqTtAa0-9eE+.,\-\s]+", value):
        return value
    return ""


def _safe_points(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if re.fullmatch(r"[0-9eE+.,\-\s]+", value):
        return value
    return ""


def _sanitize_elements(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_elements = plan.get("elements")
    if not isinstance(raw_elements, list):
        raise RuntimeError("Visual plan must contain an 'elements' array.")
    if len(raw_elements) > MAX_ELEMENTS:
        raise RuntimeError(
            f"Visual density exceeds capacity ({len(raw_elements)} > {MAX_ELEMENTS}); "
            "semantic compression or scene subdivision is required."
        )

    elements: List[Dict[str, Any]] = []

    for index, raw in enumerate(raw_elements):
        if not isinstance(raw, dict):
            continue

        kind = str(raw.get("type", "")).strip().lower()
        if kind not in ALLOWED_ELEMENT_TYPES:
            continue

        explicit_fill = raw.get("fill") is not None
        explicit_stroke = raw.get("stroke") is not None
        safe_fill = _safe_color(raw.get("fill"), _resolve_skin()[1])
        safe_stroke = _safe_color(raw.get("stroke"), _resolve_skin()[3])
        if explicit_fill and not explicit_stroke:
            safe_stroke = "none"
        e: Dict[str, Any] = {
            "type": kind,
            "id": str(raw.get("id", f"el_{index}"))[:80],
            "layer": int(_clamp(raw.get("layer"), 0, 10, 2)),
            "opacity": _clamp(raw.get("opacity"), 0, 1, 1),
            "rotation": _clamp(raw.get("rotation"), -360, 360, 0),
            "fill": safe_fill,
            "stroke": safe_stroke,
            "stroke_width": _clamp(
                raw.get("stroke_width"), 0, 20,
                0 if safe_stroke in {"none", "transparent"} else 2,
            ),
        }
        semantic_refs = raw.get("semantic_refs", [])
        if isinstance(semantic_refs, list):
            e["semantic_refs"] = list(dict.fromkeys(
                str(value).strip().upper() for value in semantic_refs[:20]
                if re.fullmatch(r"[EF]\d{1,2}", str(value).strip(), flags=re.I)
            ))
        for relationship in ("owner_id", "label_for", "from_id", "to_id"):
            reference = str(raw.get(relationship, "")).strip()
            if reference:
                e[relationship] = reference[:80]
        if bool(raw.get("minor_annotation", False)):
            e["minor_annotation"] = True
        if bool(raw.get("__beat_focus", False)):
            e["__beat_focus"] = True

        dash = str(raw.get("dash", "")).strip()
        e["dash"] = dash if re.fullmatch(r"[0-9.,\s-]*", dash) else ""

        # IMPORTANT: coordinates are NOT clamped to the canvas here. We preserve
        # the planner's geometry, then fit the whole composition uniformly.
        coord_min = -4096.0
        coord_max = 4096.0
        size_max = 4096.0

        if kind == "text":
            text = str(raw.get("text", "")).strip()
            if not text:
                continue
            e.update(
                {
                    "text": text[:600],
                    "x": _clamp(raw.get("x"), coord_min, coord_max, 100),
                    "y": _clamp(raw.get("y"), coord_min, coord_max, 100),
                    "font_size": _clamp(raw.get("font_size"), 8, 80, 18),
                    "font_weight": str(raw.get("font_weight", "normal"))[:20],
                    "align": str(raw.get("align", "left")).lower(),
                }
            )
            if e["align"] not in {"left", "center", "right"}:
                e["align"] = "left"
            e["fill"] = _safe_color(raw.get("fill"), _resolve_skin()[2])
            e["stroke"] = "none"
            if raw.get("__max_chars") is not None:
                e["__max_chars"] = int(_clamp(raw.get("__max_chars"), 8, 120, 30))
            if raw.get("__max_lines") is not None:
                e["__max_lines"] = int(_clamp(raw.get("__max_lines"), 1, 24, 3))

        elif kind == "rect":
            e.update(
                {
                    "x": _clamp(raw.get("x"), coord_min, coord_max, 100),
                    "y": _clamp(raw.get("y"), coord_min, coord_max, 100),
                    "width": _clamp(raw.get("width"), 1, size_max, 120),
                    "height": _clamp(raw.get("height"), 1, size_max, 70),
                    "rx": _clamp(raw.get("rx"), 0, 100, 8),
                }
            )

        elif kind == "circle":
            e.update(
                {
                    "x": _clamp(raw.get("x"), coord_min, coord_max, 100),
                    "y": _clamp(raw.get("y"), coord_min, coord_max, 100),
                    "radius": _clamp(raw.get("radius"), 1, 2048, 30),
                }
            )

        elif kind == "ellipse":
            e.update(
                {
                    "x": _clamp(raw.get("x"), coord_min, coord_max, 100),
                    "y": _clamp(raw.get("y"), coord_min, coord_max, 100),
                    "rx": _clamp(raw.get("rx"), 1, 2048, 50),
                    "ry": _clamp(raw.get("ry"), 1, 2048, 30),
                }
            )

        elif kind in {"line", "arrow"}:
            e.update(
                {
                    "x1": _clamp(raw.get("x1"), coord_min, coord_max, 100),
                    "y1": _clamp(raw.get("y1"), coord_min, coord_max, 100),
                    "x2": _clamp(raw.get("x2"), coord_min, coord_max, 200),
                    "y2": _clamp(raw.get("y2"), coord_min, coord_max, 100),
                    "fill": "none",
                }
            )
            for reference_key in ("from_id", "to_id"):
                reference = str(raw.get(reference_key, "")).strip()
                if reference:
                    e[reference_key] = reference[:80]

        elif kind == "path":
            d = _safe_path_d(raw.get("d"))
            if not d:
                continue
            e["d"] = d

        elif kind in {"polygon", "polyline"}:
            points = _safe_points(raw.get("points"))
            if not points:
                continue
            e["points"] = points
            if kind == "polyline" and "fill" not in raw:
                e["fill"] = "none"

        elif kind == "icon":
            query = str(raw.get("query", "")).strip()
            if not query:
                continue
            e.update(
                {
                    "query": query[:80],
                    "x": _clamp(raw.get("x"), coord_min, coord_max, CANVAS_WIDTH / 2),
                    "y": _clamp(raw.get("y"), coord_min, coord_max, CANVAS_HEIGHT / 2),
                    "width": _clamp(raw.get("width"), 10, size_max, 120),
                    "height": _clamp(raw.get("height"), 10, size_max, 120),
                }
            )

        elements.append(e)

    if not elements:
        raise RuntimeError("Visual plan contained no usable elements.")

    elements.sort(key=lambda item: int(item.get("layer", 2)))
    return elements


def _bounds_for_sanitized_element(e: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    return element_bounds_raw(e)


def _compute_layout_transform(elements: List[Dict[str, Any]]) -> Dict[str, float]:
    # A true canvas/background field establishes the planner's coordinate
    # system. Including its full 1280x720 bounds in a margin-fit calculation
    # shrinks the entire composition (and every font) even though nothing is
    # outside the canvas. Preserve authored canvas coordinates in that case;
    # the clipping gate still catches any foreground that actually escapes.
    if any(_background_element(element) for element in elements):
        return {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0}
    bounds = [_bounds_for_sanitized_element(e) for e in elements]
    bounds = [b for b in bounds if b is not None]

    if not bounds:
        return {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0}

    min_x = min(b[0] for b in bounds)
    min_y = min(b[1] for b in bounds)
    max_x = max(b[2] for b in bounds)
    max_y = max(b[3] for b in bounds)

    content_w = max(1.0, max_x - min_x)
    content_h = max(1.0, max_y - min_y)

    available_w = CANVAS_WIDTH - 2 * CANVAS_MARGIN
    available_h = CANVAS_HEIGHT - 2 * CANVAS_MARGIN

    scale = min(1.0, available_w / content_w, available_h / content_h)

    # Also scale up very tiny plans a little, but never beyond 1.18 so icons/text
    # do not become comically large.
    if content_w < available_w * 0.45 and content_h < available_h * 0.45:
        scale = min(1.18, available_w / content_w, available_h / content_h)

    scaled_w = content_w * scale
    scaled_h = content_h * scale

    target_left = (CANVAS_WIDTH - scaled_w) / 2
    target_top = (CANVAS_HEIGHT - scaled_h) / 2

    translate_x = target_left - min_x * scale
    translate_y = target_top - min_y * scale

    return {
        "scale": float(scale),
        "translate_x": float(translate_x),
        "translate_y": float(translate_y),
        "source_min_x": float(min_x),
        "source_min_y": float(min_y),
        "source_max_x": float(max_x),
        "source_max_y": float(max_y),
    }


def _foreground_autoframe(
    elements: List[Dict[str, Any]], layout: Dict[str, float]
) -> Tuple[Dict[str, float], Dict[str, Any]]:
    foreground = foreground_plan_bounds_raw({"elements": elements})
    background_count = sum(_background_element(element) for element in elements)
    diagnostic: Dict[str, Any] = {
        "background_element_count": int(background_count),
        "auto_framed": False,
        "scale_boost": 1.0,
    }
    if not foreground:
        diagnostic["reason"] = "foreground_bounds_unavailable"
        return layout, diagnostic

    x1, y1, x2, y2 = foreground
    width, height = max(1.0, x2 - x1), max(1.0, y2 - y1)
    base_scale = float(layout.get("scale", 1.0))
    horizontal_ratio = width * base_scale / CANVAS_WIDTH
    vertical_ratio = height * base_scale / CANVAS_HEIGHT
    area_ratio = horizontal_ratio * vertical_ratio
    diagnostic.update({
        "bounds_raw": {"x": x1, "y": y1, "width": width, "height": height},
        "area_ratio_before_autoframe": round(area_ratio, 6),
        "horizontal_span_ratio_before_autoframe": round(horizontal_ratio, 6),
        "vertical_span_ratio_before_autoframe": round(vertical_ratio, 6),
    })

    enabled = os.environ.get("FOREGROUND_AUTOFRAME_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
    # Video-scale readability depends on occupied area, not merely touching
    # distant points. A wide but shallow/scattered plan can still be visibly
    # tiny, so allow safe enlargement up to a 30% foreground-area threshold.
    sparse = area_ratio < 0.30 and max(horizontal_ratio, vertical_ratio) < 0.82
    if not enabled or not background_count or not sparse:
        diagnostic["reason"] = "not_sparse_or_no_background"
        return layout, diagnostic

    maximum_boost = max(1.0, min(1.5, float(os.environ.get("FOREGROUND_MAX_SCALE_BOOST", "1.35"))))
    desired_boost = min(maximum_boost, max(1.0, math.sqrt(0.38 / max(0.01, area_ratio))))
    new_scale = min(
        base_scale * desired_boost,
        (CANVAS_WIDTH - 2 * CANVAS_MARGIN) / width,
        (CANVAS_HEIGHT - 2 * CANVAS_MARGIN) / height,
    )
    if new_scale <= base_scale * 1.03:
        diagnostic["reason"] = "insufficient_safe_scale_gain"
        return layout, diagnostic

    center_x, center_y = (x1 + x2) / 2, (y1 + y2) / 2
    framed = dict(layout)
    framed.update({
        "scale": float(new_scale),
        "translate_x": float(CANVAS_WIDTH / 2 - center_x * new_scale),
        "translate_y": float(CANVAS_HEIGHT / 2 - center_y * new_scale),
        "auto_framed": True,
    })
    diagnostic.update({
        "auto_framed": True,
        "scale_boost": round(new_scale / base_scale, 6),
        "reason": "sparse_foreground_enlarged",
        "area_ratio_after_autoframe": round(width * height * new_scale * new_scale / (CANVAS_WIDTH * CANVAS_HEIGHT), 6),
    })
    return framed, diagnostic


def normalize_visual_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(plan, dict):
        raise RuntimeError("Visual plan must be a JSON object.")

    plan = canonicalize_visual_plan(plan)
    elements = _sanitize_elements(plan)
    fixed_canvas_coordinates = any(_background_element(element) for element in elements)
    layout = _compute_layout_transform(elements)
    # Enforce the publication floor after layout scaling, not merely on raw
    # font_size. Otherwise a valid 18px label silently becomes 14-16px when a
    # large composition is fitted to the canvas.
    for _ in range(4):
        scale = max(1e-6, float(layout.get("scale", 1.0)))
        changed = False
        for element in elements:
            if element.get("type") != "text":
                continue
            required = 16.0 if bool(element.get("minor_annotation")) else 18.0
            current = float(element.get("font_size", 18))
            if current * scale + 1e-6 < required:
                element["font_size"] = required / scale
                changed = True
        if not changed:
            break
        layout = _compute_layout_transform(elements)
    if fixed_canvas_coordinates:
        bounds = foreground_plan_bounds_raw({"elements": elements})
        foreground = {
            "background_element_count": sum(
                1 for element in elements if _background_element(element)
            ),
            "auto_framed": False,
            "scale_boost": 1.0,
            "reason": "fixed_canvas_background_coordinates",
        }
        if bounds:
            foreground["bounds_raw"] = {
                "x": bounds[0], "y": bounds[1],
                "width": bounds[2] - bounds[0], "height": bounds[3] - bounds[1],
            }
    else:
        layout, foreground = _foreground_autoframe(elements, layout)
    text_elements = [element for element in elements if element.get("type") == "text"]
    effective_sizes = [
        float(element.get("font_size", 18)) * float(layout.get("scale", 1.0))
        for element in text_elements
    ]
    legibility = {
        "layout_scale": round(float(layout.get("scale", 1.0)), 6),
        "minimum_effective_font_size": round(min(effective_sizes), 3) if effective_sizes else None,
        "text_below_14px_ids": [
            str(element.get("id", "text"))[:80]
            for element in text_elements
            if float(element.get("font_size", 18)) * float(layout.get("scale", 1.0)) < 14.0
        ],
        "minimum_required_font_size": 18.0,
        "text_below_minimum_ids": [
            str(element.get("id", "text"))[:80]
            for element in text_elements
            if (
                float(element.get("font_size", 18)) * float(layout.get("scale", 1.0)) < 18.0
                and not (
                    bool(element.get("minor_annotation"))
                    and float(element.get("font_size", 18))
                    * float(layout.get("scale", 1.0)) >= 16.0
                )
            )
        ],
    }

    normalized = {
        "canvas": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "elements": elements,
        "layout": layout,
        "_foreground": foreground,
        "_legibility": legibility,
    }
    # Preserve non-render metadata used by deterministic compilers, animation,
    # continuity diagnostics and the downloadable evaluation report.  Legacy
    # coordinate plans remain valid because none of these fields is required.
    for key in (
        "source", "storyboard", "_storyboard", "_freeform_repair", "diagram", "diagram_kind",
        "animations", "beat_states", "camera", "narration", "_graphic_style", "_semantic_constraints",
        "_continuity", "_explanation", "_runtime", "_quality_gate", "_beat_sync",
        "schema_version", "_canonicalization", "_candidate_selection", "_fallback",
    ):
        if key in plan:
            normalized[key] = plan[key]
    return normalized


# =========================================================
# ICONIFY SEARCH / CACHE / FETCH
# =========================================================

def _semantically_matching_icon(icons: List[Any], query: str, family: str = "") -> Optional[str]:
    """Choose a result sharing an actual name token with the search concept.

    This rejects misleading substring-only matches while still accepting
    compound icon names that share a complete requested concept token.
    """
    query_tokens = set(re.findall(r"[a-z0-9]+", str(query).lower())) - _ICON_QUERY_MODIFIERS
    ranked: List[Tuple[int, int, str]] = []
    for raw_icon in icons:
        icon = str(raw_icon)
        plain = icon.lstrip("@").split(":", 1)
        if len(plain) != 2 or (family and plain[0] != family):
            continue
        name_tokens = set(re.findall(r"[a-z0-9]+", plain[1].lower()))
        overlap = query_tokens & name_tokens
        coverage = len(overlap) / max(1, len(query_tokens))
        if overlap and (len(query_tokens) == 1 or coverage >= 0.66):
            ranked.append((len(overlap), -len(name_tokens), icon))
    return max(ranked)[2] if ranked else None


def _is_explicit_brand_query(query: str) -> bool:
    """Detect an identity/logo request without maintaining a brand table."""
    value = str(query or "").strip()
    if re.search(r"\b(logo|brand|trademark|company)\b", value, re.I):
        return True
    words = re.findall(r"[A-Za-z0-9]+", value)
    return any(
        (len(word) >= 3 and any(ch.islower() for ch in word)
         and sum(ch.isupper() for ch in word) >= 2)
        or (len(word) >= 2 and word.isupper())
        for word in words
    )

def search_iconify(query: str, preferred_family: Optional[str] = None) -> Optional[str]:
    query = str(query).strip()
    if not query:
        return None

    family = str(preferred_family or "").strip().lower()
    cache_key = (family, query.lower())
    with _ICON_SEARCH_LOCK:
        if cache_key in _ICON_SEARCH_CACHE:
            cached = _ICON_SEARCH_CACHE[cache_key]
            if _semantically_matching_icon([cached], query, family):
                return cached
        if cache_key in _ICON_NEGATIVE_SEARCH_CACHE:
            return None

    params = {
        "query": query,
        "limit": 24,
        "prefixes": family or ICONIFY_PREFIXES,
    }

    try:
        response = HTTP.get(
            f"{ICONIFY_BASE}/search",
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        icons = response.json().get("icons", [])
        if icons:
            result = _semantically_matching_icon(icons, query, family)
            if result:
                _remember_icon_search(cache_key, result)
                return result
        with _ICON_SEARCH_LOCK:
            _ICON_NEGATIVE_SEARCH_CACHE.add(cache_key)
    except (requests.RequestException, ValueError) as exc:
        print(f"Iconify preferred search failed for {query!r}: {exc}")

    if family and ICONIFY_STRICT_FAMILY:
        return None

    try:
        response = HTTP.get(
            f"{ICONIFY_BASE}/search",
            params={"query": query, "limit": 24},
            timeout=10,
        )
        response.raise_for_status()
        icons = response.json().get("icons", [])
        result = _semantically_matching_icon(icons, query)
        if result:
            _remember_icon_search(cache_key, result)
        return result
    except (requests.RequestException, ValueError) as exc:
        print(f"Iconify fallback search failed for {query!r}: {exc}")
        return None


_ICON_QUERY_MODIFIERS = {
    "icon", "icons", "symbol", "symbols", "outline", "outlined", "silhouette",
    "illustration", "graphic", "image", "simple", "minimal", "vector", "shape",
    "with", "and", "the", "of", "a", "an",
}

_ICON_WEAK_DESCRIPTORS = {
    "small", "large", "big", "tiny", "gold", "silver", "red", "green",
    "blue", "black", "white", "dark", "light", "modern", "classic",
    "open", "closed", "active", "inactive", "new", "old",
}

# Small semantic equivalence clusters bridge ordinary language to the naming
# conventions used by icon collections.  These are concept-level vocabulary,
# not topic templates: they apply to any prompt and still resolve inside the
# configured licensed family.
_ICON_CONCEPT_ALIASES = {
    "drop": ("water", "water-outline", "weather-rainy"),
    "droplet": ("water", "water-outline", "weather-rainy"),
    "rain": ("weather-rainy", "water", "weather-pouring"),
    "settings": ("cog", "gear"),
    "storage": ("database", "server", "harddisk"),
    "vehicle": ("car", "truck"),
    "home": ("house",),
    "building": ("office-building", "city"),
    "person": ("human", "account"),
    "user": ("account", "human"),
    "idea": ("lightbulb",),
    "message": ("message-text", "chat"),
    "document": ("file-document", "file"),
    "warning": ("alert", "alert-outline"),
    "success": ("check-circle", "check"),
    "checkmark": ("check", "check-circle", "checkbox-marked-circle"),
    "tick": ("check", "check-circle"),
}


def _concept_tokens(words: List[str]) -> List[str]:
    """Find useful concept words, including closed compounds like raindrop."""
    result: List[str] = []
    alias_words = tuple(_ICON_CONCEPT_ALIASES)
    for word in words:
        if word not in result:
            result.append(word)
        for concept in alias_words:
            if word != concept and len(word) > len(concept) and (
                word.startswith(concept) or word.endswith(concept)
            ):
                if concept not in result:
                    result.append(concept)
    return result


def icon_query_candidates(query: str) -> List[str]:
    """Generate generic shorter searches without knowing the input topic."""
    original = re.sub(r"\s+", " ", str(query or "").strip())
    if not original:
        return []
    words = re.findall(r"[A-Za-z0-9]+", original.lower())
    content = [word for word in words if word not in _ICON_QUERY_MODIFIERS]
    if len(content) > 1:
        content = [word for word in content if word not in _ICON_WEAK_DESCRIPTORS] or content
    concepts = _concept_tokens(content)
    candidates: List[str] = []

    def add(value: str) -> None:
        value = re.sub(r"\s+", " ", value).strip()
        if value and value not in candidates:
            candidates.append(value)

    cleaned = " ".join(content)
    if len(words) > 2 and cleaned:
        add(cleaned)
    # When a plain-language concept has established icon-library vocabulary,
    # prefer that vocabulary before an ambiguous exact search.
    for word in concepts:
        for alias in _ICON_CONCEPT_ALIASES.get(word, ()):
            add(alias)
    add(original)
    if len(content) >= 2:
        add(" ".join(content[:2]))
    for word in concepts:
        if len(word) >= 3:
            add(word)
    if len(content) >= 2:
        add(" ".join(content[-2:]))
    return candidates[:10]


def resolve_iconify(
    query: str, preferred_family: Optional[str] = None
) -> Tuple[Optional[str], Optional[str], int]:
    """Resolve within one family, retrying only deterministic query reductions."""
    attempts = 0
    original_value = re.sub(r"\s+", " ", str(query or "").strip())
    original = original_value.lower()
    configured_family = str(preferred_family or "").strip().lower()
    families = [configured_family] if configured_family else [""]
    explicit_brand = _is_explicit_brand_query(original_value)
    if explicit_brand:
        families = ["simple-icons"] + [value for value in families if value != "simple-icons"]
    original_tokens = [
        token for token in re.findall(r"[a-z0-9]+", original)
        if token not in _ICON_QUERY_MODIFIERS
    ]
    for family in families:
        for candidate in icon_query_candidates(query):
            attempts += 1
            icon_name = search_iconify(candidate, preferred_family=family or None)
            if icon_name:
                if not explicit_brand and len(original_tokens) >= 2 and not _semantically_matching_icon(
                    [icon_name], original_value, family
                ):
                    # A reduction such as "deep neural network" -> "network"
                    # can resolve to a generic network/monitor glyph. Validate
                    # the result against the full requested concept before
                    # accepting it as visual evidence.
                    continue
                if original and candidate.strip().lower() != original:
                    # Persist the semantic bridge itself. Repeated objects and
                    # future deployments resolve in O(1) without replaying misses.
                    _remember_icon_search((family, original), icon_name)
                return icon_name, candidate, attempts
    return None, None, attempts


def cache_filename(icon_name: str) -> Path:
    safe_name = icon_name.replace(":", "__").replace("/", "_")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", safe_name)
    return CACHE_DIR / f"{safe_name}.svg"


def fetch_icon_svg(icon_name: str) -> str:
    cache_file = cache_filename(icon_name)

    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8")

    if ":" not in icon_name:
        raise ValueError(f"Invalid Iconify icon name: {icon_name}")

    prefix, name = icon_name.split(":", 1)
    url = f"{ICONIFY_BASE}/{prefix}/{name}.svg"

    response = HTTP.get(
        url,
        params={"color": "#111111"},
        timeout=10,
    )
    response.raise_for_status()

    svg = response.text
    if "<svg" not in svg.lower() or "</svg>" not in svg.lower():
        raise ValueError(f"Invalid SVG returned for {icon_name}")

    cache_file.write_text(svg, encoding="utf-8")
    return svg


# =========================================================
# PARSE / SANITIZE ICONIFY SVG
# =========================================================

def extract_svg(svg: str) -> Dict[str, Any]:
    lower = svg.lower()

    forbidden = [
        "<script",
        "<foreignobject",
        "javascript:",
        "data:text/html",
    ]

    for item in forbidden:
        if item in lower:
            raise ValueError("Unsafe SVG content detected.")

    svg = re.sub(
        r"\s+on[a-zA-Z]+\s*=\s*(?:\"[^\"]*\"|'[^']*')",
        "",
        svg,
        flags=re.I,
    )

    viewbox_match = re.search(
        r'viewBox=["\']([^"\']+)["\']',
        svg,
        re.IGNORECASE,
    )

    if viewbox_match:
        parts = re.split(r"[\s,]+", viewbox_match.group(1).strip())
        if len(parts) == 4:
            try:
                min_x, min_y, width, height = map(float, parts)
            except ValueError:
                min_x, min_y, width, height = 0.0, 0.0, 24.0, 24.0
        else:
            min_x, min_y, width, height = 0.0, 0.0, 24.0, 24.0
    else:
        min_x, min_y, width, height = 0.0, 0.0, 24.0, 24.0

    match = re.search(
        r"<svg[^>]*>(.*?)</svg>",
        svg,
        re.DOTALL | re.IGNORECASE,
    )

    if not match:
        raise ValueError("Could not parse Iconify SVG.")

    return {
        "min_x": min_x,
        "min_y": min_y,
        "width": width,
        "height": height,
        "content": match.group(1),
    }


def namespace_svg_ids(content: str, namespace: str) -> str:
    ids = re.findall(r'\bid=["\']([^"\']+)["\']', content)
    for old_id in ids:
        new_id = f"{namespace}_{re.sub(r'[^A-Za-z0-9_.-]', '_', old_id)}"
        content = re.sub(
            rf'\bid=(["\']){re.escape(old_id)}\1',
            lambda m: f'id={m.group(1)}{new_id}{m.group(1)}',
            content,
        )
        content = content.replace(f"url(#{old_id})", f"url(#{new_id})")
        content = content.replace(f'href="#{old_id}"', f'href="#{new_id}"')
        content = content.replace(f"href='#{old_id}'", f"href='#{new_id}'")
        content = content.replace(f'xlink:href="#{old_id}"', f'xlink:href="#{new_id}"')
        content = content.replace(f"xlink:href='#{old_id}'", f"xlink:href='#{new_id}'")
    return content


def render_icon(
    icon: Dict[str, Any],
    x: float,
    y: float,
    target_width: float,
    target_height: float,
    namespace: str,
    rotation: float = 0.0,
    opacity: float = 1.0,
    sketch: bool = False,
    color: str = "#172033",
) -> str:
    source_width = float(icon["width"])
    source_height = float(icon["height"])

    if source_width <= 0 or source_height <= 0:
        return ""

    scale = min(target_width / source_width, target_height / source_height)
    rendered_width = source_width * scale
    rendered_height = source_height * scale

    left = x - rendered_width / 2
    top = y - rendered_height / 2

    min_x = float(icon["min_x"])
    min_y = float(icon["min_y"])
    content = namespace_svg_ids(icon["content"], namespace)
    # Iconify sets either currentColor or a fixed request colour. Recolouring
    # here also upgrades already-cached icons and keeps the entire video in one
    # consistent icon language without another network request.
    content = re.sub(r'(?i)(fill|stroke)=("|\')(?:#111111|#111|black|currentColor)\2',
                     lambda match: f'{match.group(1)}={match.group(2)}{esc(color)}{match.group(2)}', content)

    base_transform = (
        f"translate({left:.2f} {top:.2f}) "
        f"scale({scale:.6f}) "
        f"translate({-min_x:.2f} {-min_y:.2f})"
    )

    filter_attr = ' filter="url(#roughSketch)"' if sketch else ""

    if rotation:
        return (
            f'<g transform="rotate({rotation:.2f} {x:.2f} {y:.2f})" '
            f'opacity="{opacity:.3f}"{filter_attr}>'
            f'<g transform="{base_transform}">{content}</g>'
            f'</g>'
        )

    return (
        f'<g transform="{base_transform}" opacity="{opacity:.3f}"{filter_attr}>'
        f'{content}</g>'
    )


# =========================================================
# GENERIC SVG RENDERER
# =========================================================

def _sanitize_text_content(value: Any) -> str:
    """Clean a text element's CONTENT before it is rendered.

    The visual planner sometimes leaks animation-spec artifacts into text
    strings (e.g. `opacity=\"0\"` or `class=\"fade\"` fragments). Rendered as-is,
    those appear as literal text drawn on top of the diagram (the reported
    \"opacity=\"0\" Reasoning\" bug). Strip the leaked attribute patterns and
    collapse stray whitespace; keep real punctuation and parentheses that are
    part of the explanation (e.g. \"(LLM)\", \"(APIs, browsers)\").
    """
    text = unicodedata.normalize("NFKC", str(value))
    # JSON models sometimes emit a literal backslash-n. Treat it as authored
    # line structure instead of drawing "\\n" on the canvas.
    text = text.replace("\\n", "\n")
    text = text.replace("\ufffd", "").replace("□", "")
    # Remove attribute-like leaks: opacity="...", class="...", transform="...",
    # animate="...", data-...="..." — matched as whole tokens with smart quotes.
    text = re.sub(r'\b(?:opacity|class|transform|animate|id|data-\w+)\s*=\s*["\'][^"\']*["\']', '', text)
    # Preserve authored line breaks (especially code). Only collapse spaces
    # within each line; layout measurement and rendering then see the same text.
    return "\n".join(re.sub(r'[ \t]{2,}', ' ', line).strip() for line in text.splitlines()).strip()


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _style_attrs(e: Dict[str, Any], include_fill: bool = True) -> str:
    attrs = []
    if include_fill:
        attrs.append(f'fill="{esc(e.get("fill", "white"))}"')
    attrs.append(f'stroke="{esc(e.get("stroke", "#222"))}"')
    attrs.append(f'stroke-width="{float(e.get("stroke_width", 2)):.2f}"')
    attrs.append(f'opacity="{float(e.get("opacity", 1)):.3f}"')
    dash = str(e.get("dash", "")).strip()
    if dash:
        attrs.append(f'stroke-dasharray="{esc(dash)}"')
    if e.get("__stroke_opacity") is not None:
        attrs.append(f'stroke-opacity="{float(e["__stroke_opacity"]):.3f}"')
    if e.get("__linecap"):
        attrs.append(f'stroke-linecap="{esc(e["__linecap"])}"')
    if e.get("__linejoin"):
        attrs.append(f'stroke-linejoin="{esc(e["__linejoin"])}"')
    return " ".join(attrs)


def _effect_attr(e: Dict[str, Any], sketch: bool = False) -> str:
    if sketch and str(e.get("type", "")) != "text":
        return ' filter="url(#roughSketch)"'
    effect = str(e.get("__effect", ""))
    if effect in {"softShadow", "focusGlow"}:
        return f' filter="url(#{effect})"'
    return ""


def _marker_id(color: Any) -> str:
    return "arrowhead_" + hashlib.sha1(str(color).encode("utf-8")).hexdigest()[:10]


def _render_icon_monogram(element: Dict[str, Any], query: str) -> str:
    """Labeled card fallback used when no licensed icon resolves."""
    x = float(element["x"])
    y = float(element["y"])
    width = float(element["width"])
    height = float(element["height"])
    color = esc(element.get("__icon_color", "#172033"))
    words = re.findall(r"[A-Za-z0-9]+", _sanitize_text_content(query)) or ["Unknown"]
    midpoint = (len(words) + 1) // 2 if len(words) > 2 else len(words)
    lines = [" ".join(words[:midpoint])]
    if midpoint < len(words):
        lines.append(" ".join(words[midpoint:]))
    font_size = max(11.0, min(20.0, min(width / max(5, max(map(len, lines))) * 1.55, height * 0.22)))
    left, top = x - width / 2, y - height / 2
    line_height = font_size * 1.18
    first_y = y - (len(lines) - 1) * line_height / 2 + font_size * 0.34
    tspans = "".join(
        f'<tspan x="{x:.2f}" y="{first_y + index * line_height:.2f}">{esc(line)}</tspan>'
        for index, line in enumerate(lines)
    )
    return (
        f'<g opacity="{float(element.get("opacity", 1)):.3f}">'
        f'<rect x="{left:.2f}" y="{top:.2f}" width="{width:.2f}" height="{height:.2f}" '
        f'rx="{min(width, height) * 0.18:.2f}" fill="#F8FAFC" stroke="{color}" stroke-width="2"/>'
        f'<text x="{x:.2f}" y="{y + font_size * 0.34:.2f}" text-anchor="middle" '
        f'font-family="Inter, Segoe UI, Arial, sans-serif" font-size="{font_size:.2f}" '
        f'font-weight="700" fill="{color}">{tspans}</text></g>'
    )


def _rotation_wrapper_open(e: Dict[str, Any]) -> str:
    rotation = float(e.get("rotation", 0))
    if not rotation:
        return ""

    if e.get("__rotate_about") == "center":
        # diagram-engine arrowheads: polygons drawn around their tip; the
        # rotation center is the polygon's own centroid (cx/cy above).
        cx, cy = float(e.get("x", 0)), float(e.get("y", 0))
    elif e["type"] == "polygon":
        pts = _parse_points_pairs(e.get("points", ""))
        if pts:
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
        else:
            cx, cy = float(e.get("x", 0)), float(e.get("y", 0))
    elif e["type"] in {"line", "arrow"}:
        cx = (float(e.get("x1", 0)) + float(e.get("x2", 0))) / 2
        cy = (float(e.get("y1", 0)) + float(e.get("y2", 0))) / 2
    elif e["type"] == "rect":
        cx = float(e.get("x", 0)) + float(e.get("width", 0)) / 2
        cy = float(e.get("y", 0)) + float(e.get("height", 0)) / 2
    else:
        cx = float(e.get("x", 0))
        cy = float(e.get("y", 0))

    return f'<g transform="rotate({rotation:.2f} {cx:.2f} {cy:.2f})">'


def _rotation_wrapper_close(e: Dict[str, Any]) -> str:
    return "</g>" if float(e.get("rotation", 0)) else ""


def _wrap_text(text: str, max_chars: int = 30, max_lines: Optional[int] = None) -> List[str]:
    """Wrap text while preserving explicit line structure.

    Single-paragraph labels keep the historical three-line cap. Explicitly
    multiline content such as code may use more lines, bounded defensively.
    """
    text = str(text or "").strip()
    if not text:
        return [""]
    max_chars = max(8, min(120, int(max_chars)))
    explicit = text.splitlines()
    limit = max_lines if max_lines is not None else (12 if len(explicit) > 1 else 3)
    limit = max(1, min(24, int(limit)))
    lines: List[str] = []
    for source_line in explicit:
        source_line = source_line.strip()
        if not source_line:
            lines.append("")
            continue
        words = []
        for token in source_line.split():
            if len(token) <= max_chars:
                words.append(token)
            else:
                # Identifiers, URLs and tracking numbers often contain no
                # spaces. Split them deterministically instead of letting one
                # token escape its owner container.
                words.extend(token[index:index + max_chars] for index in range(0, len(token), max_chars))
        current = words[0]
        for word in words[1:]:
            candidate = current + " " + word
            if len(candidate) <= max_chars:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    if len(lines) > limit:
        lines = lines[:limit]
        if lines[-1] and not lines[-1].endswith("…"):
            lines[-1] = lines[-1].rstrip(" .") + "…"
    return lines or [text]


def render_visual_plan(
    plan: Dict[str, Any],
    sketch: bool = False,
) -> Tuple[str, List[Dict[str, str]]]:
    canvas = plan["canvas"]
    width = int(canvas["width"])
    height = int(canvas["height"])
    layout = plan.get("layout", {})

    scale = float(layout.get("scale", 1.0))
    tx = float(layout.get("translate_x", 0.0))
    ty = float(layout.get("translate_y", 0.0))

    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    ]

    arrow_colors = sorted({
        str(element.get("stroke", "#172033"))
        for element in plan.get("elements", [])
        if str(element.get("type", "")).lower() == "arrow"
    })
    marker_defs = "\n".join(
        f'<marker id="{_marker_id(color)}" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto" markerUnits="strokeWidth">'
        f'<path d="M0,0 L10,4 L0,8 Z" fill="{esc(color)}"/></marker>'
        for color in arrow_colors
    )
    parts.append(
        f"""
<defs>
  {marker_defs}
  <filter id="roughSketch" x="-20%" y="-20%" width="140%" height="140%">
    <feTurbulence type="fractalNoise" baseFrequency="0.008" numOctaves="2" seed="7" result="noise"/>
    <feDisplacementMap in="SourceGraphic" in2="noise" scale="1.35" xChannelSelector="R" yChannelSelector="G"/>
  </filter>
  <filter id="softShadow" x="-25%" y="-25%" width="150%" height="160%" color-interpolation-filters="sRGB">
    <feDropShadow dx="0" dy="7" stdDeviation="7" flood-color="#172033" flood-opacity="0.16"/>
  </filter>
  <filter id="focusGlow" x="-30%" y="-30%" width="160%" height="170%" color-interpolation-filters="sRGB">
    <feDropShadow dx="0" dy="8" stdDeviation="9" flood-color="#2563EB" flood-opacity="0.25"/>
  </filter>
</defs>
"""
    )

    graphic_meta = plan.get("_graphic_style") if isinstance(plan.get("_graphic_style"), dict) else {}
    base_background = graphic_meta.get("base_background", "white")
    parts.append(f'<rect width="{width}" height="{height}" fill="{esc(base_background)}"/>')

    # The whole composition is transformed uniformly. This is what prevents a
    # y=1000 plan from being clamped into a pile at the bottom of the canvas.
    parts.append(f'<g transform="translate({tx:.4f} {ty:.4f}) scale({scale:.6f})">')

    used_icons: List[Dict[str, str]] = []

    for index, e in enumerate(plan["elements"]):
        kind = e["type"]
        shape_filter = _effect_attr(e, sketch=sketch)

        if kind == "icon":
            query = e["query"]
            family = str(e.get("__icon_family") or graphic_meta.get("icon_family") or "").strip()
            icon_name, resolved_query, resolution_attempts = resolve_iconify(
                query, preferred_family=family or None
            )
            graphic_meta["icon_resolution_attempts"] = int(
                graphic_meta.get("icon_resolution_attempts", 0)
            ) + resolution_attempts
            if resolved_query and resolved_query.strip().lower() != query.strip().lower():
                graphic_meta["icon_query_reductions"] = int(
                    graphic_meta.get("icon_query_reductions", 0)
                ) + 1

            if not icon_name:
                print(f"No Iconify result for: {query}")
                # A semantic monogram is more informative than the old generic
                # dot fallback and keeps one SVG element per visual-plan item.
                parts.append(_render_icon_monogram(e, query))
                graphic_meta["icon_monogram_fallbacks"] = int(graphic_meta.get("icon_monogram_fallbacks", 0)) + 1
                graphic_meta.setdefault("icon_resolutions", []).append({
                    "query": query, "status": "monogram_fallback",
                    "attempts": resolution_attempts,
                    "fallback_reason": "no_semantically_matching_icon",
                    "semantic_confidence": "rejected_or_unavailable",
                    "license": None, "license_status": "not_resolved",
                })
                continue

            try:
                raw_svg = fetch_icon_svg(icon_name)
                parsed = extract_svg(raw_svg)
                rendered = render_icon(
                    parsed,
                    float(e["x"]),
                    float(e["y"]),
                    float(e["width"]),
                    float(e["height"]),
                    namespace=f"icon_{index}",
                    rotation=float(e.get("rotation", 0)),
                    opacity=float(e.get("opacity", 1)),
                    sketch=sketch,
                    color=str(e.get("__icon_color", "#172033")),
                )
                parts.append(rendered)
                used_icons.append({
                    "query": query, "resolved_query": resolved_query or query,
                    "icon": icon_name,
                    "family": icon_name.split(":", 1)[0],
                    "semantic_confidence": "validated_name_token_overlap",
                    "license": None,
                    "license_status": "collection_metadata_not_fetched",
                })
                graphic_meta["icons_resolved"] = int(graphic_meta.get("icons_resolved", 0)) + 1
                graphic_meta.setdefault("icon_resolutions", []).append({
                    "query": query, "status": "resolved",
                    "resolved_query": resolved_query or query,
                    "icon": icon_name, "attempts": resolution_attempts,
                    "family": icon_name.split(":", 1)[0],
                    "semantic_confidence": "validated_name_token_overlap",
                    "license": None,
                    "license_status": "collection_metadata_not_fetched",
                })
            except Exception as exc:
                print(f"Failed to render icon {icon_name}: {exc}")
                parts.append(_render_icon_monogram(e, query))
                graphic_meta["icon_monogram_fallbacks"] = int(graphic_meta.get("icon_monogram_fallbacks", 0)) + 1
                graphic_meta.setdefault("icon_resolutions", []).append({
                    "query": query, "status": "render_fallback",
                    "resolved_query": resolved_query or query,
                    "icon": icon_name, "attempts": resolution_attempts,
                    "family": icon_name.split(":", 1)[0],
                    "fallback_reason": str(exc)[:300],
                    "semantic_confidence": "validated_but_render_failed",
                    "license": None,
                    "license_status": "collection_metadata_not_fetched",
                })
            continue

        open_wrap = _rotation_wrapper_open(e)
        close_wrap = _rotation_wrapper_close(e)
        if open_wrap:
            parts.append(open_wrap)

        if kind == "rect":
            parts.append(
                f'<rect x="{e["x"]:.2f}" y="{e["y"]:.2f}" '
                f'width="{e["width"]:.2f}" height="{e["height"]:.2f}" '
                f'rx="{e.get("rx", 8):.2f}" {_style_attrs(e)}{shape_filter}/>'
            )

        elif kind == "circle":
            parts.append(
                f'<circle cx="{e["x"]:.2f}" cy="{e["y"]:.2f}" r="{e["radius"]:.2f}" '
                f'{_style_attrs(e)}{shape_filter}/>'
            )

        elif kind == "ellipse":
            parts.append(
                f'<ellipse cx="{e["x"]:.2f}" cy="{e["y"]:.2f}" '
                f'rx="{e["rx"]:.2f}" ry="{e["ry"]:.2f}" {_style_attrs(e)}{shape_filter}/>'
            )

        elif kind == "line":
            parts.append(
                f'<line x1="{e["x1"]:.2f}" y1="{e["y1"]:.2f}" '
                f'x2="{e["x2"]:.2f}" y2="{e["y2"]:.2f}" '
                f'{_style_attrs(e, include_fill=False)}{shape_filter}/>'
            )

        elif kind == "arrow":
            parts.append(
                f'<line x1="{e["x1"]:.2f}" y1="{e["y1"]:.2f}" '
                f'x2="{e["x2"]:.2f}" y2="{e["y2"]:.2f}" '
                f'{_style_attrs(e, include_fill=False)} marker-end="url(#{_marker_id(e.get("stroke", "#172033"))})"{shape_filter}/>'
            )

        elif kind == "path":
            parts.append(
                f'<path d="{esc(e["d"])}" {_style_attrs(e)}{shape_filter}/>'
            )

        elif kind == "polygon":
            parts.append(
                f'<polygon points="{esc(e["points"])}" {_style_attrs(e)}{shape_filter}/>'
            )

        elif kind == "polyline":
            parts.append(
                f'<polyline points="{esc(e["points"])}" {_style_attrs(e)}{shape_filter}/>'
            )

        elif kind == "text":
            anchor = {"left": "start", "center": "middle", "right": "end"}.get(
                e.get("align"), "start"
            )
            font_weight = esc(e.get("font_weight", "normal"))
            lines = _wrap_text(
                _sanitize_text_content(e["text"]),
                max_chars=int(e.get("__max_chars", 30)),
                max_lines=int(e.get("__max_lines", 12)),
            )
            line_height = float(e["font_size"]) * 1.18

            if len(lines) == 1:
                parts.append(
                    f'<text x="{e["x"]:.2f}" y="{e["y"]:.2f}" text-anchor="{anchor}" '
                    f'font-family="{esc(e.get("__font_family", "Arial, sans-serif"))}" font-size="{e["font_size"]:.2f}" '
                    f'font-weight="{font_weight}" fill="{esc(e["fill"])}" '
                    f'opacity="{float(e.get("opacity", 1)):.3f}">{esc(lines[0])}</text>'
                )
            else:
                tspans = []
                for i, line in enumerate(lines):
                    dy = 0 if i == 0 else line_height
                    tspans.append(
                        f'<tspan x="{e["x"]:.2f}" dy="{dy:.2f}">{esc(line)}</tspan>'
                    )
                parts.append(
                    f'<text x="{e["x"]:.2f}" y="{e["y"]:.2f}" text-anchor="{anchor}" '
                    f'font-family="{esc(e.get("__font_family", "Arial, sans-serif"))}" font-size="{e["font_size"]:.2f}" '
                    f'font-weight="{font_weight}" fill="{esc(e["fill"])}" '
                    f'opacity="{float(e.get("opacity", 1)):.3f}">' + "".join(tspans) + "</text>"
                )

        if close_wrap:
            parts.append(close_wrap)

    parts.append("</g>")
    parts.append("</svg>")
    return "\n".join(parts), used_icons
