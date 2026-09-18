"""Narration-aligned, object-level scene animation (token-efficient).

Design
------
The LLM emits a tiny typed animation list per scene (no prose, fixed JSON
schema) alongside the static visual plan. Each entry says which element
animates, how, when, and for how long:

    {"t": 3, "e": 12, "a": "draw", "d": 0.8}
       t = appear_at seconds (narration time)
       e = element index in the visual plan
       a = animation type
       d = duration in seconds

Supported animation types: fade, draw, move, scale, pulse, pop, wipe.

Rendering
---------
For each frame in the scene we template the SVG in pure Python (inline
attribute values — works with the fast ffmpeg+librsvg renderer, no CSS
needed), rasterize to PNG, and encode the frame series into the segment
MP4 with correct per-frame hold times so animations line up with the
narration. Frame budget is capped (default 16) so cost and latency stay
small.

Token budget
------------
- One extra system block (~900 chars) reused for all scenes.
- Output per scene: <= 16 entries of 6 short fields ≈ 300-500 extra tokens.
- No extra LLM call: the list is part of the same visual plan response.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

MAX_ANIMATIONS_PER_SCENE = int(
    __import__("os").environ.get("MAX_ANIMATIONS_PER_SCENE", "16")
)
ANIM_SCENE_FPS = int(__import__("os").environ.get("ANIM_SCENE_FPS", "12"))
ANIM_ENABLED = __import__("os").environ.get(
    "ANIM_ENABLED", "1"
).strip().lower() in {"1", "true", "yes", "on"}

ANIM_SYSTEM_PROMPT = """
STRICT REQUIREMENT — you MUST include an "animations" array in your plan:
Each entry animates ONE element in sync with the narration:

{"t": 3.2, "e": 12, "a": "draw", "d": 0.8}

Fields:
- t  : seconds from scene start when the animation happens (align with narration)
- e  : index of the element in "elements" (0-based, SAME ORDER as your elements array)
- a  : one of fade | draw | move | scale | pulse | pop | wipe
       fade = element appears (use for most elements)
       draw = path/line/arrow draws in (stroke-based)
       move = element slides slightly to indicate flow
       scale = element grows to draw attention
       pulse = brief emphasis highlight
       pop  = appears with a small bounce (use for key new objects)
       wipe = reveal left-to-right (for result/emphasis boxes)
- d  : duration in seconds (0.2-1.2)
- w  : OPTIONAL 1-3 words copied EXACTLY from the narration that this animation
       illustrates (e.g. w=["cache","memory"]). Used to lock the animation to
       the exact moment those words are spoken. Leave out when unsure.

Rules: 4-16 entries. Animate at least one element per scene. Use the right type
per semantics; most elements just fade. Never animate decorative/support
elements. Never exceed the scene's narration length. Return terse numeric
values only.

