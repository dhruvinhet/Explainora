"""Token-free, topic-agnostic art direction for normalized visual plans.

The visual planner remains responsible for meaning and composition.  This
module only supplies the shared graphic language that an individual LLM call
cannot reliably maintain: one icon family, a restrained neutral palette,
stroke hierarchy, typography, depth, and safe treatment of oversized frames.
No coordinates, labels, semantic colours, or element ordering are changed.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any, Dict, Iterable, Tuple


CANVAS_WIDTH = max(320, int(os.environ.get("VIDEO_WIDTH", "1280")))
CANVAS_HEIGHT = max(180, int(os.environ.get("VIDEO_HEIGHT", "720")))

THEMES = {
    "editorial": {
        "background": "#F8FAFC",
        "surface": "#FFFFFF",
        "soft_fill": "#E8F1FF",
        "ink": "#172033",
        "muted_ink": "#526078",
        "accent": "#2563EB",
    },
    "midnight": {
        "background": "#F6F8FC",
        "surface": "#FFFFFF",
        "soft_fill": "#E9EDFF",
        "ink": "#182033",
        "muted_ink": "#59647A",
        "accent": "#4F46E5",
    },
}

ICON_LICENSES = {
    "tabler": "MIT",
    "lucide": "ISC",
    "ph": "MIT",
    "mdi": "Apache-2.0",
}

_DARK_NEUTRALS = {
    "#111", "#111111", "#222", "#222222", "#333", "#333333",
    "black", "#000", "#000000",
}
_WHITE_NEUTRALS = {"white", "#fff", "#ffffff"}
_GENERIC_PALE_FILLS = {"#e3f2fd", "#eaf3ff", "#eeeeee", "#f2faff"}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _color(value: Any) -> str:
    return str(value or "").strip().lower()


def _tokens(value: Any) -> set[str]:
    return set(filter(None, re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split()))


def _bounds(element: Dict[str, Any]) -> Tuple[float, float, float, float] | None:
    kind = str(element.get("type", "")).lower()
    if kind == "rect":
        x, y = _num(element.get("x")), _num(element.get("y"))
        return x, y, x + _num(element.get("width")), y + _num(element.get("height"))
    if kind == "circle":
        x, y, r = _num(element.get("x")), _num(element.get("y")), _num(element.get("radius"))
        return x - r, y - r, x + r, y + r
    if kind == "ellipse":
        x, y = _num(element.get("x")), _num(element.get("y"))
        rx, ry = _num(element.get("rx")), _num(element.get("ry"))
        return x - rx, y - ry, x + rx, y + ry
    if kind == "text":
        x, y = _num(element.get("x")), _num(element.get("y"))
        size = max(8.0, _num(element.get("font_size"), 18))
        width = max(size, len(str(element.get("text", ""))) * size * 0.54)
        align = str(element.get("align", "left")).lower()
        left = x - width / 2 if align == "center" else (x - width if align == "right" else x)
        return left, y - size, left + width, y + size * 0.2
    return None


def _area_ratio(element: Dict[str, Any]) -> float:
    bounds = _bounds(element)
    if not bounds:
        return 0.0
    return max(0.0, bounds[2] - bounds[0]) * max(0.0, bounds[3] - bounds[1]) / (
        CANVAS_WIDTH * CANVAS_HEIGHT
    )


def _distinct_colors(elements: Iterable[Dict[str, Any]]) -> int:
    colors = {
        _color(element.get(field))
        for element in elements
        for field in ("fill", "stroke")
        if _color(element.get(field)) not in {"", "none", "transparent"}
    }
    return len(colors)


def _rgb(value: Any) -> Tuple[int, int, int] | None:
    color = _color(value)
    named = {"white": "#ffffff", "black": "#000000"}
    color = named.get(color, color)
    if re.fullmatch(r"#[0-9a-f]{3}", color):
        color = "#" + "".join(char * 2 for char in color[1:])
    if not re.fullmatch(r"#[0-9a-f]{6}", color):
        return None
    return tuple(int(color[pos:pos + 2], 16) for pos in (1, 3, 5))


def _contrast(first: Any, second: Any) -> float | None:
    def luminance(rgb: Tuple[int, int, int]) -> float:
        channels = []
        for value in rgb:
            channel = value / 255.0
            channels.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
    rgb_a, rgb_b = _rgb(first), _rgb(second)
    if rgb_a is None or rgb_b is None:
        return None
    light, dark = sorted((luminance(rgb_a), luminance(rgb_b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def apply_graphic_style(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a conservative shared visual language without changing layout."""
    if not isinstance(plan, dict) or not isinstance(plan.get("elements"), list):
        return plan
    if os.environ.get("GRAPHICS_STYLE_ENABLED", "1").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return plan

    result = copy.deepcopy(plan)
    elements = result["elements"]
    theme_name = os.environ.get("GRAPHICS_THEME", "editorial").strip().lower()
    theme = THEMES.get(theme_name, THEMES["editorial"])
    if theme_name not in THEMES:
        theme_name = "editorial"
    family = os.environ.get("GRAPHICS_ICON_FAMILY", "mdi").strip().lower() or "mdi"
    font_family = os.environ.get(
        "GRAPHICS_FONT_FAMILY", "Inter, Segoe UI, Arial, sans-serif"
    ).strip()

    diagnostics: Dict[str, Any] = {
        "applied": True,
        "adapter": "generalized_art_direction_v1",
        "theme": theme_name,
        "icon_family": family,
        "icon_license": ICON_LICENSES.get(family, "verify-upstream"),
        "layout_preserved": True,
        "semantic_colors_preserved": True,
        "extra_llm_calls": 0,
        "font_family": font_family,
        "neutral_colors_harmonized": 0,
        "stroke_hierarchy_applied": 0,
        "corners_harmonized": 0,
        "depth_effects_applied": 0,
        "outer_containers_softened": 0,
        "environment_borders_removed": 0,
        "typography_harmonized": 0,
        "arrowheads_harmonized": 0,
        "icon_query_count": 0,
        "icon_resolution_attempts": 0,
        "icon_query_reductions": 0,
        "icons_resolved": 0,
        "icon_monogram_fallbacks": 0,
        "generic_dot_fallbacks": 0,
        "icon_resolutions": [],
        "distinct_colors_before": _distinct_colors(elements),
        "composite_parts_preserved": 0,
    }

    # Filled circles/ellipses are often pieces of one illustration (a cloud,
    # cluster, silhouette, etc.).  Adding a shadow to every nested piece turns
    # the composition into unrelated bubbles. Detect ownership geometrically
    # and through shared semantic references; no topic vocabulary is used.
    composite_parts = set()
    rounded = [
        element for element in elements
        if str(element.get("type", "")).lower() in {"circle", "ellipse"}
    ]
    for child in rounded:
        child_refs = set(child.get("semantic_refs") or [])
        cx, cy = _num(child.get("x"), 0), _num(child.get("y"), 0)
        child_area = _area_ratio(child)
        for parent in rounded:
            if parent is child or _area_ratio(parent) <= child_area:
                continue
            parent_refs = set(parent.get("semantic_refs") or [])
            if not child_refs or not (child_refs & parent_refs):
                continue
            rx = _num(parent.get("radius"), _num(parent.get("rx"), 0))
            ry = _num(parent.get("radius"), _num(parent.get("ry"), 0))
            if rx and ry and abs(cx - _num(parent.get("x"), 0)) <= rx and abs(cy - _num(parent.get("y"), 0)) <= ry:
                composite_parts.add(id(child))
                break

    emphasis_words = {"focus", "main", "primary", "key", "highlight", "result", "output"}
    for element in elements:
        kind = str(element.get("type", "")).lower()
        element_tokens = _tokens(element.get("id"))
        fill = _color(element.get("fill"))
        stroke = _color(element.get("stroke"))

        # Only replace generic defaults. Deliberate red/green/category colours
        # are left exactly as authored because they may carry meaning.
        if fill in _GENERIC_PALE_FILLS:
            element["fill"] = theme["soft_fill"]
            diagnostics["neutral_colors_harmonized"] += 1
        elif fill in _WHITE_NEUTRALS:
            element["fill"] = theme["surface"]
        if stroke in _DARK_NEUTRALS:
            element["stroke"] = theme["ink"]
            diagnostics["neutral_colors_harmonized"] += 1

        if kind == "text":
            element["__font_family"] = font_family
            element["__font_role"] = "display" if _num(element.get("font_size"), 18) >= 30 else "label"
            if element["__font_role"] == "display" and str(element.get("font_weight", "normal")).lower() in {
                "normal", "400", "500",
            }:
                element["font_weight"] = "600"
            if _color(element.get("fill")) in _DARK_NEUTRALS:
                element["fill"] = theme["ink"]
            diagnostics["typography_harmonized"] += 1
            continue

        if kind == "icon":
            element["__icon_family"] = family
            element["__icon_color"] = theme["ink"]
            diagnostics["icon_query_count"] += 1
            continue

        if kind in {"line", "arrow", "polyline"}:
            desired_width = 3.0 if kind == "arrow" else 2.25
            if _num(element.get("stroke_width"), 2) <= 2.25:
                element["stroke_width"] = desired_width
                diagnostics["stroke_hierarchy_applied"] += 1
            element["__linecap"] = "round"
            element["__linejoin"] = "round"
            if kind == "arrow":
                diagnostics["arrowheads_harmonized"] += 1

        if kind == "rect":
            ratio = _area_ratio(element)
            if int(_num(element.get("layer"), 2)) <= 0 and ratio >= 0.25:
                # Environmental fields and canvas backgrounds are filled
                # regions, not bordered UI cards. The renderer already owns
                # the outer canvas edge.
                if _color(element.get("stroke")) not in {"none", "transparent"}:
                    diagnostics["environment_borders_removed"] += 1
                element["stroke"] = "none"
                element["stroke_width"] = 0
                element.pop("__effect", None)
            is_outer = ratio >= 0.62 and (
                int(_num(element.get("layer"), 2)) <= 1
                or bool(element_tokens & {"background", "canvas", "frame", "container", "panel"})
            )
            if is_outer:
                # Keep contextual fills but remove the heavy presentation-box
                # border that makes every scene look like a generic UI card.
                if _color(element.get("stroke")) not in {"none", "transparent"}:
                    element["stroke_width"] = min(1.0, _num(element.get("stroke_width"), 1))
                    element["stroke"] = theme["muted_ink"]
                    element["__stroke_opacity"] = 0.22
                    diagnostics["outer_containers_softened"] += 1
            else:
                height = max(1.0, _num(element.get("height"), 1))
                width = max(1.0, _num(element.get("width"), 1))
                if width >= 42 and height >= 32 and 0.18 <= width / height <= 8:
                    desired_rx = min(18.0, max(10.0, height * 0.14))
                    if _num(element.get("rx"), 8) < desired_rx:
                        element["rx"] = desired_rx
                        diagnostics["corners_harmonized"] += 1
                if 0.008 <= ratio <= 0.28 and _color(element.get("fill")) not in {"none", "transparent"}:
                    element["__effect"] = "focusGlow" if element_tokens & emphasis_words else "softShadow"
                    diagnostics["depth_effects_applied"] += 1

        elif kind in {"circle", "ellipse"}:
            ratio = _area_ratio(element)
            if id(element) in composite_parts:
                element.pop("__effect", None)
                element["__composite_part"] = True
                diagnostics["composite_parts_preserved"] += 1
            elif 0.002 <= ratio <= 0.18 and int(_num(element.get("layer"), 2)) >= 2:
                element["__effect"] = "focusGlow" if element_tokens & emphasis_words else "softShadow"
                diagnostics["depth_effects_applied"] += 1

        if bool(element.get("__beat_focus")):
            element["__effect"] = "focusGlow"

        if kind in {"rect", "circle", "ellipse", "path", "polygon"} and _num(element.get("stroke_width"), 2) <= 2:
            element["stroke_width"] = 2.25
            diagnostics["stroke_hierarchy_applied"] += 1
        if kind in {"path", "polygon", "polyline"}:
            element["__linejoin"] = "round"

    # Styling can change a white authored canvas to the shared light theme
    # after the earlier geometry repair has run. Recheck labels and pale
    # outlined objects against their actual containing surface at this final
    # stage so they cannot disappear into the background.
    background_rects = [
        element for element in elements
        if str(element.get("type", "")).lower() == "rect"
        and _color(element.get("fill")) not in {"", "none", "transparent"}
    ]
    diagnostics["final_contrast_repairs"] = 0
    for element in elements:
        kind = str(element.get("type", "")).lower()
        bounds = _bounds(element)
        if not bounds:
            continue
        center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
        containing = [
            candidate for candidate in background_rects
            if candidate is not element
            and int(_num(candidate.get("layer"), 0)) <= int(_num(element.get("layer"), 2))
            and (candidate_bounds := _bounds(candidate))
            and candidate_bounds[0] <= center[0] <= candidate_bounds[2]
            and candidate_bounds[1] <= center[1] <= candidate_bounds[3]
        ]
        containing.sort(key=lambda candidate: int(_num(candidate.get("layer"), 0)))
        local_background = containing[-1].get("fill") if containing else theme["background"]
        if kind == "text" and (_contrast(element.get("fill"), local_background) or 99) < 4.5:
            dark_ratio = _contrast(theme["ink"], local_background) or 0
            light_ratio = _contrast("#FFFFFF", local_background) or 0
            element["fill"] = theme["ink"] if dark_ratio >= light_ratio else "#FFFFFF"
            diagnostics["final_contrast_repairs"] += 1
        elif kind in {"circle", "ellipse", "rect", "path", "polygon"}:
            if (_contrast(element.get("fill"), local_background) or 99) < 1.25 and (
                _contrast(element.get("stroke"), local_background) or 99
            ) < 1.6:
                element["stroke"] = theme["muted_ink"]
                element["stroke_width"] = max(2.25, _num(element.get("stroke_width"), 2.25))
                diagnostics["final_contrast_repairs"] += 1

    diagnostics["distinct_colors_after"] = _distinct_colors(elements)
    diagnostics["base_background"] = theme["background"]
    result["_graphic_style"] = diagnostics
    return result


__all__ = ["apply_graphic_style", "ICON_LICENSES", "THEMES"]
