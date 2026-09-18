"""Conservative, topic-agnostic repairs for expressive free-form visual plans.

This module intentionally does not auto-layout or redesign a scene.  It fixes
only objective defects that cannot be an artistic choice: duplicate IDs,
byte-for-byte duplicate elements, unreadably tiny labels, and connector
endpoints when the planner explicitly identifies their semantic endpoints.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple


# 18px at 1280x720 is a publication contract, not an optional style setting.
# A configured value may raise it but cannot lower it. Explicitly declared
# minor annotations are handled separately at 16px by the final gate.
MIN_FONT_SIZE = max(18.0, float(os.environ.get("MIN_VISUAL_FONT_SIZE", "18")))
CANVAS_WIDTH = max(320, int(os.environ.get("VIDEO_WIDTH", "1280")))
CANVAS_HEIGHT = max(180, int(os.environ.get("VIDEO_HEIGHT", "720")))


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _slug(value: Any, fallback: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return (result or fallback)[:80]


def _signature(element: Dict[str, Any]) -> str:
    # Keep id/layer/opacity in the signature: visually identical geometry may
    # be an intentional multi-layer illustration. Only true duplicate records
    # are removed.
    ignored = {"persistent"}
    stable = {key: value for key, value in element.items() if key not in ignored}
    return json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)


def _bounds(element: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    kind = str(element.get("type", "")).lower()
    if kind == "rect":
        x, y = _num(element.get("x")), _num(element.get("y"))
        return x, y, x + max(1, _num(element.get("width"), 1)), y + max(1, _num(element.get("height"), 1))
    if kind == "circle":
        x, y, radius = _num(element.get("x")), _num(element.get("y")), max(1, _num(element.get("radius"), 1))
        return x - radius, y - radius, x + radius, y + radius
    if kind == "ellipse":
        x, y = _num(element.get("x")), _num(element.get("y"))
        rx, ry = max(1, _num(element.get("rx"), 1)), max(1, _num(element.get("ry"), 1))
        return x - rx, y - ry, x + rx, y + ry
    if kind == "icon":
        x, y = _num(element.get("x")), _num(element.get("y"))
        width, height = max(1, _num(element.get("width"), 1)), max(1, _num(element.get("height"), 1))
        return x - width / 2, y - height / 2, x + width / 2, y + height / 2
    if kind in {"line", "arrow"}:
        x1, y1 = _num(element.get("x1")), _num(element.get("y1"))
        x2, y2 = _num(element.get("x2")), _num(element.get("y2"))
        return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
    if kind in {"path", "polygon", "polyline"}:
        source = element.get("d") if kind == "path" else element.get("points")
        numbers = [
            float(value) for value in re.findall(
                r"[-+]?(?:\d*\.\d+|\d+)", str(source or "")
            )
        ]
        if len(numbers) >= 4:
            xs, ys = numbers[0::2], numbers[1::2]
            if xs and ys:
                return min(xs), min(ys), max(xs), max(ys)
    return None


def _is_background(element: Dict[str, Any]) -> bool:
    bounds = _bounds(element)
    if not bounds:
        return False
    area = max(0.0, bounds[2] - bounds[0]) * max(0.0, bounds[3] - bounds[1])
    semantic = _slug(element.get("id"), "")
    named_background = any(token in semantic.split("_") for token in ("bg", "background", "canvas", "field", "pitch"))
    return area >= CANVAS_WIDTH * CANVAS_HEIGHT * 0.55 and (
        int(_num(element.get("layer"), 2)) <= 0 or named_background
    )


def _rgb(value: Any) -> Optional[Tuple[float, float, float]]:
    text = str(value or "").strip().lower()
    named = {
        "white": "#ffffff", "black": "#000000", "yellow": "#ffff00",
        "red": "#ff0000", "green": "#008000", "blue": "#0000ff",
        "navy": "#000080", "gray": "#808080", "grey": "#808080",
    }
    text = named.get(text, text)
    match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", text)
    if not match:
        return None
    raw = match.group(1)
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    return tuple(int(raw[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _luminance(rgb: Tuple[float, float, float]) -> float:
    converted = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in rgb]
    return 0.2126 * converted[0] + 0.7152 * converted[1] + 0.0722 * converted[2]


def _contrast(a: Any, b: Any) -> Optional[float]:
    rgb_a, rgb_b = _rgb(a), _rgb(b)
    if not rgb_a or not rgb_b:
        return None
    light, dark = sorted((_luminance(rgb_a), _luminance(rgb_b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _boundary_points(
    source: Tuple[float, float, float, float],
    target: Tuple[float, float, float, float],
) -> Tuple[float, float, float, float]:
    sx, sy = (source[0] + source[2]) / 2, (source[1] + source[3]) / 2
    tx, ty = (target[0] + target[2]) / 2, (target[1] + target[3]) / 2
    dx, dy = tx - sx, ty - sy
    if not dx and not dy:
        return sx, sy, tx, ty
    sw, sh = source[2] - source[0], source[3] - source[1]
    tw, th = target[2] - target[0], target[3] - target[1]
    source_scale = 1 / max(abs(dx) / max(1, sw / 2), abs(dy) / max(1, sh / 2))
    target_scale = 1 / max(abs(dx) / max(1, tw / 2), abs(dy) / max(1, th / 2))
    return (
        sx + dx * source_scale, sy + dy * source_scale,
        tx - dx * target_scale, ty - dy * target_scale,
    )


def _text_bounds(element: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    if str(element.get("type", "")).lower() != "text":
        return None
    text = str(element.get("text", "")).strip()
    if not text:
        return None
    size = max(MIN_FONT_SIZE, _num(element.get("font_size"), 18))
    max_chars = max(8, int(_num(element.get("__max_chars"), 30)))
    max_lines = max(1, int(_num(element.get("__max_lines"), 12)))
    lines: List[str] = []
    for source_line in text.splitlines() or [text]:
        words = []
        for token in source_line.strip().split():
            if len(token) <= max_chars:
                words.append(token)
            else:
                words.extend(token[index:index + max_chars] for index in range(0, len(token), max_chars))
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if len(candidate) <= max_chars:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    lines = lines[:max_lines] or [text]
    width = max(size, max(len(line) for line in lines) * size * 0.54)
    height = size + max(0, len(lines) - 1) * size * 1.18
    x, y = _num(element.get("x")), _num(element.get("y"))
    align = str(element.get("align", "left")).lower()
    left = x - width / 2 if align == "center" else x - width if align == "right" else x
    return left, y - size, left + width, y - size + height


def _constrain_text_to_named_containers(
    elements: List[Dict[str, Any]], diagnostics: Dict[str, Any]
) -> None:
    """Give text inside its named container renderer-consistent wrap limits."""
    containers = [
        element for element in elements
        if str(element.get("type", "")).lower() == "rect"
        and not _is_background(element) and _bounds(element)
    ]
    constrained, truncated = [], []
    for text in [element for element in elements if str(element.get("type", "")).lower() == "text"]:
        text_root = _semantic_root(text)
        x, y = _num(text.get("x")), _num(text.get("y"))
        candidates = []
        explicit_owner = str(text.get("owner_id") or text.get("label_for") or "")
        explicit_owner_element = next(
            (item for item in elements if str(item.get("id") or "") == explicit_owner),
            None,
        )
        if explicit_owner_element and str(explicit_owner_element.get("type", "")).lower() != "rect":
            # Icon labels and polygon-node labels are adjacent/owned
            # relationships, not rectangle-containment candidates.
            continue
        for container in containers:
            bounds = _bounds(container)
            if not bounds:
                continue
            common = text_root & _semantic_root(container)
            geometrically_inside = (
                bounds[0] + 2 < x < bounds[2] - 2
                and bounds[1] + 2 < y < bounds[3] - 2
            )
            container_root = _semantic_root(container)
            strong_name_match = bool(
                text_root and container_root
                and (text_root <= container_root or container_root <= text_root)
            )
            if not geometrically_inside and not strong_name_match and explicit_owner != str(container.get("id") or ""):
                continue
            size = max(MIN_FONT_SIZE, _num(text.get("font_size"), 18))
            proximity = max(72.0, size * 3.5)
            if not (
                bounds[0] - proximity <= x <= bounds[2] + proximity
                and bounds[1] - proximity <= y <= bounds[3] + proximity
            ):
                continue
            area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
            cx, cy = _center(bounds)
            distance = math.hypot(x - cx, y - cy)
            candidates.append((
                -len(common),
                0 if explicit_owner == str(container.get("id") or "") else 1,
                0 if geometrically_inside else 1,
                distance,
                area,
                container,
                bounds,
            ))
        if not candidates:
            continue
        *_, container, bounds = min(candidates, key=lambda item: item[:5])
        # Persist the inferred relationship so every downstream geometry gate
        # can distinguish intentional containment from unrelated overlap.
        container_id = str(container.get("id") or "")
        if container_id:
            current_owner = next(
                (item for item in containers if str(item.get("id") or "") == explicit_owner),
                None,
            )
            current_overlap = len(text_root & _semantic_root(current_owner)) if current_owner else 0
            selected_overlap = len(text_root & _semantic_root(container))
            if not explicit_owner or selected_overlap > current_overlap:
                text["owner_id"] = container_id
                text["label_for"] = container_id
                if explicit_owner and explicit_owner != container_id:
                    text["__owner_reassigned_from"] = explicit_owner
        size = max(MIN_FONT_SIZE, _num(text.get("font_size"), 18))
        available_width = max(size * 8, bounds[2] - bounds[0] - 20)
        available_height = max(size * 1.18, bounds[3] - bounds[1] - 10)
        max_chars = max(8, min(120, int(available_width / max(1.0, size * 0.54))))
        max_lines = max(1, min(24, int(available_height / max(1.0, size * 1.18))))
        raw_lines = str(text.get("text", "")).splitlines()
        estimated_lines = 0
        for line in raw_lines or [str(text.get("text", ""))]:
            estimated_lines += max(1, math.ceil(max(1, len(line.strip())) / max_chars))
        text["__max_chars"] = max_chars
        text["__max_lines"] = max_lines
        current_bounds = _text_bounds(text)
        outside = not current_bounds or not (
            current_bounds[0] >= bounds[0] + 6
            and current_bounds[1] >= bounds[1] + 4
            and current_bounds[2] <= bounds[2] - 6
            and current_bounds[3] <= bounds[3] - 4
        )
        if outside:
            # Rect coordinates are top-left while text coordinates are an
            # anchor. Models occasionally use the rect's x/y as if it were a
            # center. Semantic ownership makes that ambiguity observable.
            text["align"] = "center"
            text["x"] = (bounds[0] + bounds[2]) / 2
            text["y"] = (bounds[1] + bounds[3]) / 2 + size * 0.34
            text["__container_alignment_repaired"] = True
            diagnostics.setdefault("container_alignment_repaired_ids", []).append(
                str(text.get("id") or "text")[:80]
            )

        # A compact named container may contain a decorative icon and label.
        # Give each a lane instead of letting collision repair eject the label.
        icons = []
        for icon in elements:
            if str(icon.get("type", "")).lower() != "icon":
                continue
            icon_bounds = _bounds(icon)
            if not icon_bounds:
                continue
            cx, cy = _center(icon_bounds)
            # A symbol exactly on a container edge is commonly an external
            # scanner/connector endpoint, not a child to be rearranged into a
            # label lane.
            if bounds[0] + 2 < cx < bounds[2] - 2 and bounds[1] + 2 < cy < bounds[3] - 2:
                icons.append((icon, icon_bounds))
        if len(icons) == 1:
            icon, icon_bounds = icons[0]
            width = bounds[2] - bounds[0]
            icon_width = icon_bounds[2] - icon_bounds[0]
            text_width = (_text_bounds(text) or (0, 0, size * 4, 0))[2] - (_text_bounds(text) or (0, 0, 0, 0))[0]
            if width >= icon_width + min(text_width, width * 0.6) + 24:
                icon["x"] = bounds[0] + 10 + icon_width / 2
                icon["y"] = (bounds[1] + bounds[3]) / 2
                label_left = bounds[0] + 20 + icon_width
                text["x"] = (label_left + bounds[2] - 8) / 2
                text["y"] = (bounds[1] + bounds[3]) / 2 + size * 0.34
                text["align"] = "center"
                text["__container_icon_lane_repaired"] = True
                diagnostics.setdefault("container_icon_lane_repaired_ids", []).append(
                    str(text.get("id") or "text")[:80]
                )
        constrained.append(str(text.get("id") or "text")[:80])
        if estimated_lines > max_lines:
            truncated.append(str(text.get("id") or "text")[:80])
    diagnostics["container_text_constraints_applied"] = len(constrained)
    diagnostics["container_text_constrained_ids"] = constrained
    diagnostics["container_text_truncated_count"] = len(truncated)
    diagnostics["container_text_truncated_ids"] = truncated
    diagnostics["container_alignment_repaired_count"] = len(
        diagnostics.get("container_alignment_repaired_ids", [])
    )
    diagnostics["container_icon_lane_repaired_count"] = len(
        diagnostics.get("container_icon_lane_repaired_ids", [])
    )


def _intersection_ratio(
    first: Tuple[float, float, float, float],
    second: Tuple[float, float, float, float],
) -> float:
    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    intersection = width * height
    first_area = max(1.0, (first[2] - first[0]) * (first[3] - first[1]))
    second_area = max(1.0, (second[2] - second[0]) * (second[3] - second[1]))
    return intersection / min(first_area, second_area)


def _semantic_root(element: Dict[str, Any]) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", str(element.get("id", "")).lower()))
    tokens = {token for token in tokens if not token.isdigit()}
    return tokens - {
        "bg", "background", "backdrop", "frame", "container", "panel",
        "surface", "outline", "border", "perimeter", "base",
        "label", "text", "icon", "box", "shape",
    }


def _remove_persistent_support_overlays(
    elements: List[Dict[str, Any]], diagnostics: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Remove only carried support surfaces that cover unrelated new content."""
    support_tokens = {
        "bg", "background", "backdrop", "frame", "container", "panel", "surface",
        "outline", "border", "perimeter", "base", "field", "rink", "pitch", "court", "canvas",
    }
    environment_tokens = {"field", "rink", "pitch", "court", "canvas", "arena"}
    removed = set()
    for index, element in enumerate(elements):
        if not element.get("persistent") or str(element.get("type", "")).lower() not in {"rect", "circle", "ellipse"}:
            continue
        element_tokens = set(re.findall(r"[a-z0-9]+", str(element.get("id", "")).lower()))
        if not (element_tokens & support_tokens):
            continue
        bounds = _bounds(element)
        if not bounds:
            continue
        root = _semantic_root(element)
        for other_index, other in enumerate(elements):
            if other is element or str(other.get("type", "")).lower() in {"line", "arrow"}:
                continue
            other_bounds = _text_bounds(other) or _bounds(other)
            if not other_bounds:
                continue
            overlap = _intersection_ratio(bounds, other_bounds)
            other_root = _semantic_root(other)
            other_tokens = set(re.findall(r"[a-z0-9]+", str(other.get("id", "")).lower()))
            equivalent_support = bool(
                root and other_root and (root <= other_root or other_root <= root)
                and other_tokens & support_tokens
                and overlap >= 0.65 and other_index < index
            )
            if equivalent_support:
                removed.add(index)
                diagnostics["duplicate_environment_supports_removed"] += 1
                diagnostics["orphan_support_overlay_ids"].append(str(element.get("id", index))[:80])
                break
            if root & other_root:
                continue
            # A carried field/rink/court may legitimately remain as the first
            # environmental base. Other carried panels are stale when they
            # substantially overlap unrelated current-scene content.
            if other.get("persistent"):
                continue
            if other_index >= index and element_tokens & environment_tokens:
                continue
            if overlap >= 0.32:
                removed.add(index)
                diagnostics["orphan_support_overlays_removed"] += 1
                diagnostics["orphan_support_overlay_ids"].append(str(element.get("id", index))[:80])
                break
    return [element for index, element in enumerate(elements) if index not in removed]