BUDGET RULE (apply ONLY when a scene_budget block is given):
The ENTIRE video has one shared element budget across all scenes. A scene is
told its position and its allocated share. IMPORTANT scenes may use slightly
more than their share; simple scenes must use less. Never exceed the per-scene
maximum. Keep every scene lean: redundant decorative shapes, background frames,
and duplicate labels waste budget — spend elements on the idea, not on filler.
Same rule for animations: spend them on elements that advance the argument."""

ANIMATION_TYPES = {"fade", "draw", "move", "scale", "pulse", "pop", "wipe"}

FALLBACK_DURATIONS = {"fade": 0.4, "draw": 0.6, "move": 0.5, "scale": 0.45,
                      "pulse": 0.5, "pop": 0.5, "wipe": 0.6}

FFMPEG_BIN = __import__("os").environ.get(
    "FFMPEG_BIN", "/usr/bin/ffmpeg"
)

_FAST_SVG_ANIMATION_AVAILABLE: Optional[bool] = None


def fast_svg_animation_available() -> bool:
    """Return whether per-frame SVG rasterization is locally fast/supported."""
    global _FAST_SVG_ANIMATION_AVAILABLE
    if _FAST_SVG_ANIMATION_AVAILABLE is not None:
        return _FAST_SVG_ANIMATION_AVAILABLE
    try:
        import cairosvg
        rendered = cairosvg.svg2png(
            bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2"/></svg>',
            output_width=2,
            output_height=2,
        )
        if rendered:
            _FAST_SVG_ANIMATION_AVAILABLE = True
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        completed = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-decoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
        )
        output = completed.stdout.decode("utf-8", errors="replace").lower()
        _FAST_SVG_ANIMATION_AVAILABLE = bool(
            re.search(r"\bsvg\b|librsvg", output)
        )
    except Exception:  # noqa: BLE001
        _FAST_SVG_ANIMATION_AVAILABLE = False
    return bool(_FAST_SVG_ANIMATION_AVAILABLE)


def sanitize_animations(raw: Any, narration_seconds: float, n_elements: int) -> List[Dict[str, Any]]:
    """Normalize and validate a raw LLM animation list.

    Keeps the token cost low by being strict: invalid entries are dropped or
    clamped instead of triggering an expensive repair call.
    """
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw[: MAX_ANIMATIONS_PER_SCENE + 8]:
        if not isinstance(item, dict):
            continue
        t = _num(item.get("t"), 0.0)
        e = _num(item.get("e"), None)
        a = str(item.get("a", "fade")).strip().lower()
        d = _num(item.get("d"), None)
        if a not in ANIMATION_TYPES:
            continue
        if e is None or not (0 <= e < n_elements):
            continue
        if d is None or not (0.1 <= d <= 2.0):
            d = FALLBACK_DURATIONS.get(a, 0.4)
        t = max(0.0, min(t, max(0.3, narration_seconds - 0.1)))
        entry: Dict[str, Any] = {"t": round(t, 3), "e": int(e), "a": a, "d": round(d, 3)}
        # 'w' (optional word list) is passed through to the renderer for
        # whisper word-timestamp alignment; invalid shapes are dropped.
        w = item.get("w")
        if isinstance(w, list) and all(isinstance(x, str) and x.strip() for x in w):
            entry["w"] = [str(x).strip() for x in w[:3]]
        beat_id = str(item.get("beat_id") or item.get("_beat_id") or "").strip()
        if beat_id:
            entry["_beat_id"] = beat_id[:64]
        out.append(entry)
    return out


def _num(value: Any, default: Optional[float]) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_animations_from_plan(plan: Dict[str, Any], narration_seconds: float) -> List[Dict[str, Any]]:
    """Extract and sanitize the animation list from a visual plan dict."""
    raw = plan.get("animations") or []
    if not raw:
        return []
    n = len(plan.get("elements", []))
    cleaned = sanitize_animations(raw, narration_seconds, n)
    plan["animations"] = cleaned
    return cleaned


# ---------------------------------------------------------------------------
# per-animation SVG attribute templates
# ---------------------------------------------------------------------------

# Each template returns (open, close) SVG fragments interpolated at progress
# p in [0, 1]. 'open' wraps the element; 'close' ends the wrapper. For
# attribute-injection animations we return ('', '') and mutate attrs instead
# via element_attrs_progress().

def element_attrs_progress(anim: Dict[str, Any], p: float, element: Dict[str, Any]) -> Dict[str, str]:
    """Extra inline attributes for `element` at animation progress p."""
    kind = anim["a"]
    if p <= 0.0:
        # before start: invisible for appearance kinds, normal otherwise
        if kind in {"fade", "pop"}:
            return {"opacity": "0"}
        if kind == "draw":
            return {}  # drawn gradually; start at full dashoffset handled below
        if kind == "wipe":
            # librsvg does not support CSS clip-path: use an opacity-free
            # 'hidden' state. The wipe itself animates via scaleX in a wrapper.
            return {"opacity": "0"}
        if kind in {"move", "scale", "pulse"}:
            return {}
        return {}

    ease = _ease(p)
    if kind == "fade":
        return {"opacity": f"{ease:.3f}"}
    if kind == "pop":
        s = 0.8 + 0.2 * _pop_curve(p)
        return {"transform": f"scale({s:.3f})", "opacity": f"{min(1.0, p * 3):.3f}"}
    if kind == "scale":
        s = 0.55 + 0.45 * ease
        return {"transform": f"scale({s:.3f})"}
    if kind == "pulse":
        amp = 0.10 * (1.0 - p)
        val = 1.0 + amp * __import__("math").sin(p * __import__("math").pi)
        return {"opacity": f"{min(1.0, val):.3f}"}
    if kind == "move":
        dx = 18.0 * (1.0 - ease)
        dy = 10.0 * (1.0 - ease)
        return {"transform": f"translate({dx:.2f} {dy:.2f})"}
    if kind == "wipe":
        # librsvg-safe wipe: scale horizontally from 0 (right edge reveal).
        # transform-origin right is not supported either, so shrink the X
        # axis from the left and rely on the reveal sweep feeling natural.
        s = 0.001 + 0.999 * ease
        return {"transform": f"scale({s:.3f} 1)"}
    return {}


def _ease(p: float) -> float:
    return min(1.0, max(0.0, 1.0 - (1.0 - p) ** 3))  # ease-out cubic


def _pop_curve(p: float) -> float:
    # fast overshoot bounce: peaks at p~0.65 then settles to 1
    if p <= 0.65:
        return p / 0.65 * 1.12
    return 1.12 - 0.12 * ((p - 0.65) / 0.35)


def draw_dash_progress(anim: Dict[str, Any], p: float, element: Dict[str, Any]) -> Optional[str]:
    """If this animation is stroke-based, return a stroke-dasharray/dashoffset
    string describing progress p. Returns None if not stroke-based."""
    kind = anim["a"]
    if kind != "draw":
        return None
    # estimate path length from element geometry
    est = _estimate_length(element)
    if est <= 0:
        est = 400.0
    remaining = est * (1.0 - _ease(p))
    return f'stroke-dasharray="{est:.0f}" stroke-dashoffset="{remaining:.0f}"'


def _estimate_length(element: Dict[str, Any]) -> float:
    t = str(element.get("type", ""))
    if t in {"line", "arrow"}:
        dx = float(element.get("x2", 0)) - float(element.get("x1", 0))
        dy = float(element.get("y2", 0)) - float(element.get("y1", 0))
        return (dx * dx + dy * dy) ** 0.5
    if t == "rect":
        return 2.0 * (float(element.get("width", 0)) + float(element.get("height", 0)))
    if t in {"circle", "ellipse"}:
        rx = float(element.get("radius", element.get("rx", 30)))
        return 2.0 * 3.1416 * rx
    if t == "path" and isinstance(element.get("d"), str):
        nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", element["d"])]
        if len(nums) >= 4:
            return sum(abs(a - b) for a, b in zip(nums[::2], nums[1::2]))
    return 0.0


# ---------------------------------------------------------------------------
# frame scheduling
# ---------------------------------------------------------------------------

@dataclass
class AnimFrame:
    hold_seconds: float  # real time this frame represents
    per_element: Dict[int, float]  # element index -> progress in [0,1]
    time: float = 0.0        # absolute scene time at frame center (seconds)
    seed: int = 0            # per-job determinism seed (camera variety)


def _normalize_word(text: str) -> str:
    """Casefold a word for matching (whisper words and planner 'w' lists)."""
    return re.sub(r"[^a-z0-9'\-]", "", str(text or "").lower()).strip()


def _align_animation_to_words(
    animation: Dict[str, Any],
    words: List[Dict[str, Any]],
) -> Optional[float]:
    """Find the whisper start time of an animation's anchor words.

    The animation's 'w' field lists the narration words it illustrates; we
    align to the START of the FIRST listed word that actually appears in the
    alignment. If none of the anchor words appear, return None (caller keeps
    the blended narration-relative timing).
    """
    anchors = animation.get("w") or []
    if not words or not anchors:
        return None
    norm_words = [(_normalize_word(wd.get("word", "")), wd) for wd in words]
    # Build a normalized narration word list in order (whisper sometimes splits
    # punctuation; match the anchor against whole and merged runs).
    for anchor in anchors:
        na = _normalize_word(anchor)
        if not na:
            continue
        for nw, wd in norm_words:
            if na == nw:
                return float(wd["start"])
        # fuzzy: anchor contained within a whisper word (e.g. 'memory,' → 'memory')
        for nw, wd in norm_words:
            if na in nw or nw in na:
                return float(wd["start"])
    return None


def narrate_align_animations(
    animations: List[Dict[str, Any]],
    total_seconds: float,
    narration: str = "",
    words: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Re-sequence animations to feel narration-driven (teacher pacing).

    Two deterministic transforms, pure Python, zero LLM tokens:
    1. Clump-and-distribute: when several animations start within a short
       window, keep them but stretch the clump so each element appears when
       the narration reaches it (rough phrase model: narration words evenly
       spaced across total_seconds; a 't' is nudged toward the moment the
       element's own label/position suggests, approximated by element order).
    2. Pause moments: compress idle gaps longer than ~1.5s so the scene never
       visually stalls while the narration is still flowing (we shift late
       animations earlier to fill the gap).

    Element order approximates narrative order because render_visual_plan
    emits elements in plan order, and the planner is told to list elements
    in the order they are spoken about.
    """
    if not animations or total_seconds <= 0:
        return animations
    out = [dict(a) for a in animations]

    beat_timed = all(
        "_beat_start_fraction" in animation and "_beat_end_fraction" in animation
        for animation in out
    )

    # Sort by element index (narrative order) then by original time, so the
    # viewer follows the same order the visual was designed with.
    out.sort(key=lambda a: (a["e"], a["t"]))

    # Spread: place animations evenly-ish across the narration, weighted by
    # element index. Earlier elements appear earlier; later ones later.
    # Keep each animation within [0, total - d].
    n = len(out)
    max_start = max(0.05, total_seconds - max((a["d"] for a in out), default=0.4))
    if beat_timed:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for animation in out:
            groups.setdefault(str(animation.get("_beat_id", "")), []).append(animation)
        for group in groups.values():
            group.sort(key=lambda animation: (
                bool(animation.get("_late_emphasis")), animation["e"], animation["t"]
            ))
            for rank, animation in enumerate(group):
                start = float(animation.get("_beat_start_fraction", 0)) * total_seconds
                end = float(animation.get("_beat_end_fraction", 1)) * total_seconds
                usable = max(0.1, end - start - animation["d"])
                fraction = rank / max(1, len(group) - 1) if len(group) > 1 else 0.45
                animation["t"] = round(max(0.05, min(max_start, start + 0.08 + usable * fraction)), 3)
    elif n > 1:
        indices = sorted({a["e"] for a in out})
        imin, imax = indices[0], indices[-1]
        span = (imax - imin) if imax > imin else 1
        for a in out:
            frac = ((a["e"] - imin) / span) if span else 0.5
            # blend: 60% toward narrative order, 40% keep LLM's timing (it may
            # know things we don't, e.g. key moment emphasis)
            blended = 0.4 * a["t"] + 0.6 * frac * max_start
            a["t"] = round(max(0.05, min(max_start, blended)), 3)

    # Pause moments: collapse gaps > 1.5s by pulling the later animation's
    # start up to at most 1.5s after the previous animation's end.
    if not beat_timed:
        out.sort(key=lambda a: a["t"])
        prev_end = 0.0
        for a in out:
            if a["t"] - prev_end > 1.5 and a["t"] > 0.6:
                a["t"] = round(prev_end + 1.2, 3)
            prev_end = max(prev_end, a["t"] + a["d"])
    out.sort(key=lambda a: (a["e"], a["t"]))

    # ---- feature B: whisper word-timestamp anchoring -------------------------
    # For animations carrying a 'w' list, the whispered narration audio tells
    # us the exact moment those words are spoken. Anchor the animation to that
    # moment (blend 70% whisper / 30% narrative-order time so a misaligned
    # whisper word can't drag an animation into an absurd position).
    if words:
        for rank, a in enumerate(out):
            anchored = _align_animation_to_words(a, words)
            if anchored is None and not beat_timed:
                # Not every planner animation carries explicit anchor words.
                # Map its narrative-order rank to an actually spoken word so
                # the complete visual sequence spans the complete narration
                # instead of ending halfway through the scene.
                word_index = round(
                    (rank / max(1, len(out) - 1)) * max(0, len(words) - 1)
                )
                anchored = float(words[word_index].get("start", a["t"]))
            # Exact word timing dominates, while a small amount of the
            # planner's intended timing preserves deliberate visual grouping.
            if anchored is not None:
                blended = 0.85 * anchored + 0.15 * a["t"]
                a["t"] = round(max(0.05, min(max_start, blended)), 3)

    # Keep composite objects coherent.  A label/detail must never appear
    # before the container or pictured object it belongs to, even when Whisper
    # finds the label's anchor word earlier.
    id_to_animation: Dict[str, Dict[str, Any]] = {}
    for animation in out:
        element = animation.get("_element")
        if not isinstance(element, dict):
            continue
        element_id = str(element.get("id", "")).strip()
        if element_id and (
            element_id not in id_to_animation
            or float(animation["t"]) < float(id_to_animation[element_id]["t"])
        ):
            id_to_animation[element_id] = animation
    for animation in out:
        element = animation.get("_element")
        if not isinstance(element, dict):
            continue
        owner_id = str(element.get("owner_id") or element.get("label_for") or "").strip()
        owner_animation = id_to_animation.get(owner_id)
        if owner_animation is None:
            continue
        minimum_start = float(owner_animation["t"]) + min(0.12, float(owner_animation["d"]) * 0.35)
        if float(animation["t"]) < minimum_start:
            animation["t"] = round(min(max_start, minimum_start), 3)

    out.sort(key=lambda a: (a["e"], a["t"]))
    return out


