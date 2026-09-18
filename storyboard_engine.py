"""Compile compact semantic storyboards into the existing visual element DSL.

The LLM describes *what* belongs in a scene and how concepts relate.  This
module owns coordinates, sizing, typography and connector geometry.  It has no
topic-specific rules and makes no model calls, so it is fast, deterministic and
safe to use for every subject.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Tuple


CANVAS_WIDTH = max(320, int(os.environ.get("VIDEO_WIDTH", "1280")))
CANVAS_HEIGHT = max(180, int(os.environ.get("VIDEO_HEIGHT", "720")))
MAX_ITEMS = 10
MAX_RELATIONS = 14

LAYOUTS = {
    "flow", "cycle", "comparison", "timeline", "hierarchy", "grid", "focus",
}
KINDS = {"concept", "object", "state", "value", "event", "group"}
REPRESENTATIONS = {"card", "circle", "ellipse", "ring", "icon", "label", "pill", "bar"}

PALETTES = {
    "default": {"fill": "#EAF3FF", "accent": "#2563EB", "text": "#152238", "stroke": "#31506F", "soft": "#F7FAFC"},
    "ocean": {"fill": "#DDF3FF", "accent": "#0284C7", "text": "#0A2E4D", "stroke": "#0D3B66", "soft": "#F2FAFF"},
    "forest": {"fill": "#E1F2E3", "accent": "#2E7D32", "text": "#14331A", "stroke": "#1F4A28", "soft": "#F7FBF5"},
    "sunset": {"fill": "#FFF0DC", "accent": "#E65100", "text": "#3E2723", "stroke": "#795548", "soft": "#FFFAF4"},
    "mono": {"fill": "#EEEEEE", "accent": "#424242", "text": "#111111", "stroke": "#333333", "soft": "#FAFAFA"},
}


def _slug(value: Any, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return (slug or fallback)[:64]


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    clipped = text[:limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    while re.search(r"\b(?:and|or|to|of|in|for|with|by|from|as|the|a|an)$", clipped, re.I):
        clipped = clipped.rsplit(" ", 1)[0].rstrip(" ,;:-")
    return clipped or text[:limit].rstrip(" ,;:-")


def _palette() -> Dict[str, str]:
    name = os.environ.get("STYLE_SKIN", "default").strip().lower()
    return dict(PALETTES.get(name, PALETTES["default"]))


def sanitize_storyboard(raw: Any) -> Dict[str, Any]:
    """Validate a model storyboard without spending tokens on JSON repair."""
    if not isinstance(raw, dict):
        return {}
    layout = str(raw.get("layout", "flow")).strip().lower()
    if layout not in LAYOUTS:
        layout = "flow"

    items: List[Dict[str, Any]] = []
    seen = set()
    for index, item in enumerate(raw.get("items") or []):
        if not isinstance(item, dict):
            continue
        label = _short(item.get("label"), 42)
        if not label:
            continue
        item_id = _slug(item.get("id") or label, f"item_{index + 1}")
        if item_id in seen:
            continue
        seen.add(item_id)
        kind = str(item.get("kind", "concept")).strip().lower()
        if kind not in KINDS:
            kind = "concept"
        representation = str(item.get("representation", "")).strip().lower()
        if representation not in REPRESENTATIONS:
            if kind == "object":
                representation = "icon" if item.get("icon") else "circle"
            elif kind in {"value", "event"}:
                representation = "pill"
            elif kind == "group":
                representation = "ring"
            else:
                representation = "card"
        items.append({
            "id": item_id,
            "label": label,
            "detail": _short(item.get("detail"), 46),
            "kind": kind,
            "group": _short(item.get("group"), 32),
            "icon": _short(item.get("icon"), 32),
            "representation": representation,
            "size": str(item.get("size", "medium")).strip().lower()
                    if str(item.get("size", "medium")).strip().lower() in {"small", "medium", "large"}
                    else "medium",
            "emphasis": bool(item.get("emphasis", False)),
            "semantic_refs": list(dict.fromkeys(
                str(value).strip().upper() for value in item.get("semantic_refs", [])[:20]
                if re.fullmatch(r"[EF]\d{1,2}", str(value).strip(), flags=re.I)
            )) if isinstance(item.get("semantic_refs"), list) else [],
        })
        if len(items) >= MAX_ITEMS:
            break

    relations: List[Dict[str, str]] = []
    for relation in raw.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        source = _slug(relation.get("from"), "")
        target = _slug(relation.get("to"), "")
        if source in seen and target in seen and source != target:
            relations.append({
                "from": source,
                "to": target,
                "label": _short(relation.get("label"), 30),
                "kind": str(relation.get("kind", "arrow")).strip().lower(),
                "semantic_refs": list(dict.fromkeys(
                    str(value).strip().upper()
                    for value in relation.get("semantic_refs", [])[:20]
                    if re.fullmatch(r"[EF]\d{1,2}", str(value).strip(), flags=re.I)
                )) if isinstance(relation.get("semantic_refs"), list) else [],
            })
        if len(relations) >= MAX_RELATIONS:
            break

    if not items:
        return {}
    return {
        "title": _short(raw.get("title"), 64),
        "layout": layout,
        "items": items,
        "relations": relations,
    }


def has_storyboard(plan: Dict[str, Any]) -> bool:
    return isinstance(plan, dict) and bool(sanitize_storyboard(plan.get("storyboard")))


def _flow_positions(count: int, top: float, bottom: float) -> Dict[int, Tuple[float, float]]:
    if count <= 1:
        return {0: (CANVAS_WIDTH / 2, (top + bottom) / 2)}
    if count <= 5:
        step = (CANVAS_WIDTH - 180) / max(1, count - 1)
        return {i: (90 + i * step, (top + bottom) / 2) for i in range(count)}
    cols = math.ceil(count / 2)
    step = (CANVAS_WIDTH - 180) / max(1, cols - 1)
    positions = {}
    for i in range(count):
        row, col = divmod(i, cols)
        actual_col = col if row == 0 else cols - 1 - col
        positions[i] = (90 + actual_col * step, top + (bottom - top) * (0.30 + row * 0.48))
    return positions


def _grid_positions(count: int, top: float, bottom: float) -> Dict[int, Tuple[float, float]]:
    cols = 2 if count <= 4 else 3
    rows = max(1, math.ceil(count / cols))
    cell_w = (CANVAS_WIDTH - 140) / cols
    cell_h = (bottom - top) / rows
    return {
        i: (70 + cell_w * (i % cols + 0.5), top + cell_h * (i // cols + 0.5))
        for i in range(count)
    }


def _positions(storyboard: Dict[str, Any], top: float, bottom: float) -> Dict[int, Tuple[float, float]]:
    items = storyboard["items"]
    count = len(items)
    layout = storyboard["layout"]
    if layout in {"flow", "timeline"}:
        return _flow_positions(count, top, bottom)
    if layout == "grid":
        return _grid_positions(count, top, bottom)
    if layout == "cycle":
        radius_x = min(430, CANVAS_WIDTH * 0.34)
        radius_y = min(220, (bottom - top) * 0.38)
        center_y = (top + bottom) / 2
        return {
            i: (
                CANVAS_WIDTH / 2 + radius_x * math.cos(-math.pi / 2 + 2 * math.pi * i / count),
                center_y + radius_y * math.sin(-math.pi / 2 + 2 * math.pi * i / count),
            ) for i in range(count)
        }
    if layout == "comparison":
        groups = []
        for item in items:
            group = item.get("group")
            if group and group not in groups:
                groups.append(group)
        if len(groups) < 2:
            groups = ["left", "right"]
        by_group = {group: [] for group in groups[:2]}
        for i, item in enumerate(items):
            group = item.get("group")
            target = group if group in by_group else groups[0 if i < math.ceil(count / 2) else 1]
            by_group[target].append(i)
        result = {}
        for col, group in enumerate(groups[:2]):
            indices = by_group[group]
            for row, index in enumerate(indices):
                result[index] = (CANVAS_WIDTH * (0.27 + 0.46 * col), top + (bottom - top) * (row + 1) / (len(indices) + 1))
        return result
    if layout == "focus":
        center_index = next((i for i, item in enumerate(items) if item["emphasis"]), 0)
        result = {center_index: (CANVAS_WIDTH / 2, (top + bottom) / 2)}
        others = [i for i in range(count) if i != center_index]
        for order, index in enumerate(others):
            angle = -math.pi / 2 + 2 * math.pi * order / max(1, len(others))
            result[index] = (CANVAS_WIDTH / 2 + 420 * math.cos(angle), (top + bottom) / 2 + 210 * math.sin(angle))
        return result
    if layout == "hierarchy":
        incoming = {item["id"]: 0 for item in items}
        for rel in storyboard["relations"]:
            incoming[rel["to"]] += 1
        roots = [item["id"] for item in items if incoming[item["id"]] == 0]
        levels = {item_id: 0 for item_id in roots}
        for _ in range(count):
            for rel in storyboard["relations"]:
                if rel["from"] in levels:
                    levels[rel["to"]] = max(levels.get(rel["to"], 0), levels[rel["from"]] + 1)
        by_level: Dict[int, List[int]] = {}
        for i, item in enumerate(items):
            by_level.setdefault(levels.get(item["id"], 0), []).append(i)
        result = {}
        max_level = max(by_level, default=0)
        for level, indices in by_level.items():
            y = top + (bottom - top) * (level + 0.5) / (max_level + 1)
            for col, index in enumerate(indices):
                result[index] = (CANVAS_WIDTH * (col + 1) / (len(indices) + 1), y)
        return result
    return _grid_positions(count, top, bottom)


def _node_size(item: Dict[str, Any], layout: str, count: int) -> Tuple[float, float]:
    """Choose readable geometry from semantic role, never from the topic."""
    rep = item["representation"]
    factor = {"small": 0.82, "medium": 1.0, "large": 1.22}[item["size"]]
    extra_text_height = 24 if len(item["label"]) > 30 and item["detail"] else 0
    if rep in {"circle", "ring"}:
        side = (126 if count <= 6 else 104) * factor
        return side + extra_text_height, side + extra_text_height
    if rep == "ellipse":
        return max(180, min(310, len(item["label"]) * 11)) * factor, 108 * factor
    if rep == "icon":
        return 154 * factor, (132 + extra_text_height) * factor
    if rep == "bar":
        return 230 * factor, 54 * factor
    if rep == "label":
        return 170 * factor, 58 * factor
    if rep == "pill":
        return (190 if layout != "comparison" else 260) * factor, (76 + extra_text_height) * factor
    if layout == "comparison":
        return 330 * factor, ((108 if item["detail"] else 88) + extra_text_height) * factor
    if layout in {"flow", "timeline"} and count > 5:
        return 176 * factor, ((104 if item["detail"] else 86) + extra_text_height) * factor
    return 224 * factor, ((116 if item["detail"] else 96) + extra_text_height) * factor


def _bounds(center: Tuple[float, float], size: Tuple[float, float]) -> Tuple[float, float, float, float]:
    cx, cy = center
    width, height = size
    return cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2


def _resolve_collisions(
    positions: Dict[int, Tuple[float, float]],
    sizes_by_index: Dict[int, Tuple[float, float]],
    top: float,
    bottom: float,
) -> Dict[int, Tuple[float, float]]:
    """Deterministic constraint pass shared by every topic and layout."""
    result = {index: [float(point[0]), float(point[1])] for index, point in positions.items()}
    gap = 20.0
    for _ in range(24):
        changed = False
        indices = sorted(result)
        for offset, i in enumerate(indices):
            for j in indices[offset + 1:]:
                wi, hi = sizes_by_index[i]
                wj, hj = sizes_by_index[j]
                dx, dy = result[j][0] - result[i][0], result[j][1] - result[i][1]
                overlap_x = (wi + wj) / 2 + gap - abs(dx)
                overlap_y = (hi + hj) / 2 + gap - abs(dy)
                if overlap_x <= 0 or overlap_y <= 0:
                    continue
                changed = True
                if overlap_x < overlap_y:
                    direction = 1.0 if dx >= 0 else -1.0
                    shift = overlap_x / 2 + 0.5
                    result[i][0] -= direction * shift
                    result[j][0] += direction * shift
                else:
                    direction = 1.0 if dy >= 0 else -1.0
                    shift = overlap_y / 2 + 0.5
                    result[i][1] -= direction * shift
                    result[j][1] += direction * shift
        for index, point in result.items():
            width, height = sizes_by_index[index]
            point[0] = max(46 + width / 2, min(CANVAS_WIDTH - 46 - width / 2, point[0]))
            point[1] = max(top + height / 2, min(bottom - height / 2, point[1]))
        if not changed:
            break
    return {index: (point[0], point[1]) for index, point in result.items()}


def _edge_points(
    a: Tuple[float, float], b: Tuple[float, float],
    a_size: Tuple[float, float], b_size: Tuple[float, float],
) -> Tuple[float, float, float, float]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if not dx and not dy:
        return a[0], a[1], b[0], b[1]
    # Ray/rectangle intersection gives an accurate boundary anchor even when
    # the two semantic objects use different visual representations.
    a_scale = 1.0 / max(abs(dx) / max(1.0, a_size[0] / 2), abs(dy) / max(1.0, a_size[1] / 2))
    b_scale = 1.0 / max(abs(dx) / max(1.0, b_size[0] / 2), abs(dy) / max(1.0, b_size[1] / 2))
    return (
        a[0] + dx * a_scale, a[1] + dy * a_scale,
        b[0] - dx * b_scale, b[1] - dy * b_scale,
    )


def _point_clear(x: float, y: float, boxes: List[Tuple[float, float, float, float]], padding: float = 12) -> bool:
    return all(not (x1 - padding <= x <= x2 + padding and y1 - padding <= y <= y2 + padding)
               for x1, y1, x2, y2 in boxes)


def _relation_label_position(
    x1: float, y1: float, x2: float, y2: float,
    boxes: List[Tuple[float, float, float, float]],
) -> Tuple[float, float] | None:
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    length = max(1.0, math.hypot(x2 - x1, y2 - y1))
    nx, ny = -(y2 - y1) / length, (x2 - x1) / length
    for offset in (22, -22, 42, -42):
        candidate = (mx + nx * offset, my + ny * offset)
        if _point_clear(candidate[0], candidate[1], boxes, 46):
            return candidate
    return None


def _render_item(
    elements: List[Dict[str, Any]], item: Dict[str, Any],
    center: Tuple[float, float], size: Tuple[float, float], palette: Dict[str, str],
) -> None:
    first_element = len(elements)
    cx, cy = center
    width, height = size
    rep = item["representation"]
    outline = palette["accent"] if item["emphasis"] else palette["stroke"]
    stroke_width = 4 if item["emphasis"] else 2.5
    fill = palette["soft"] if item["emphasis"] else palette["fill"]

    if rep in {"circle", "ring"}:
        elements.append({
            "type": "circle", "id": item["id"], "x": cx, "y": cy,
            "radius": min(width, height) / 2,
            "fill": "none" if rep == "ring" else fill,
            "stroke": outline, "stroke_width": stroke_width, "layer": 2,
        })
        label_y = cy - 18 if len(item["label"]) > 30 and item["detail"] else cy + 7
    elif rep == "ellipse":
        elements.append({
            "type": "ellipse", "id": item["id"], "x": cx, "y": cy,
            "rx": width / 2, "ry": height / 2,
            "fill": fill, "stroke": outline,
            "stroke_width": stroke_width, "layer": 2,
        })
        label_y = cy + 7
    elif rep == "icon" and item["icon"]:
        icon_size = min(82.0, height * 0.62)
        elements.append({
            "type": "icon", "id": item["id"], "query": item["icon"],
            "x": cx, "y": cy - 14, "width": icon_size, "height": icon_size,
            "fill": outline, "stroke": outline, "stroke_width": 1.5, "layer": 2,
        })
        label_y = cy + height / 2 - 3
    elif rep == "label":
        label_y = cy + 7
    else:
        radius = height / 2 if rep == "pill" else (height / 2 if rep == "bar" else 18)
        elements.append({
            "type": "rect", "id": item["id"], "x": cx - width / 2,
            "y": cy - height / 2, "width": width, "height": height,
            "rx": radius, "fill": fill, "stroke": outline,
            "stroke_width": stroke_width, "layer": 2,
        })
        label_y = cy + (6 if not item["detail"] else (-24 if len(item["label"]) > 30 else -7))

    elements.append({
        "type": "text", "id": f"{item['id']}_label", "x": cx, "y": label_y,
        "text": item["label"], "font_size": 24 if len(item["label"]) <= 25 else 20,
        "font_weight": "bold", "align": "center", "fill": palette["text"], "layer": 3,
    })
    if item["detail"] and rep != "icon":
        elements.append({
            "type": "text", "id": f"{item['id']}_detail", "x": cx,
            "y": cy + min(height * 0.31, 31), "text": item["detail"],
            "font_size": 16, "font_weight": "normal", "align": "center",
            "fill": palette["text"], "layer": 3,
        })
    if item.get("semantic_refs"):
        for element in elements[first_element:]:
            element["semantic_refs"] = list(item["semantic_refs"])


def build_storyboard_scene(raw: Any) -> Dict[str, Any]:
    """Return a normalized-shape-compatible plan with deterministic geometry."""
    storyboard = sanitize_storyboard(raw)
    if not storyboard:
        return {}
    palette = _palette()
    title = storyboard["title"]
    top = 112.0 if title else 64.0
    bottom = CANVAS_HEIGHT - 64.0
    positions = _positions(storyboard, top, bottom)
    count = len(storyboard["items"])
    layout = storyboard["layout"]
    sizes = {
        item["id"]: _node_size(item, layout, count)
        for item in storyboard["items"]
    }
    positions = _resolve_collisions(
        positions,
        {i: sizes[item["id"]] for i, item in enumerate(storyboard["items"])},
        top,
        bottom,
    )

    elements: List[Dict[str, Any]] = []
    if title:
        elements.append({"type": "text", "id": "scene_title", "x": CANVAS_WIDTH / 2, "y": 62, "text": title, "font_size": 34, "font_weight": "bold", "align": "center", "fill": palette["text"], "layer": 3})

    index_by_id = {item["id"]: i for i, item in enumerate(storyboard["items"])}
    relations = list(storyboard["relations"])
    if not relations and layout in {"flow", "timeline", "cycle"}:
        for i in range(count - 1):
            relations.append({"from": storyboard["items"][i]["id"], "to": storyboard["items"][i + 1]["id"], "label": "", "kind": "arrow"})
        if layout == "cycle" and count > 2:
            relations.append({"from": storyboard["items"][-1]["id"], "to": storyboard["items"][0]["id"], "label": "", "kind": "arrow"})

    node_boxes = [
        _bounds(positions[i], sizes[item["id"]])
        for i, item in enumerate(storyboard["items"])
    ]
    node_overlap_count = sum(
        1
        for i, (ax1, ay1, ax2, ay2) in enumerate(node_boxes)
        for bx1, by1, bx2, by2 in node_boxes[i + 1:]
        if ax1 < bx2 and ax2 > bx1 and ay1 < by2 and ay2 > by1
    )
    omitted_relation_labels = 0

    # Connectors are emitted first so they stay behind objects and labels.
    for ri, relation in enumerate(relations):
        source_i = index_by_id[relation["from"]]
        target_i = index_by_id[relation["to"]]
        a, b = positions[source_i], positions[target_i]
        x1, y1, x2, y2 = _edge_points(
            a, b, sizes[relation["from"]], sizes[relation["to"]]
        )
        connector_type = "line" if relation["kind"] == "association" else "arrow"
        elements.append({"type": connector_type, "id": f"relation_{relation['from']}_{relation['to']}", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "stroke": palette["accent"], "stroke_width": 3, "fill": "none", "layer": 1, "semantic_refs": list(relation.get("semantic_refs", []))})
        if relation["label"]:
            label_position = _relation_label_position(x1, y1, x2, y2, node_boxes)
            if label_position:
                lx, ly = label_position
                plate_w = max(54, min(180, len(relation["label"]) * 9 + 20))
                elements.append({"type": "rect", "id": f"relation_{ri + 1}_label_plate", "x": lx - plate_w / 2, "y": ly - 18, "width": plate_w, "height": 26, "rx": 10, "fill": "white", "stroke": "none", "stroke_width": 0, "layer": 2})
                elements.append({"type": "text", "id": f"relation_{ri + 1}_label", "x": lx, "y": ly + 3, "text": relation["label"], "font_size": 15, "font_weight": "bold", "align": "center", "fill": palette["text"], "layer": 3})
            else:
                omitted_relation_labels += 1

    for i, item in enumerate(storyboard["items"]):
        _render_item(elements, item, positions[i], sizes[item["id"]], palette)

    representation_counts = {
        rep: sum(item["representation"] == rep for item in storyboard["items"])
        for rep in sorted({item["representation"] for item in storyboard["items"]})
    }

    return {
        "canvas": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "elements": elements,
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
        "source": "semantic_storyboard",
        "storyboard": storyboard,
        "_storyboard": {
            "compiled": True,
            "layout": storyboard["layout"],
            "item_count": len(storyboard["items"]),
            "relation_count": len(relations),
            "coordinate_fields_from_llm": 0,
            "compiler": "deterministic_v2",
            "representation_counts": representation_counts,
            "card_only": set(representation_counts) <= {"card", "pill"},
            "node_overlap_count": node_overlap_count,
            "relation_label_omitted_count": omitted_relation_labels,
        },
    }


__all__ = ["LAYOUTS", "REPRESENTATIONS", "build_storyboard_scene", "has_storyboard", "sanitize_storyboard"]