def _repair_persistent_outline_fills(
    elements: List[Dict[str, Any]], diagnostics: Dict[str, Any]
) -> None:
    """Make large carried white outlines non-occluding as a final safeguard."""
    outline_tokens = {"outline", "border", "perimeter"}
    near_white = {"white", "#fff", "#ffffff", "#f8fafc", "#f9fafb", "#f7f7f7"}
    for element in elements:
        if str(element.get("type", "")).lower() not in {"rect", "circle", "ellipse", "path", "polygon"}:
            continue
        tokens = set(re.findall(r"[a-z0-9]+", str(element.get("id", "")).lower()))
        bounds = _bounds(element)
        if not bounds or not tokens & outline_tokens:
            continue
        area_ratio = max(0.0, bounds[2] - bounds[0]) * max(0.0, bounds[3] - bounds[1]) / (
            CANVAS_WIDTH * CANVAS_HEIGHT
        )
        if area_ratio >= 0.30 and str(element.get("fill", "")).strip().lower() in near_white:
            element["fill"] = "none"
            diagnostics["outline_fills_removed"] += 1


def _foreground_occlusions(elements: List[Dict[str, Any]]) -> List[str]:
    """Find opaque late shapes covering unrelated earlier foreground."""
    occluding_ids: List[str] = []
    for index, element in enumerate(elements):
        if str(element.get("type", "")).lower() not in {"rect", "circle", "ellipse", "path", "polygon"}:
            continue
        fill = str(element.get("fill", "none")).strip().lower()
        if fill in {"", "none", "transparent"} or _num(element.get("opacity"), 1) < 0.82:
            continue
        bounds = _bounds(element)
        if not bounds:
            continue
        area_ratio = max(0.0, bounds[2] - bounds[0]) * max(0.0, bounds[3] - bounds[1]) / (
            CANVAS_WIDTH * CANVAS_HEIGHT
        )
        if area_ratio < 0.12:
            continue
        # A later opaque shape that owns several later children is a genuine
        # panel/modal/container. Its containment of earlier context can be an
        # intentional focus transition and is not evidence of accidental paint
        # order occlusion.
        contained_later_children = 0
        for child in elements[index + 1:]:
            child_bounds = _text_bounds(child) or _bounds(child)
            if not child_bounds:
                continue
            cx, cy = _center(child_bounds)
            if bounds[0] <= cx <= bounds[2] and bounds[1] <= cy <= bounds[3]:
                contained_later_children += 1
        if contained_later_children >= 2:
            continue
        root = _semantic_root(element)
        for previous in elements[:index]:
            previous_bounds = _text_bounds(previous) or _bounds(previous)
            if previous_bounds and int(_num(previous.get("layer"), 2)) <= 0:
                previous_area = max(0.0, previous_bounds[2] - previous_bounds[0]) * max(
                    0.0, previous_bounds[3] - previous_bounds[1]
                ) / (CANVAS_WIDTH * CANVAS_HEIGHT)
                if previous_area >= 0.45:
                    continue
            if str(previous.get("type", "")).lower() in {"line", "arrow"} and int(_num(previous.get("layer"), 1)) <= 1:
                # Environmental lines may intentionally sit below a field.
                continue
            if not previous_bounds or root & _semantic_root(previous):
                continue
            if _intersection_ratio(bounds, previous_bounds) >= 0.55:
                occluding_ids.append(str(element.get("id", index))[:80])
                break
    return sorted(set(occluding_ids))