def build_frame_schedule(
    animations: List[Dict[str, Any]], total_seconds: float, fps: int = ANIM_SCENE_FPS
) -> List[AnimFrame]:
    """Produce the frame list for one scene.

    Each animation occupies [t, t+d]; the element's progress runs 0→1 across
    that window (held at 1 afterward). Frames between events are cheap
    duplicates so total frames stay bounded.
    """
    fps = max(2, min(12, fps))
    requested_frames = int(max(2, round(total_seconds * fps)))
    max_frames = max(2, int(__import__("os").environ.get("ANIM_MAX_FRAMES", "240")))
    sample_count = min(requested_frames, max_frames)
    # Always cover the complete narration.  The former loop stopped when it
    # reached ANIM_MAX_FRAMES, which could leave a long scene with no trailing
    # visual hold and made higher FPS settings truncate the schedule.
    frame_dt = total_seconds / sample_count

    windows = [(a["e"], a["t"], a["t"] + a["d"]) for a in animations]

    frames: List[AnimFrame] = []
    t = 0.0
    while len(frames) < sample_count:
        progress: Dict[int, float] = {}
        for e, a_t, a_end in windows:
            if t < a_t:
                p = 0.0
            elif t >= a_end:
                p = 1.0
            else:
                p = (t - a_t) / max(1e-6, a_end - a_t)
            progress[e] = p
        frames.append(AnimFrame(hold_seconds=frame_dt, per_element=progress,
                                time=min(total_seconds, t + frame_dt / 2.0)))
        t += frame_dt

    # Keep the trailing hold frames (everything at progress 1.0 after the
    # last animation end) — they are what makes the video last the full
    # narration length. Previously these were trimmed away, which made the
    # video shorter than the audio and silently truncated the narration.
    return frames


# ---------------------------------------------------------------------------
# render one scene (SVG text -> animated segment.mp4)
# ---------------------------------------------------------------------------

def _iter_element_matches(svg: str):
    """Yield (index, match) for renderable elements only.

    Skips everything inside <defs> (markers, filters) and the layout wrapper
    <g transform="translate(tx ty) scale(s)"> that render_visual_plan opens
    around all elements, so plan index == visual index.
    """
    defs_depth = 0
    layout_skipped = False
    icon_group_depth = 0
    canvas_background_skipped = False
    idx = 0
    for m in re.finditer(
        r"</?(defs|g|rect|circle|ellipse|line|path|polygon|polyline|text)(\s[^>]*)?>",
        svg,
    ):
        name = m.group(1)
        is_close = m.group(0).startswith("</")
        if name == "defs":
            defs_depth += -1 if is_close else 1
            continue
        if defs_depth > 0:
            continue
        # An icon is one visual-plan element rendered as a top-level <g>
        # containing one or more SVG shapes. Count the wrapper once and skip
        # its implementation details; otherwise every icon shifts all later
        # animation indices.
        if icon_group_depth > 0:
            if name == "g":
                icon_group_depth += -1 if is_close else 1
            continue
        if not is_close:
            # render_visual_plan emits a full-canvas background before the
            # layout wrapper.  It is renderer infrastructure, not element 0
            # from the visual plan. Counting it shifted every animation index
            # and could hide the entire canvas until the first reveal.
            if name == "rect" and not layout_skipped and not canvas_background_skipped:
                attrs = m.group(2) or ""
                width = re.search(r'\bwidth="([\d.]+)"', attrs)
                height = re.search(r'\bheight="([\d.]+)"', attrs)
                if width and height and float(width.group(1)) >= 1000 and float(height.group(1)) >= 500:
                    canvas_background_skipped = True
                    continue
            # The layout wrapper is the FIRST <g ...> and carries ONLY a
            # transform (icons carry extra opacity/filter attrs too).
            if name == "g" and not layout_skipped:
                mm = re.match(
                    r'^\s*transform="translate\([\d\-. ]+\) scale\([\d\-.]+\)"\s*$',
                    m.group(2) or "",
                )
                if mm:
                    layout_skipped = True
                    continue
            yield idx, m
            idx += 1
            if name == "g":
                icon_group_depth = 1