def _repair_text_collisions(
    elements: List[Dict[str, Any]], plan: Dict[str, Any], diagnostics: Dict[str, Any]
) -> None:
    texts = [
        element for element in elements
        if str(element.get("type", "")).lower() == "text"
        and _num(element.get("opacity"), 1.0) > 0.01
    ]
    if len(texts) < 2:
        return
    layout = plan.get("layout") if isinstance(plan.get("layout"), dict) else {}
    scale = max(1e-6, _num(layout.get("scale"), 1.0))
    tx, ty = _num(layout.get("translate_x")), _num(layout.get("translate_y"))
    margin = 18.0
    safe = (
        (margin - tx) / scale, (margin - ty) / scale,
        (CANVAS_WIDTH - margin - tx) / scale, (CANVAS_HEIGHT - margin - ty) / scale,
    )

    def collides(first: Dict[str, Any], second: Dict[str, Any]) -> bool:
        # Alternative state symbols commonly share one anchor intentionally.
        if len(str(first.get("text", "")).strip()) <= 2 and len(str(second.get("text", "")).strip()) <= 2:
            return False
        a, b = _text_bounds(first), _text_bounds(second)
        return bool(a and b and _intersection_ratio(a, b) >= 0.12)

    def valid_candidate(target: Dict[str, Any], x: float, y: float) -> bool:
        original_x, original_y = target.get("x"), target.get("y")
        target["x"], target["y"] = x, y
        bounds = _text_bounds(target)
        fits = bool(bounds and bounds[0] >= safe[0] and bounds[1] >= safe[1] and bounds[2] <= safe[2] and bounds[3] <= safe[3])
        clear = fits and not any(other is not target and collides(target, other) for other in texts)
        target["x"], target["y"] = original_x, original_y
        return clear

    max_iterations = max(8, len(texts) * 3)
    for _ in range(max_iterations):
        collision = None
        for first_index, first in enumerate(texts):
            for second in texts[first_index + 1:]:
                if collides(first, second):
                    collision = (first, second)
                    break
            if collision:
                break
        if not collision:
            break
        first, second = collision
        if bool(first.get("persistent")) != bool(second.get("persistent")):
            target = first if first.get("persistent") else second
        else:
            target = second
        x, y = _num(target.get("x")), _num(target.get("y"))
        size = max(MIN_FONT_SIZE, _num(target.get("font_size"), 18))
        bounds = _text_bounds(target)
        width = (bounds[2] - bounds[0]) if bounds else size * 5
        vertical = size * 1.38
        horizontal = max(size * 2.2, width * 0.62)
        offsets = [
            (0, -vertical), (0, vertical), (0, -2 * vertical), (0, 2 * vertical),
            (-horizontal, 0), (horizontal, 0),
            (-horizontal, -vertical), (horizontal, vertical),
            (-horizontal, vertical), (horizontal, -vertical),
        ]
        moved = False
        for dx, dy in offsets:
            if valid_candidate(target, x + dx, y + dy):
                target["x"], target["y"] = x + dx, y + dy
                target["__collision_repaired"] = True
                diagnostics["text_overlaps_repaired"] += 1
                diagnostics["text_overlap_repaired_ids"].append(str(target.get("id", "text"))[:80])
                moved = True
                break
        if not moved:
            break

    unresolved = 0
    for first_index, first in enumerate(texts):
        unresolved += sum(1 for second in texts[first_index + 1:] if collides(first, second))
    diagnostics["unresolved_text_overlap_count"] = unresolved