def _strip_attr(svg: str, element_index: int, attr_name: str) -> str:
    """Remove an existing attribute from the element's opening tag so that a
    later re-injection can never produce a duplicate attribute (XML error)."""
    # Icons are emitted by render_visual_plan as a top-level
    # <g transform="..." opacity="..."><path .../></g> wrapper, so count
    # transform-bearing <g> tags as elements too.
    for idx, m in _iter_element_matches(svg):
        if idx == element_index:
            tag = m.group(0)
            tag = re.sub(r'\s+' + re.escape(attr_name) + r'="[^"]*"', "", tag, count=1)
            return svg[: m.start()] + tag + svg[m.end():]
    return svg


def _patch_element_in_svg(svg: str, element_index: int, extra_attrs: str) -> str:
    """Insert extra inline attributes into element at position `element_index`.

    The SVG produced by render_visual_plan emits elements in plan order as
    top-level children of the layout <g>. We count element open-tags.
    Pre-existing attributes that would conflict with the injection are
    stripped first to keep the XML valid.

    Icon elements are rendered as a <g transform="..." opacity="..."><path
    .../></g> wrapper. Injecting the same attributes onto the wrapper would
    duplicate them, so for icon groups we instead wrap the inner path in a
    fresh <g> carrying the animation attributes (which compose correctly with
    the wrapper's existing transform).
    """
    # collect the attributes we plan to inject
    inj_keys = [
        key for key, _ in re.findall(
            r'([A-Za-z_:][\w:.-]*)="([^"]*)"', extra_attrs
        )
    ]

    for idx, m in _iter_element_matches(svg):
        if idx == element_index:
            tag = m.group(0)
            if tag.startswith("<g"):
                # icon wrapper: apply animation attrs to a nested group
                return _patch_icon_inner(svg, m, extra_attrs)
            # strip any pre-existing attribute that would conflict with the
            # injection (elements may already carry opacity/transform/etc.,
            # e.g. deterministic fallback defaults) — duplicate attributes
            # are invalid XML and make librsvg bail with exit 69
            for attr_name in inj_keys:
                tag = re.sub(r'\s+' + re.escape(attr_name) + r'="[^"]*"', " ", tag, count=1)
            # The tag match ends at the closing '>' (self-closing '/>' or plain
            # '>'); pre-existing-attr stripping may leave a trailing space
            # ('<text ... fill="#111" >'). Always insert the new attributes
            # BEFORE the final '>' — appending after '>' put them into the
            # element's text content and rendered them as visible text.
            if tag.rstrip().endswith("/>"):
                head = tag.rstrip()[:-2].rstrip()
                tail = "/>"
            else:
                head = tag.rstrip()[:-1].rstrip()
                tail = ">"
            return svg[: m.start()] + head + " " + extra_attrs + " " + tail + svg[m.end():]
    return svg


def _patch_icon_inner(svg: str, g_match: re.Match, extra_attrs: str) -> str:
    """Apply animation attributes to the first inner shape of an icon group.

    The icon wrapper <g transform="..." opacity="..."><path d="..."/></g>
    already owns transform/opacity; wrapping the inner path in
    <g {anim_attrs}><path/></g> composes the animation with the wrapper.
    """
    start = g_match.end()
    # locate the inner shape open-tag within this group (next non-close tag)
    inner = re.search(r"<(rect|circle|ellipse|line|path|polygon|polyline|text)(\s[^>]*)?>", svg[start:])
    if not inner:
        return svg
    inner_tag = inner.group(0)
    # inject attributes into the inner shape's opening tag instead of
    # wrapping (same duplicate-attribute safety; keeps text content clean)
    for attr_name in re.findall(r'([A-Za-z_:][\w:.-]*)="', extra_attrs):
        inner_tag = re.sub(r'\s+' + re.escape(attr_name) + r'="[^"]*"', " ", inner_tag, count=1)
    stripped = inner_tag.rstrip()
    self_closing = stripped.endswith("/>")
    head = stripped[:-2].rstrip() if self_closing else stripped[:-1].rstrip()
    injected = head + " " + extra_attrs + ("/>" if self_closing else ">")
    return svg[: start + inner.start()] + injected + svg[start + inner.end():]


def _patch_dash_in_svg(svg: str, element_index: int, dash_attrs: str) -> str:
    """Patch dash attributes using the same plan-index mapping as all other
    animation properties. This also replaces existing hyphenated SVG
    attributes, preventing invalid duplicate ``stroke-dasharray`` fields."""
    return _patch_element_in_svg(svg, element_index, dash_attrs)


def _wrap_scene_with_camera(svg: str, frame: "AnimFrame", total: float,
                            canvas_w: float, canvas_h: float,
                            camera_spec: Dict[str, Any],
                            element_bboxes: List[Tuple[float, float, float, float]]) -> str:
    """Wrap the scene content in a camera-transform <g> at this frame's time."""
    from camera_engine import camera_transform
    # canvas width/height may come from viewBox or explicit attributes
    cw, ch = canvas_w or 1280.0, canvas_h or 720.0
    m = re.search(r'<svg[^>]*viewBox="[\d.\s\-]*"', svg)
    if m:
        vb = m.group(0)
        parts = [x for x in re.findall(r"[\d.\-]+", vb[vb.index('viewBox'):])]
        if len(parts) >= 4:
            cw, ch = float(parts[2]), float(parts[3])
    t = frame.time
    tr = camera_transform(t, total, camera_spec, cw, ch, element_bboxes,
                          seed=frame.seed)
    # place a <g> wrapper around all content after the first '<svg...>' tag
    # and before the closing '</svg>'. The defs group stays un-wrapped so
    # filters/markers are unaffected.
    svg_start = svg.find("<svg")
    _gt = svg.find(">", svg_start)
    start = _gt + 1 if _gt >= 0 else 0
    end = svg.rfind("</svg>")
    if start < end:
        return (svg[:start]
                + f'<g transform="{tr}">'
                + svg[start:end]
                + "</g>"
                + svg[end:])
    return svg