def _expanded_obstacle_bounds(element: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    bounds = _bounds(element)
    if not bounds:
        return None
    kind = str(element.get("type", "")).lower()
    if kind in {"line", "arrow"}:
        padding = max(8.0, _num(element.get("stroke_width"), 2) * 3.0)
        return bounds[0] - padding, bounds[1] - padding, bounds[2] + padding, bounds[3] + padding
    return bounds


def _connector_crosses_bounds(
    connector: Dict[str, Any], bounds: Tuple[float, float, float, float],
) -> bool:
    """Return whether the actual connector segment crosses a padded box.

    A diagonal line's axis-aligned bounding box can cover a large part of the
    canvas even though the stroke does not.  Treating that box as painted
    geometry produced false collision failures and unnecessary fallbacks.
    """
    x1, y1 = _num(connector.get("x1")), _num(connector.get("y1"))
    x2, y2 = _num(connector.get("x2")), _num(connector.get("y2"))
    padding = max(4.0, _num(connector.get("stroke_width"), 2.0) * 0.75)
    left, top, right, bottom = (
        bounds[0] - padding, bounds[1] - padding,
        bounds[2] + padding, bounds[3] + padding,
    )
    dx, dy = x2 - x1, y2 - y1
    p = (-dx, dx, -dy, dy)
    q = (x1 - left, right - x1, y1 - top, bottom - y1)
    lower, upper = 0.0, 1.0
    for pi, qi in zip(p, q):
        if abs(pi) < 1e-9:
            if qi < 0:
                return False
            continue
        ratio = qi / pi
        if pi < 0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return False
    return True


def _cross_type_collision_pairs(
    elements: List[Dict[str, Any]],
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Find visible text collisions with icons, connectors and unrelated shapes."""
    texts = [
        element for element in elements
        if str(element.get("type", "")).lower() == "text"
        and _num(element.get("opacity"), 1.0) > 0.01
    ]
    obstacles = [
        element for element in elements
        if str(element.get("type", "")).lower() in {
            "icon", "line", "arrow", "circle", "ellipse", "path", "polygon", "polyline",
        } and _num(element.get("opacity"), 1.0) > 0.01
    ]
    pairs = []
    for text in texts:
        text_bounds = _text_bounds(text)
        if not text_bounds:
            continue
        tx, ty = _center(text_bounds)
        text_root = _semantic_root(text)
        for obstacle in obstacles:
            obstacle_bounds = _expanded_obstacle_bounds(obstacle)
            if not obstacle_bounds:
                continue
            kind = str(obstacle.get("type", "")).lower()
            owner = str(text.get("owner_id") or text.get("label_for") or "")
            if (
                owner and owner == str(obstacle.get("id") or "")
                and kind not in {"icon", "line", "arrow"}
            ):
                continue
            text_refs = {
                str(value).strip().upper() for value in text.get("semantic_refs", [])
                if str(value).strip()
            }
            obstacle_refs = {
                str(value).strip().upper() for value in obstacle.get("semantic_refs", [])
                if str(value).strip()
            }
            if (
                kind not in {"icon", "line", "arrow"}
                and text_refs & obstacle_refs
                and any(ref.startswith("E") for ref in text_refs & obstacle_refs)
            ):
                continue
            # A short label intentionally centered inside its own symbol is
            # allowed. Icons and connectors are never text containers.
            if kind in {"circle", "ellipse", "path", "polygon", "polyline"}:
                if obstacle_bounds[0] <= tx <= obstacle_bounds[2] and obstacle_bounds[1] <= ty <= obstacle_bounds[3]:
                    if text_root & _semantic_root(obstacle):
                        continue
            if kind in {"line", "arrow"}:
                collides = _connector_crosses_bounds(obstacle, text_bounds)
            else:
                collides = _intersection_ratio(text_bounds, obstacle_bounds) >= 0.18
            if collides:
                pairs.append((text, obstacle))
    return pairs


def _repair_cross_type_collisions(
    elements: List[Dict[str, Any]], plan: Dict[str, Any], diagnostics: Dict[str, Any]
) -> None:
    """Move labels away from unrelated icons/connectors when a safe slot exists."""
    repaired: List[str] = []
    layout = plan.get("layout") if isinstance(plan.get("layout"), dict) else {}
    scale = max(1e-6, _num(layout.get("scale"), 1.0))
    tx, ty = _num(layout.get("translate_x")), _num(layout.get("translate_y"))
    safe = ((18 - tx) / scale, (18 - ty) / scale,
            (CANVAS_WIDTH - 18 - tx) / scale, (CANVAS_HEIGHT - 18 - ty) / scale)

    blocked_targets: set[int] = set()
    for _ in range(max(12, len(elements) * 3)):
        pairs = [pair for pair in _cross_type_collision_pairs(elements) if id(pair[0]) not in blocked_targets]
        if not pairs:
            break
        target, obstacle = pairs[0]
        x, y = _num(target.get("x")), _num(target.get("y"))
        size = max(MIN_FONT_SIZE, _num(target.get("font_size"), 18))
        bounds = _text_bounds(target)
        width = bounds[2] - bounds[0] if bounds else size * 5
        horizontal = max(size, width * 0.25)
        offsets = []
        for ring in (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.5, 8.0, 10.0, 12.0):
            vertical = size * ring
            sideways = horizontal * ring
            offsets.extend([
                (0, -vertical), (0, vertical), (-sideways, 0), (sideways, 0),
                (-sideways, -vertical), (sideways, -vertical),
                (-sideways, vertical), (sideways, vertical),
            ])
        owner_id = str(target.get("owner_id") or target.get("label_for") or "")
        owner = next((element for element in elements if str(element.get("id") or "") == owner_id), None)
        owner_bounds = _bounds(owner) if owner else None
        moved = False
        if (
            owner_bounds and str(owner.get("type", "")).lower() == "rect"
            and (owner_bounds[2] - owner_bounds[0]) * (owner_bounds[3] - owner_bounds[1])
            <= CANVAS_WIDTH * CANVAS_HEIGHT * 0.10
            and str(obstacle.get("type", "")).lower() in {"line", "arrow"}
        ):
            original_owner = (_num(owner.get("x")), _num(owner.get("y")))
            for dx, dy in offsets:
                owner["x"], owner["y"] = original_owner[0] + dx, original_owner[1] + dy
                target["x"], target["y"] = x + dx, y + dy
                moved_owner = _bounds(owner)
                fits_owner = bool(
                    moved_owner and moved_owner[0] >= safe[0] and moved_owner[1] >= safe[1]
                    and moved_owner[2] <= safe[2] and moved_owner[3] <= safe[3]
                )
                if fits_owner and not any(
                    pair[0] is target for pair in _cross_type_collision_pairs(elements)
                ):
                    owner["__connector_collision_group_repaired"] = True
                    target["__connector_collision_group_repaired"] = True
                    repaired.append(str(target.get("id") or "text")[:80])
                    moved = True
                    break
            if moved:
                continue
            owner["x"], owner["y"] = original_owner
            target["x"], target["y"] = x, y
        # If an unrelated icon has landed inside a text owner's container,
        # preserve the owned label and displace the icon first. This commonly
        # occurs when a moving physical object and its shipping/status label
        # share a final animation coordinate.
        obstacle_bounds = _bounds(obstacle)
        if (
            owner_bounds and obstacle_bounds
            and str(obstacle.get("type", "")).lower() == "icon"
            and not (_semantic_root(target) & _semantic_root(obstacle))
        ):
            ocx, ocy = _center(obstacle_bounds)
            if owner_bounds[0] <= ocx <= owner_bounds[2] and owner_bounds[1] <= ocy <= owner_bounds[3]:
                icon_width = obstacle_bounds[2] - obstacle_bounds[0]
                icon_height = obstacle_bounds[3] - obstacle_bounds[1]
                owner_cx, owner_cy = _center(owner_bounds)
                icon_slots = [
                    (owner_bounds[0] - icon_width / 2 - 12, owner_cy),
                    (owner_bounds[2] + icon_width / 2 + 12, owner_cy),
                    (owner_cx, owner_bounds[1] - icon_height / 2 - 12),
                    (owner_cx, owner_bounds[3] + icon_height / 2 + 12),
                ]
                original_icon = (_num(obstacle.get("x")), _num(obstacle.get("y")))
                for icon_x, icon_y in icon_slots:
                    obstacle["x"], obstacle["y"] = icon_x, icon_y
                    moved_bounds = _bounds(obstacle)
                    fits_icon = bool(
                        moved_bounds and moved_bounds[0] >= safe[0] and moved_bounds[1] >= safe[1]
                        and moved_bounds[2] <= safe[2] and moved_bounds[3] <= safe[3]
                    )
                    if fits_icon and not any(pair[1] is obstacle for pair in _cross_type_collision_pairs(elements)):
                        obstacle["__owner_collision_displaced"] = owner_id
                        repaired.append(str(target.get("id") or "text")[:80])
                        moved = True
                        break
                if moved:
                    continue
                obstacle["x"], obstacle["y"] = original_icon
        for dx, dy in offsets:
            target["x"], target["y"] = x + dx, y + dy
            candidate = _text_bounds(target)
            fits = bool(candidate and candidate[0] >= safe[0] and candidate[1] >= safe[1]
                        and candidate[2] <= safe[2] and candidate[3] <= safe[3])
            owner_kind = str(owner.get("type", "")).lower() if owner else ""
            owner_can_contain_text = bool(
                owner_bounds and owner_kind in {"rect", "circle", "ellipse", "path", "polygon"}
                and owner_bounds[2] - owner_bounds[0] >= max(40.0, width + 8.0)
                and owner_bounds[3] - owner_bounds[1] >= max(size + 6.0, 20.0)
            )
            if fits and owner_can_contain_text:
                fits = bool(
                    candidate[0] >= owner_bounds[0] + 4 and candidate[1] >= owner_bounds[1] + 3
                    and candidate[2] <= owner_bounds[2] - 4 and candidate[3] <= owner_bounds[3] - 3
                )
            other_text_collision = any(
                other is not target and _text_bounds(other)
                and _intersection_ratio(candidate, _text_bounds(other)) >= 0.12
                for other in elements if str(other.get("type", "")).lower() == "text"
            ) if candidate else True
            remaining_cross = any(pair[0] is target for pair in _cross_type_collision_pairs(elements))
            if fits and not other_text_collision and not remaining_cross:
                target["__cross_type_collision_repaired"] = True
                repaired.append(str(target.get("id") or "text")[:80])
                moved = True
                break
        if not moved:
            target["x"], target["y"] = x, y
            blocked_targets.add(id(target))

    unresolved_pairs = _cross_type_collision_pairs(elements)
    suppressed = []
    for text, _obstacle in list(unresolved_pairs):
        owner_id = str(text.get("owner_id") or text.get("label_for") or "")
        owner = next(
            (element for element in elements if str(element.get("id") or "") == owner_id),
            None,
        )
        owner_bounds = _bounds(owner) if owner else None
        owner_area = (
            (owner_bounds[2] - owner_bounds[0]) * (owner_bounds[3] - owner_bounds[1])
            if owner_bounds else 0.0
        )
        if (
            owner and owner_area >= CANVAS_WIDTH * CANVAS_HEIGHT * 0.03
            and _semantic_root(text) & _semantic_root(owner)
            and bool(owner.get("semantic_refs"))
        ):
            text["opacity"] = 0
            text["__redundant_collision_label_suppressed"] = True
            suppressed.append(str(text.get("id") or "label")[:80])
    if suppressed:
        unresolved_pairs = _cross_type_collision_pairs(elements)
    diagnostics["redundant_collision_labels_suppressed"] = len(set(suppressed))
    diagnostics["redundant_collision_label_ids"] = sorted(set(suppressed))
    diagnostics["cross_type_collisions_repaired"] = len(repaired)
    diagnostics["cross_type_collision_repaired_ids"] = repaired
    diagnostics["unresolved_cross_type_collision_count"] = len(unresolved_pairs)
    diagnostics["unresolved_cross_type_collision_ids"] = sorted({
        str(text.get("id") or "text")[:80] for text, _ in unresolved_pairs
    })


def _remove_visible_layout_placeholders(
    elements: List[Dict[str, Any]], spoken_context: str, diagnostics: Dict[str, Any],
) -> List[Dict[str, Any]]:
    context = str(spoken_context or "").casefold()
    positional = {"top left", "top right", "bottom left", "bottom right", "center", "placeholder"}
    structural = {"triangle", "circle", "rectangle", "box", "node", "label", "item"}
    kept = []
    removed = []
    for element in elements:
        if str(element.get("type", "")).lower() != "text":
            kept.append(element)
            continue
        text = " ".join(str(element.get("text", "")).strip().casefold().split())
        is_placeholder = text in positional or (text in structural and text not in context)
        if is_placeholder:
            removed.append(str(element.get("id") or text)[:80])
        else:
            kept.append(element)
    diagnostics["visible_layout_placeholders_removed"] = len(removed)
    diagnostics["visible_layout_placeholder_ids"] = removed
    return kept


def _remove_visible_meta_instructions(
    elements: List[Dict[str, Any]], spoken_context: str, diagnostics: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Remove planning/constraint scaffolding accidentally drawn as content."""
    spoken = " ".join(str(spoken_context or "").casefold().split())
    meta_id_tokens = {"anchor", "constraint", "metadata", "instruction", "placeholder"}
    meta_phrases = {
        "exact visual anchors", "copy these literally", "semantic constraints",
        "internal constraint", "anchor list", "constraint legend",
    }
    kept, removed = [], []
    for index, element in enumerate(elements):
        if str(element.get("type", "")).lower() != "text":
            kept.append(element)
            continue
        text = " ".join(str(element.get("text", "")).strip().casefold().split())
        id_tokens = set(re.findall(r"[a-z0-9]+", str(element.get("id", "")).casefold()))
        phrase_match = any(phrase in text for phrase in meta_phrases)
        metadata_id = bool(id_tokens & meta_id_tokens) and text not in spoken
        if phrase_match or metadata_id:
            removed.append(str(element.get("id") or index)[:80])
        else:
            kept.append(element)
    diagnostics["visible_meta_text_removed"] = len(removed)
    diagnostics["visible_meta_text_ids"] = removed
    return kept


def _center(bounds: Tuple[float, float, float, float]) -> Tuple[float, float]:
    return (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2


def _repair_detached_semantic_labels(
    elements: List[Dict[str, Any]], diagnostics: Dict[str, Any]
) -> None:
    """Attach labels to their named visual component when clearly detached."""
    by_id = {str(element.get("id") or ""): element for element in elements}
    hidden_with_owner = []
    for text in elements:
        if str(text.get("type", "")).lower() != "text":
            continue
        owner_id = str(text.get("owner_id") or text.get("label_for") or "")
        owner = by_id.get(owner_id)
        if owner and _num(owner.get("opacity"), 1.0) <= 0.01:
            text["opacity"] = 0
            text["__hidden_with_owner"] = True
            hidden_with_owner.append(str(text.get("id") or "label")[:80])
    diagnostics["labels_hidden_with_owner"] = len(hidden_with_owner)
    diagnostics["label_ids_hidden_with_owner"] = hidden_with_owner
    drawable = [
        element for element in elements
        if str(element.get("type", "")).lower() in {"rect", "circle", "ellipse", "icon"}
        and _num(element.get("opacity"), 1.0) > 0.01
        and not _is_background(element)
        and _bounds(element)
    ]
    repaired = list(diagnostics.get("detached_label_repaired_ids", []))
    unresolved = []
    for text in [
        element for element in elements
        if str(element.get("type", "")).lower() == "text"
        and _num(element.get("opacity"), 1.0) > 0.01
    ]:
        text_id = str(text.get("id", "")).casefold()
        id_tokens = set(re.findall(r"[a-z0-9]+", text_id)) - {"label", "text", "caption"}
        if not id_tokens or not ({"label", "text", "caption"} & set(re.findall(r"[a-z0-9]+", text_id))):
            continue
        candidates = []
        for shape in drawable:
            shape_tokens = _semantic_root(shape)
            overlap = id_tokens & shape_tokens
            if not overlap:
                continue
            score = len(overlap) / max(1, len(id_tokens | shape_tokens))
            if id_tokens <= shape_tokens or shape_tokens <= id_tokens:
                score += 1.0
            candidates.append((score, shape))
        if not candidates:
            continue
        _, shape = max(candidates, key=lambda item: item[0])
        shape_bounds = _bounds(shape)
        text_bounds = _text_bounds(text)
        if not shape_bounds or not text_bounds:
            continue
        cx, cy = _center(shape_bounds)
        tx, ty = _center(text_bounds)
        shape_height = shape_bounds[3] - shape_bounds[1]
        text_size = _num(text.get("font_size"), 18)
        label_allowance = max(40.0, shape_height * 1.2, text_size * 3.0)
        horizontal_allowance = max(40.0, text_size * 3.0)
        expanded = (
            shape_bounds[0] - horizontal_allowance, shape_bounds[1] - label_allowance,
            shape_bounds[2] + horizontal_allowance, shape_bounds[3] + label_allowance,
        )
        already_attached = expanded[0] <= tx <= expanded[2] and expanded[1] <= ty <= expanded[3]
        if already_attached:
            continue
        distance = math.hypot(tx - cx, ty - cy)
        if distance > max(110.0, (shape_bounds[2] - shape_bounds[0]) * 1.5):
            text["x"] = cx
            if str(shape.get("type", "")).lower() == "icon":
                size = max(MIN_FONT_SIZE, _num(text.get("font_size"), 18))
                below_y = shape_bounds[3] + size * 1.35
                text["y"] = below_y if below_y <= CANVAS_HEIGHT - 18 else shape_bounds[1] - size * 0.35
            else:
                text["y"] = cy + max(6.0, _num(text.get("font_size"), 18) * 0.34)
            text["align"] = "center"
            text["__semantic_label_reattached"] = True
            repaired_id = str(text.get("id") or "label")[:80]
            if repaired_id not in repaired:
                repaired.append(repaired_id)
        else:
            unresolved.append(str(text.get("id") or "label")[:80])
    diagnostics["detached_labels_repaired"] = len(repaired)
    diagnostics["detached_label_repaired_ids"] = repaired
    diagnostics["unresolved_detached_label_ids"] = unresolved
    diagnostics["unresolved_detached_label_count"] = len(unresolved)


def _lower_occluding_shape_layers(
    elements: List[Dict[str, Any]], diagnostics: Dict[str, Any]
) -> None:
    """Put opaque support shapes behind subjects they would otherwise hide."""
    lowered = []
    for index, element in enumerate(elements):
        kind = str(element.get("type", "")).lower()
        if kind not in {"rect", "circle", "ellipse", "path", "polygon"}:
            continue
        fill = str(element.get("fill", "none")).strip().lower()
        if fill in {"", "none", "transparent"} or _num(element.get("opacity"), 1) < 0.82:
            continue
        bounds = _bounds(element)
        if not bounds:
            continue
        current_layer = int(_num(element.get("layer"), 2))
        for previous in elements[:index]:
            # Icon silhouettes are indivisible subjects. Opaque shapes painted
            # over most of an icon are almost always a layering error. Other
            # shape-on-shape overlaps can be legitimate illustrations and are
            # reported by QA instead of being redesigned here.
            if str(previous.get("type", "")).lower() != "icon":
                continue
            previous_bounds = _text_bounds(previous) or _bounds(previous)
            previous_layer = int(_num(previous.get("layer"), 2))
            if not previous_bounds or current_layer < previous_layer:
                continue
            if _semantic_root(element) & _semantic_root(previous):
                continue
            if _intersection_ratio(bounds, previous_bounds) >= 0.60:
                element["layer"] = max(0, previous_layer - 1)
                element["__occlusion_layer_repaired"] = True
                lowered.append(str(element.get("id") or index)[:80])
                break
    diagnostics["occluding_layers_lowered"] = len(set(lowered))
    diagnostics["occluding_layer_repaired_ids"] = sorted(set(lowered))


def _resort_layers(
    elements: List[Dict[str, Any]], plan: Dict[str, Any], diagnostics: Dict[str, Any]
) -> List[Dict[str, Any]]:
    indexed = list(enumerate(elements))
    ordered = sorted(indexed, key=lambda item: (int(_num(item[1].get("layer"), 2)), item[0]))
    old_to_new = {old: new for new, (old, _) in enumerate(ordered)}
    reordered = sum(1 for new, (old, _) in enumerate(ordered) if new != old)
    animations = plan.get("animations")
    if isinstance(animations, list) and reordered:
        for animation in animations:
            if not isinstance(animation, dict):
                continue
            try:
                old = int(animation.get("e", -1))
            except (TypeError, ValueError):
                continue
            if old in old_to_new:
                animation["e"] = old_to_new[old]
    diagnostics["post_repair_layer_reorders"] = reordered
    return [element for _, element in ordered]


def _composition_defects(elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Objective, topic-agnostic checks for empty or weak visual containers."""
    empty, low_density = [], []
    container_role_tokens = {"container", "panel", "card", "modal", "box", "frame"}
    for index, container in enumerate(elements):
        if (
            str(container.get("type", "")).lower() != "rect"
            or _is_background(container)
            or int(_num(container.get("layer"), 2)) <= 0
        ):
            continue
        bounds = _bounds(container)
        if not bounds:
            continue
        id_tokens = set(re.findall(r"[a-z0-9]+", str(container.get("id", "")).casefold()))
        if not id_tokens & container_role_tokens and not bool(container.get("is_container")):
            # Toolbars, dividers, meters, progress tracks and illustration
            # surfaces are valid empty shapes; emptiness alone does not make
            # every large rectangle a broken container.
            continue
        area = max(1.0, (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]))
        if area < CANVAS_WIDTH * CANVAS_HEIGHT * 0.025:
            continue
        child_area = 0.0
        child_count = 0
        for child in elements:
            if child is container or str(child.get("type", "")).lower() in {"line", "arrow"}:
                continue
            child_bounds = _text_bounds(child) or _bounds(child)
            if not child_bounds:
                continue
            cx, cy = _center(child_bounds)
            if bounds[0] <= cx <= bounds[2] and bounds[1] <= cy <= bounds[3]:
                child_count += 1
                child_area += min(
                    area,
                    max(1.0, (child_bounds[2] - child_bounds[0]) * (child_bounds[3] - child_bounds[1])),
                )
        container_id = str(container.get("id") or index)[:80]
        if child_count == 0:
            empty.append(container_id)
        elif area >= CANVAS_WIDTH * CANVAS_HEIGHT * 0.12 and child_area / area < 0.035:
            low_density.append(container_id)
    return {
        "empty_container_ids": empty,
        "empty_container_count": len(empty),
        "low_density_container_ids": low_density,
        "low_density_container_count": len(low_density),
    }


def _repair_inferred_connector_endpoints(
    elements: List[Dict[str, Any]], diagnostics: Dict[str, Any]
) -> None:
    """Snap connectors that terminate inside nodes to node boundaries.

    This uses geometry and ownership only. It fixes the common case where a
    connector is aimed at a node centre and crosses the node's label, without
    requiring topic-specific IDs or another model call.
    """
    shapes = [
        element for element in elements
        if str(element.get("type", "")).lower() in {"rect", "circle", "ellipse", "polygon", "path"}
        and not _is_background(element) and _bounds(element)
    ]

    def owner_at(x: float, y: float) -> Optional[Dict[str, Any]]:
        matches = []
        for shape in shapes:
            bounds = _bounds(shape)
            if bounds and bounds[0] - 1 <= x <= bounds[2] + 1 and bounds[1] - 1 <= y <= bounds[3] + 1:
                area = max(1.0, (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]))
                matches.append((area, shape))
        return min(matches, key=lambda item: item[0])[1] if matches else None

    repaired = []
    for connector in elements:
        if str(connector.get("type", "")).lower() not in {"line", "arrow"}:
            continue
        source = owner_at(_num(connector.get("x1")), _num(connector.get("y1")))
        target = owner_at(_num(connector.get("x2")), _num(connector.get("y2")))
        if not source or not target or source is target:
            continue
        source_bounds, target_bounds = _bounds(source), _bounds(target)
        if not source_bounds or not target_bounds:
            continue
        x1, y1, x2, y2 = _boundary_points(source_bounds, target_bounds)
        original = tuple(_num(connector.get(key)) for key in ("x1", "y1", "x2", "y2"))
        updated = (x1, y1, x2, y2)
        if any(abs(a - b) > 0.5 for a, b in zip(original, updated)):
            connector.update({"x1": x1, "y1": y1, "x2": x2, "y2": y2})
            connector.setdefault("from_id", str(source.get("id") or ""))
            connector.setdefault("to_id", str(target.get("id") or ""))
            repaired.append(str(connector.get("id") or "connector")[:80])
    diagnostics["inferred_connector_endpoints_repaired"] = len(repaired)
    diagnostics["inferred_connector_endpoint_ids"] = repaired


def _repair_connected_node_spacing(
    elements: List[Dict[str, Any]], diagnostics: Dict[str, Any]
) -> None:
    """Create a minimum gap between compact nodes joined by a connector."""
    shapes = [
        element for element in elements
        if str(element.get("type", "")).lower() in {"rect", "circle", "ellipse", "polygon", "polyline"}
        and not _is_background(element) and _bounds(element)
    ]

    def owner_at(x: float, y: float) -> Optional[Dict[str, Any]]:
        matches = []
        for shape in shapes:
            bounds = _bounds(shape)
            if bounds and bounds[0] - 1 <= x <= bounds[2] + 1 and bounds[1] - 1 <= y <= bounds[3] + 1:
                area = max(1.0, (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]))
                if area <= CANVAS_WIDTH * CANVAS_HEIGHT * 0.20:
                    matches.append((area, shape))
        return min(matches, key=lambda item: item[0])[1] if matches else None

    def translate(element: Dict[str, Any], dx: float, dy: float) -> None:
        kind = str(element.get("type", "")).lower()
        if kind in {"rect", "circle", "ellipse", "icon", "text"}:
            element["x"] = _num(element.get("x")) + dx
            element["y"] = _num(element.get("y")) + dy
        elif kind in {"polygon", "polyline"}:
            numbers = [
                float(value) for value in re.findall(
                    r"[-+]?(?:\d*\.\d+|\d+)", str(element.get("points") or "")
                )
            ]
            if len(numbers) >= 4:
                shifted = [value + (dx if index % 2 == 0 else dy) for index, value in enumerate(numbers)]
                element["points"] = " ".join(
                    f"{shifted[index]:g},{shifted[index + 1]:g}"
                    for index in range(0, len(shifted) - 1, 2)
                )

    moved = []
    minimum_gap = 24.0
    for connector in elements:
        if str(connector.get("type", "")).lower() not in {"line", "arrow"}:
            continue
        source = owner_at(_num(connector.get("x1")), _num(connector.get("y1")))
        target = owner_at(_num(connector.get("x2")), _num(connector.get("y2")))
        if not source or not target or source is target:
            continue
        source_bounds, target_bounds = _bounds(source), _bounds(target)
        if not source_bounds or not target_bounds:
            continue
        scx, scy = _center(source_bounds)
        tcx, tcy = _center(target_bounds)
        dx = dy = 0.0
        if abs(tcx - scx) >= abs(tcy - scy):
            if tcx >= scx:
                dx = max(0.0, source_bounds[2] + minimum_gap - target_bounds[0])
                dx = min(dx, max(0.0, CANVAS_WIDTH - 18 - target_bounds[2]))
            else:
                dx = -max(0.0, target_bounds[2] + minimum_gap - source_bounds[0])
                dx = max(dx, min(0.0, 18 - target_bounds[0]))
        elif tcy >= scy:
            dy = max(0.0, source_bounds[3] + minimum_gap - target_bounds[1])
            dy = min(dy, max(0.0, CANVAS_HEIGHT - 18 - target_bounds[3]))
        else:
            dy = -max(0.0, target_bounds[3] + minimum_gap - source_bounds[1])
            dy = max(dy, min(0.0, 18 - target_bounds[1]))
        if abs(dx) <= 0.5 and abs(dy) <= 0.5:
            continue
        target_id = str(target.get("id") or "")
        # Preserve graph attachment while the node moves. Only shift an
        # endpoint whose smallest containing node is this target, so an
        # overlapping source endpoint is not dragged with it.
        for edge in elements:
            if str(edge.get("type", "")).lower() not in {"line", "arrow"}:
                continue
            for x_key, y_key in (("x1", "y1"), ("x2", "y2")):
                if owner_at(_num(edge.get(x_key)), _num(edge.get(y_key))) is target:
                    edge[x_key] = _num(edge.get(x_key)) + dx
                    edge[y_key] = _num(edge.get(y_key)) + dy
        translate(target, dx, dy)
        for child in elements:
            if child is target:
                continue
            if target_id and target_id in {
                str(child.get("owner_id") or ""), str(child.get("label_for") or "")
            }:
                translate(child, dx, dy)
        moved.append(target_id[:80])
    diagnostics["connected_nodes_spaced"] = len(moved)
    diagnostics["connected_node_spacing_ids"] = moved


def _place_icon_labels_adjacent(
    elements: List[Dict[str, Any]], diagnostics: Dict[str, Any]
) -> None:
    """Place owned labels beside icons instead of painting through glyphs."""
    by_id = {str(element.get("id") or ""): element for element in elements}
    repaired = []
    for text in elements:
        if str(text.get("type", "")).lower() != "text":
            continue
        owner_id = str(text.get("owner_id") or text.get("label_for") or "")
        owner = by_id.get(owner_id)
        if not owner or str(owner.get("type", "")).lower() != "icon":
            continue
        text_bounds, icon_bounds = _text_bounds(text), _bounds(owner)
        if not text_bounds or not icon_bounds or _intersection_ratio(text_bounds, icon_bounds) < 0.12:
            continue
        size = max(MIN_FONT_SIZE, _num(text.get("font_size"), 18))
        cx, _ = _center(icon_bounds)
        below_y = icon_bounds[3] + size * 1.35
        above_y = icon_bounds[1] - size * 0.35
        text["x"] = cx
        text["y"] = below_y if below_y <= CANVAS_HEIGHT - 18 else above_y
        text["align"] = "center"
        text["__icon_label_repositioned"] = True
        repaired.append(str(text.get("id") or "label")[:80])
    diagnostics["icon_labels_repositioned"] = len(repaired)
    diagnostics["icon_label_repositioned_ids"] = repaired


def _repair_explicit_vertical_order(
    elements: List[Dict[str, Any]], context: str, diagnostics: Dict[str, Any]
) -> None:
    """Honor explicit higher/near-surface relations without topic templates."""
    text = re.sub(r"\s+", " ", str(context or "")).casefold()
    lower_match = re.search(
        r"(?:from|and|,)\s+([a-z][a-z -]{0,24}?)\s+"
        r"(?:near|at)\s+(?:the\s+)?(?:surface|bottom|base|ground)\b",
        text,
    )
    higher_match = re.search(
        r"(?:to|and|,)\s+([a-z][a-z -]{0,24}?)\s+"
        r"(?:higher(?:\s+up)?|above|at\s+(?:the\s+)?top)\b",
        text,
    )
    if not lower_match or not higher_match:
        return

    def phrase_tokens(value: str) -> set:
        return {
            token for token in re.findall(r"[a-z0-9]+", value)
            if token not in {"the", "a", "an", "layer", "area", "region"}
        }

    def matching_group(phrase: str) -> List[Dict[str, Any]]:
        wanted = phrase_tokens(phrase)
        if not wanted:
            return []
        return [
            element for element in elements
            if wanted <= set(re.findall(
                r"[a-z0-9]+",
                " ".join(str(element.get(key, "")) for key in ("id", "text", "query")).casefold(),
            ))
            and (_bounds(element) or _text_bounds(element))
        ]

    lower_group = matching_group(lower_match.group(1))
    higher_group = matching_group(higher_match.group(1))
    if not lower_group or not higher_group or set(map(id, lower_group)) & set(map(id, higher_group)):
        return

    def group_bounds(group: List[Dict[str, Any]]) -> Tuple[float, float]:
        boxes = [_bounds(element) or _text_bounds(element) for element in group]
        return min(box[1] for box in boxes if box), max(box[3] for box in boxes if box)

    lower_top, lower_bottom = group_bounds(lower_group)
    higher_top, higher_bottom = group_bounds(higher_group)
    if higher_bottom <= lower_top:
        return
    union_top = min(lower_top, higher_top)
    high_height = higher_bottom - higher_top
    low_height = lower_bottom - lower_top
    high_delta = union_top - higher_top
    low_delta = union_top + high_height - lower_top

    def shift(group: List[Dict[str, Any]], delta: float) -> None:
        for element in group:
            kind = str(element.get("type", "")).lower()
            if kind in {"line", "arrow"}:
                if "y1" in element:
                    element["y1"] = _num(element.get("y1")) + delta
                if "y2" in element:
                    element["y2"] = _num(element.get("y2")) + delta
            elif "y" in element:
                element["y"] = _num(element.get("y")) + delta

    shift(higher_group, high_delta)
    shift(lower_group, low_delta)
    diagnostics["explicit_vertical_order_repairs"] = 1
    diagnostics["explicit_vertical_order"] = {
        "higher_phrase": higher_match.group(1).strip(),
        "lower_phrase": lower_match.group(1).strip(),
        "higher_ids": [str(element.get("id", ""))[:80] for element in higher_group],
        "lower_ids": [str(element.get("id", ""))[:80] for element in lower_group],
        "combined_height_preserved": round(high_height + low_height, 3),
    }


def repair_post_continuity_plan(
    plan: Dict[str, Any], spoken_context: str = "",
) -> Dict[str, Any]:
    """Conservative final composition guard after continuity reconciliation."""
    result = copy.deepcopy(plan)
    elements = result.get("elements")
    if not isinstance(elements, list):
        return result
    previous = result.get("_freeform_repair") if isinstance(result.get("_freeform_repair"), dict) else {}
    diagnostics = dict(previous)
    diagnostics.update({
        "post_continuity_applied": True,
        "post_continuity_repairer": "collision_guard_v1",
        "text_overlaps_repaired": 0,
        "text_overlap_repaired_ids": [],
        "unresolved_text_overlap_count": 0,
        "orphan_support_overlays_removed": 0,
        "orphan_support_overlay_ids": [],
        "duplicate_environment_supports_removed": 0,
        "outline_fills_removed": 0,
        "foreground_occlusion_count": 0,
        "foreground_occluding_ids": [],
        "visible_layout_placeholders_removed": 0,
        "visible_layout_placeholder_ids": [],
        "visible_meta_text_removed": 0,
        "visible_meta_text_ids": [],
        "detached_labels_repaired": 0,
        "detached_label_repaired_ids": [],
        "unresolved_detached_label_count": 0,
        "unresolved_detached_label_ids": [],
        "occluding_layers_lowered": 0,
        "occluding_layer_repaired_ids": [],
        "post_repair_layer_reorders": 0,
        "empty_container_count": 0,
        "empty_container_ids": [],
        "low_density_container_count": 0,
        "low_density_container_ids": [],
        "container_text_constraints_applied": 0,
        "container_text_constrained_ids": [],
        "container_text_truncated_count": 0,
        "container_text_truncated_ids": [],
        "cross_type_collisions_repaired": 0,
        "cross_type_collision_repaired_ids": [],
        "unresolved_cross_type_collision_count": 0,
        "unresolved_cross_type_collision_ids": [],
        "explicit_vertical_order_repairs": 0,
    })
    _repair_explicit_vertical_order(elements, spoken_context, diagnostics)
    _repair_persistent_outline_fills(elements, diagnostics)
    elements = _remove_persistent_support_overlays(elements, diagnostics)
    elements = _remove_visible_layout_placeholders(elements, spoken_context, diagnostics)
    elements = _remove_visible_meta_instructions(elements, spoken_context, diagnostics)
    _lower_occluding_shape_layers(elements, diagnostics)
    elements = _resort_layers(elements, result, diagnostics)
    _repair_detached_semantic_labels(elements, diagnostics)
    _constrain_text_to_named_containers(elements, diagnostics)
    _place_icon_labels_adjacent(elements, diagnostics)
    _repair_connected_node_spacing(elements, diagnostics)
    _repair_inferred_connector_endpoints(elements, diagnostics)
    _repair_text_collisions(elements, result, diagnostics)
    _repair_cross_type_collisions(elements, result, diagnostics)
    # Collision resolution can change a label's position.  Re-evaluate the
    # final composition instead of publishing stale pre-repair diagnostics.
    _repair_detached_semantic_labels(elements, diagnostics)
    occluding_ids = _foreground_occlusions(elements)
    diagnostics["foreground_occluding_ids"] = occluding_ids
    diagnostics["foreground_occlusion_count"] = len(occluding_ids)
    diagnostics.update(_composition_defects(elements))
    result["elements"] = elements
    result["_freeform_repair"] = diagnostics
    return result


def repair_freeform_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Return a repaired copy while preserving the authored composition."""
    result = copy.deepcopy(plan)
    raw_elements = result.get("elements")
    if not isinstance(raw_elements, list):
        return result

    diagnostics = {
        "applied": True,
        "repairer": "conservative_v1",
        "input_element_count": len(raw_elements),
        "duplicate_elements_removed": 0,
        "duplicate_ids_renamed": 0,
        "font_sizes_raised": 0,
        "connector_endpoints_repaired": 0,
        "background_layers_repaired": 0,
        "text_contrast_repaired": 0,
        "composition_preserved": True,
    }

    unique: List[Dict[str, Any]] = []
    signatures = set()
    used_ids = set()
    for index, raw in enumerate(raw_elements):
        if not isinstance(raw, dict):
            continue
        element = copy.deepcopy(raw)
        signature = _signature(element)
        if signature in signatures:
            diagnostics["duplicate_elements_removed"] += 1
            continue
        signatures.add(signature)

        base_id = _slug(element.get("id"), f"el_{index}")
        element_id = base_id
        suffix = 2
        while element_id in used_ids:
            element_id = f"{base_id}_{suffix}"
            suffix += 1
        if element_id != base_id:
            diagnostics["duplicate_ids_renamed"] += 1
        element["id"] = element_id
        used_ids.add(element_id)

        if str(element.get("type", "")).lower() == "text":
            size = _num(element.get("font_size"), 18)
            required_size = 16.0 if bool(element.get("minor_annotation")) else MIN_FONT_SIZE
            if size < required_size:
                element["font_size"] = required_size
                diagnostics["font_sizes_raised"] += 1
        if _is_background(element) and int(_num(element.get("layer"), 2)) != 0:
            element["layer"] = 0
            diagnostics["background_layers_repaired"] += 1
        unique.append(element)

    # Contrast is checked against the topmost containing rectangle. Unknown
    # SVG colors are left untouched rather than guessed.
    background_rects = [element for element in unique if str(element.get("type", "")).lower() == "rect"]
    for element in unique:
        if str(element.get("type", "")).lower() != "text":
            continue
        x, y = _num(element.get("x")), _num(element.get("y"))
        containing = []
        for background in background_rects:
            bounds = _bounds(background)
            if bounds and bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]:
                containing.append(background)
        containing.sort(key=lambda candidate: int(_num(candidate.get("layer"), 0)))
        background_fill = containing[-1].get("fill", "#ffffff") if containing else "#ffffff"
        ratio = _contrast(element.get("fill", "#111827"), background_fill)
        if ratio is not None and ratio < 3.0:
            dark_ratio = _contrast("#111827", background_fill) or 0
            light_ratio = _contrast("#ffffff", background_fill) or 0
            element["fill"] = "#111827" if dark_ratio >= light_ratio else "#ffffff"
            diagnostics["text_contrast_repaired"] += 1

    by_id = {str(element.get("id")): element for element in unique}
    for element in unique:
        if str(element.get("type", "")).lower() not in {"line", "arrow"}:
            continue
        source = by_id.get(_slug(element.get("from_id"), ""))
        target = by_id.get(_slug(element.get("to_id"), ""))
        source_bounds = _bounds(source) if source else None
        target_bounds = _bounds(target) if target else None
        if source_bounds and target_bounds:
            x1, y1, x2, y2 = _boundary_points(source_bounds, target_bounds)
            element.update({"x1": x1, "y1": y1, "x2": x2, "y2": y2})
            diagnostics["connector_endpoints_repaired"] += 1

    diagnostics["output_element_count"] = len(unique)
    result["elements"] = unique
    result["source"] = "freeform_elements"
    result["_freeform_repair"] = diagnostics
    return result


__all__ = ["repair_freeform_plan", "repair_post_continuity_plan"]