def _element_bboxes_from_plan(
    elements: List[Dict[str, Any]],
    svg: str,
) -> List[Tuple[float, float, float, float]]:
    """Estimate an (x, y, w, h) bbox for each plan element (render order).

    Positions come from the plan element dictionaries (rect/circle/text-like
    fields: x/y/w/h or x/y/r or xy). Used only by the camera layer to choose
    focus targets; a missing element falls back to canvas center so the
    camera motion degrades gracefully instead of crashing.
    """
    boxes: List[Tuple[float, float, float, float]] = []
    for el in elements or []:
        if isinstance(el.get("x"), (int, float)) and isinstance(el.get("y"), (int, float)):
            w = float(el.get("w") or 0.0) or 120.0
            h = float(el.get("h") or 0.0) or 60.0
            boxes.append((float(el["x"]), float(el["y"]), w, h))
        elif isinstance(el.get("xy"), list) and len(el["xy"]) >= 2:
            boxes.append((float(el["xy"][0]), float(el["xy"][1]), 120.0, 60.0))
        else:
            boxes.append((640.0, 360.0, 120.0, 60.0))  # canvas center fallback
    return boxes


def _render_frame_svg(svg: str, animations: List[Dict[str, Any]], frame: AnimFrame) -> str:
    patched = svg
    for anim in animations:
        e_idx = anim["e"]
        p = frame.per_element.get(e_idx, 1.0)
        element = anim.get("_element") or {}
        dash = draw_dash_progress(anim, p, element)
        if dash:
            patched = _patch_dash_in_svg(patched, e_idx, dash)
        attrs = element_attrs_progress(anim, p, element)
        attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        if attr_str:
            patched = _patch_element_in_svg(patched, e_idx, attr_str)
    return patched


def rasterize_svg(svg_text: str, png_path: Path) -> None:
    """Rasterize frames without assuming FFmpeg was built with SVG support."""
    png_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        vw = int(os.environ.get("VIDEO_WIDTH", "1280"))
        vh = int(os.environ.get("VIDEO_HEIGHT", "720"))
        import cairosvg
        cairosvg.svg2png(
            bytestring=svg_text.encode("utf-8"),
            write_to=str(png_path),
            output_width=vw,
            output_height=vh,
        )
        if png_path.exists() and png_path.stat().st_size > 0:
            return
    except Exception as cairo_error:  # noqa: BLE001
        print(f"CairoSVG animation frame fallback: {cairo_error}")
    # The established static renderer detects FFmpeg support and falls back to
    # installed Edge/Chrome. This avoids repeating a guaranteed FFmpeg error.
    from video_engine import svg_to_png
    svg_to_png(svg_text, png_path)


def _CAMERA_ENABLED() -> bool:
    """Resolve camera state from the isolated job runtime when available."""
    try:
        from job_runtime import current_runtime

        runtime = current_runtime()
        if runtime is not None:
            return runtime.camera_enabled
        import os as _os
        return str(_os.environ.get("CAMERA", "1")).strip().lower() not in {
            "0", "false", "no", "off",
        }
    except Exception:  # noqa: BLE001
        return True


def render_animated_segment(
    svg: str,
    elements: List[Dict[str, Any]],
    animations: List[Dict[str, Any]],
    wav_path: Path,
    output_mp4: Path,
    fps: int = ANIM_SCENE_FPS,
    narration: str = "",
    caption_text: str = "",
    camera_spec: Optional[Dict[str, Any]] = None,
    job_seed: int = 0,
) -> Path:
    """Render one scene: animated frame PNGs + narration wav -> segment mp4.

    Frame timing: first animation frame holds until its animation's start,
    transitions are sampled at fps. Audio track is copied to match.
    Animations are narration-aligned (narrate_align_animations) before
    scheduling: pure Python pacing transform, zero tokens.
    """
    from video_engine import audio_duration_seconds

    total = audio_duration_seconds(wav_path)

    # ---- feature B: word-level alignment -------------------------------------
    # words.json is written by the TTS stage's background whisper pass. If it
    # exists, animations that carry a 'w' list are locked to the exact moment
    # their anchor words are spoken.
    _words: Optional[List[Dict[str, Any]]] = None
    if ANIM_ENABLED:
        try:
            from tts_engine import load_word_timestamps
            _words = load_word_timestamps(wav_path.parent)
        except Exception:  # noqa: BLE001
            _words = None
    animations = narrate_align_animations(animations, total, narration, words=_words)

    frames = build_frame_schedule(animations, total, fps)
    if not frames:
        raise RuntimeError("No frames scheduled for animated segment")

    # ---- feature D: camera layer ---------------------------------------------
    # A 2D camera that drifts/zooms across the scene. Zero tokens (spec comes
    # from the planner's optional 'camera' field, ~20 extra tokens/scene).
    camera_spec = camera_spec or {}
    if _CAMERA_ENABLED() and camera_spec:
        for frame in frames:
            frame.seed = job_seed
        element_bboxes = _element_bboxes_from_plan(elements, svg)
    else:
        element_bboxes = None
        if not camera_spec:
            camera_spec = None

    output_mp4 = Path(output_mp4)
    output_mp4.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="simi_anim_") as tmp:
        tmp_path = Path(tmp)
        pngs: List[Path] = []
        seen = {}  # element-progress key -> png path
        for fi, frame in enumerate(frames):
            key = json.dumps(frame.per_element, sort_keys=True)
            # reuse identical frames (static stretches between animations)
            png_name = f"frame_{fi:04d}.png"
            dedup = seen.get(key)
            if dedup is not None:
                pngs.append(dedup)
                continue
            svg_frame = _render_frame_svg(svg, animations, frame)
            if camera_spec:
                # camera transform differs every frame -> unique frames
                from fast_render import VIDEO_WIDTH as _CAM_W, VIDEO_HEIGHT as _CAM_H
                svg_frame = _wrap_scene_with_camera(
                    svg_frame, frame, total, float(_CAM_W), float(_CAM_H),
                    camera_spec, element_bboxes or [],
                )
                key += json.dumps({"cam": frame.time}, sort_keys=True)
            png_file = tmp_path / png_name
            rasterize_svg(svg_frame, png_file)
            pngs.append(png_file)
            seen[key] = png_file

        # ---- encode ---------------------------------------------------------
        # Encode the variable-duration PNG sequence in one FFmpeg process.
        # The old implementation launched one encoder per frame and then a
        # concat process, which made local encoding dominate end-to-end time.
        from fast_render import (
            VIDEO_WIDTH as _VW, VIDEO_HEIGHT as _VH,
            caption_chunks_for_scene, _drawtext_filter, _music_filter,
            _captions_enabled,
        )
        vf = (
            f"fps={fps},scale={_VW}:{_VH}:force_original_aspect_ratio=decrease,"
            f"pad={_VW}:{_VH}:(ow-iw)/2:(oh-ih)/2:white,format=yuv420p"
        )
        chunks = []
        if _captions_enabled() and caption_text:
            chunks = caption_chunks_for_scene(caption_text, total)
            if chunks:
                vf += "," + _drawtext_filter(
                    chunks, textfile_dir=tmp_path / "captions"
                )
        list_file = tmp_path / "list.txt"
        concat_rows: List[str] = []
        for png, frame in zip(pngs, frames):
            safe_path = str(png.resolve()).replace("'", "'\\''")
            concat_rows.extend((f"file '{safe_path}'\n", f"duration {frame.hold_seconds:.9f}\n"))
        # concat demuxer applies the final duration only when the last file is
        # repeated without a duration directive.
        safe_last = str(pngs[-1].resolve()).replace("'", "'\\''")
        concat_rows.append(f"file '{safe_last}'\n")
        list_file.write_text("".join(concat_rows), encoding="utf-8")
        video_only = tmp_path / "video_only.mp4"
        subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast",
             "-crf", "26", "-r", str(fps), "-an", str(video_only)],
            check=True, timeout=120,
        )

        # ---- narration (and optional ducked music bed) -----------------------
        import os as _os
        from fast_render import MUSIC_TRACK_PATH, MUSIC_VOLUME
        use_music = bool(MUSIC_TRACK_PATH) and _os.path.isfile(MUSIC_TRACK_PATH)
        if use_music:
            music_filter = _music_filter(total)
            subprocess.run(
                [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(video_only),
                 "-i", str(wav_path.resolve()),
                 "-i", MUSIC_TRACK_PATH,
                 "-t", f"{total:.6f}",
                 "-filter_complex",
                 f"[1:a][2:a]{music_filter}[mixed]",
                 "-map", "0:v", "-map", "[mixed]",
                 "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "1",
                 "-shortest",
                 str(output_mp4)],
                check=True, timeout=120,
            )
        else:
            subprocess.run(
                [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(video_only),
                 "-i", str(wav_path.resolve()),
                 "-t", f"{total:.6f}",
                 "-filter_complex", "[1:a]loudnorm=I=-16:TP=-1.5:LRA=7[narration]",
                 "-map", "0:v", "-map", "[narration]",
                 "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "1",
                 "-shortest",
                 str(output_mp4)],
                check=True, timeout=120,
            )

    if not output_mp4.exists():
        raise RuntimeError(f"Animated segment not produced: {output_mp4}")
    return output_mp4
