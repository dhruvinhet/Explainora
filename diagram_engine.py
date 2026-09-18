"""
diagram_engine.py — Deterministic diagram engine (feature C).

Renders structured, deterministic diagrams (flowcharts, cycles, comparisons,
hierarchies, timelines, concept maps) as SVG element lists that the existing
renderer (visual_engine.render_visual_plan) can draw, animate
(animated_scene) and rasterize (fast_render) — no change to the render path.

Why deterministic diagrams
--------------------------
LLM-composed SVGs are great for free-form illustrations but unreliable for
structured visuals: node positions drift, labels duplicate, arrows miss their
targets. This engine computes geometry from the *data* (nodes + edges) with a
simple layered auto-layout, so diagrams are always well-formed. Per-job
randomness (shapes, curve weights, palette emphasis) keeps them visually
varied — the layout is data-driven, not a template.

Design notes
------------
- The planner supplies a `diagram` object per scene (nodes/edges + optional
  `kind`). Token cost: one extra block in the visual plan (~80-150 tokens),
  and it REPLACES the element list for that scene — cheaper than a full
  hand-laid-out scene with the same information density.
- Geometry is computed at render time from the element list, so skins and the
  rough-sketch filter apply automatically.
- No LLM call of its own; zero extra Groq cost beyond the planner schema
  field.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

DIAGRAM_TYPES = {"flow", "cycle", "compare", "tree", "timeline", "map"}

# Per-scene geometry budget (canvas-relative). Diagrams share the same
# 1280x720 canvas as free-form scenes.
CANVAS_WIDTH = int(__import__("os").environ.get("VIDEO_WIDTH", "1280"))
CANVAS_HEIGHT = int(__import__("os").environ.get("VIDEO_HEIGHT", "720"))
MARGIN = 70.0
NODE_W = 170.0
NODE_H = 88.0

# Seeded diversity: each job varies node shapes, edge curves, and accent
# placement so two videos of the same topic never render identically.
_NODE_SHAPES = ["rounded", "circle", "hexagon", "notched"]
_EDGE_STYLES = ["straight", "curved", "elbow"]
_LAYOUT_VARIANTS = ["balanced", "top_weighted", "zigzag"]

SKIN_DEFAULT = {"fill": "#e3f2fd", "text": "#111111", "stroke": "#222222",
                "bg": "#ffffff", "accent": "#1976d2"}
SKIN_MAP = {
    "ocean": {"fill": "#b3e0ff", "text": "#0a2e4d", "stroke": "#0d3b66",
              "bg": "#f2faff", "accent": "#0277bd"},
    "forest": {"fill": "#cde8d0", "text": "#14331a", "stroke": "#1f4a28",
               "bg": "#f7fbf5", "accent": "#2e7d32"},
    "sunset": {"fill": "#ffe0b2", "text": "#3e2723", "stroke": "#795548",
               "bg": "#fffaf4", "accent": "#e65100"},
    "mono": {"fill": "#eeeeee", "text": "#111111", "stroke": "#333333",
             "bg": "#ffffff", "accent": "#424242"},
}


def _skin(name: Optional[str]) -> Dict[str, str]:
    n = (name or "default").strip().lower()
    if n != "default" and n in SKIN_MAP:
        return dict(SKIN_MAP[n])
    return dict(SKIN_DEFAULT)


def _rng(job_id: str, kind: str = "diagram") -> random.Random:
    """Deterministic per-job random (varies across jobs, stable within one)."""
    seed = int(hashlib.sha1(f"{job_id}:{kind}".encode()).hexdigest()[:8], 16)
    return random.Random(seed)


# ---------------------------------------------------------------------------
# layout: layered assignment (sugiyama-lite)
# ---------------------------------------------------------------------------

def _assign_layers(nodes: List[Dict[str, Any]],
                   edges: List[Dict[str, Any]]) -> Dict[str, int]:
    """Longest-path layering — keeps the diagram flowing top-to-bottom."""
    ids = [str(n["id"]) for n in nodes]
    out: Dict[str, int] = {i: 0 for i in ids}
    changed = True
    rounds = 0
    while changed and rounds < len(ids):
        changed = False
        rounds += 1
        for e in edges:
            s, t = str(e["from"]), str(e["to"])
            if s in out and t in out and out[t] <= out[s]:
                out[t] = out[s] + 1
                changed = True
    return out


def _barycenter_order(layers: Dict[str, int],
                      edges: List[Dict[str, Any]],
                      rng: random.Random) -> List[str]:
    """Order nodes within each layer: respect edge crossings lightly, with a
    small random shuffle so equal cases render differently per job."""
    by_layer: Dict[int, List[str]] = {}
    for nid, ly in layers.items():
        by_layer.setdefault(ly, []).append(nid)
    for ly in by_layer:
        by_layer[ly].sort(key=lambda x: (sum(layers[str(e["from"])] if str(e["from"]) == x else 0
                                            for e in edges), x))
        # gentle in-layer shuffle preserves crossing-reduced order but varies
        rng.shuffle(by_layer[ly])
    order: List[str] = []
    for ly in sorted(by_layer):
        order.extend(by_layer[ly])
    return order


def layout_nodes(nodes: List[Dict[str, Any]],
                 edges: List[Dict[str, Any]],
                 rng: random.Random,
                 canvas_w: float = CANVAS_WIDTH,
                 canvas_h: float = CANVAS_HEIGHT) -> Dict[str, Tuple[float, float]]:
    """Compute (cx, cy) for every node. Pure geometry, no randomness in the
    core (randomness only in the optional in-layer shuffle above)."""
    if not nodes:
        return {}

    layers = _assign_layers(nodes, edges)
    max_layer = max(layers.values()) if layers else 0
    layer_counts: Dict[int, int] = {}
    for ly in layers.values():
        layer_counts[ly] = layer_counts.get(ly, 0) + 1
    max_count = max(layer_counts.values())

    usable_w = canvas_w - 2 * MARGIN - NODE_W
    usable_h = canvas_h - 2 * MARGIN - NODE_H

    step_x = usable_w / max(max_count - 1, 1) if max_count > 1 else 0.0
    step_y = usable_h / max(max_layer, 1) if max_layer else usable_h / 2.0

    positions: Dict[str, Tuple[float, float]] = {}
    for nid, ly in layers.items():
        peers = [i for i, l in layers.items() if l == ly]
        idx = peers.index(nid)
        # center the layer
        row_w = (len(peers) - 1) * step_x if len(peers) > 1 else 0.0
        row_start = MARGIN + NODE_W / 2 + (usable_w - row_w) / 2
        cx = row_start + idx * step_x
        cy = MARGIN + NODE_H / 2 + ly * step_y
        # nudge for zigzag/weighted variants so layouts aren't rigid grids
        variant = rng.choice(_LAYOUT_VARIANTS)
        if variant == "zigzag" and len(peers) > 1:
            cy += ((idx % 2) * 2 - 1) * 12.0
        elif variant == "top_weighted" and ly == 0:
            cy -= 26.0
        positions[nid] = (cx, cy)
    return positions


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------

def _add_arrowhead(elements: List[Dict[str, Any]],
                     cx: float, cy: float, angle_deg: float,
                     stroke: str, idx: int) -> None:
    """Append a small filled triangle facing the given angle.

    The three corners are computed directly in absolute canvas coordinates
    (tip at the edge endpoint, wings behind it) — no rotation field needed,
    so the renderer's generic rotation handling never touches it.
    """
    size = 9.0
    a = math.radians(angle_deg)
    tip = (cx, cy)
    base = (cx - 2 * size * math.cos(a), cy - 2 * size * math.sin(a))
    perp = a + math.pi / 2
    dx, dy = size * 0.7 * math.cos(perp), size * 0.7 * math.sin(perp)
    pts = (f"{tip[0]:.1f},{tip[1]:.1f} "
           f"{base[0] + dx:.1f},{base[1] + dy:.1f} "
           f"{base[0] - dx:.1f},{base[1] - dy:.1f}")
    elements.append({
        "type": "polygon", "points": pts, "fill": stroke,
        "stroke": "none", "stroke_width": 0, "opacity": 1,
        "rotation": 0, "id": f"diag_{idx + 1}",
    })


def _clip_to_bounds(center: Tuple[float, float],
                     target: Tuple[float, float],
                     half_w: float, half_h: float) -> Tuple[float, float]:
    """Shorten the vector from `center` toward `target` so it ends at the
    rectangle/circle boundary around `center`. Keeps arrowheads outside the
    node label area."""
    cx, cy = center
    tx, ty = target
    dx, dy = tx - cx, ty - cy
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return center
    # parametric t where the ray hits the box |x|=half_w, |y|=half_h
    t = min(half_w / abs(dx) if abs(dx) > 1e-6 else 1e9,
            half_h / abs(dy) if abs(dy) > 1e-6 else 1e9)
    return (cx + dx * t, cy + dy * t)


def _edge_path(src: Tuple[float, float], dst: Tuple[float, float],
               style: str, rng: random.Random,
               src_hw: float = NODE_W / 2 + 6.0,
               src_hh: float = NODE_H / 2 + 6.0,
               dst_hw: float = NODE_W / 2 + 6.0,
               dst_hh: float = NODE_H / 2 + 6.0) -> str:
    """SVG path between node boundaries (not centers), so arrowheads land at
    the node edge instead of on top of labels."""
    x1, y1 = _clip_to_bounds(src, dst, src_hw, src_hh)
    x2, y2 = _clip_to_bounds(dst, src, dst_hw, dst_hh)
    if style == "straight":
        return f"M {x1:.1f} {y1:.1f} L {x2:.1f} {y2:.1f}"
    if style == "curved":
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2 + rng.uniform(-40, 40)
        return f"M {x1:.1f} {y1:.1f} Q {mx:.1f} {my:.1f} {x2:.1f} {y2:.1f}"
    # elbow: vertical then horizontal then vertical
    mid_y = (y1 + y2) / 2
    return (f"M {x1:.1f} {y1:.1f} L {x1:.1f} {mid_y:.1f} "
            f"L {x2:.1f} {mid_y:.1f} L {x2:.1f} {y2:.1f}")


def _node_shape(kind: str, cx: float, cy: float,
                w: float = NODE_W, h: float = NODE_H) -> Tuple[str, float, float]:
    """Return (path_d_or_shape, rx, ry) — rendered element depends on kind."""
    if kind == "circle":
        return "circle", min(w, h) / 2, min(w, h) / 2
    if kind == "hexagon":
        pts = []
        for i in range(6):
            ang = math.radians(60 * i - 90)
            pts.append(f"{cx + (w / 2) * math.cos(ang):.1f},"
                       f"{cy + (h / 2) * math.sin(ang):.1f}")
        return "polygon", " ".join(pts), 0.0
    if kind == "notched":
        # rectangle with a small notch cut on the right edge (process box)
        d = (f"M {cx - w/2:.1f} {cy - h/2:.1f} "
             f"L {cx + w/2 - 14:.1f} {cy - h/2:.1f} "
             f"L {cx + w/2:.1f} {cy:.1f} "
             f"L {cx + w/2 - 14:.1f} {cy + h/2:.1f} "
             f"L {cx - w/2:.1f} {cy + h/2:.1f} Z")
        return "path", d, 0.0
    # rounded (default)
    return "rect", 14.0, 0.0


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def build_diagram_scene(spec: Dict[str, Any],
                        job_id: str = "",
                        narration: str = "",
                        style: Optional[str] = None) -> Dict[str, Any]:
    """Build a deterministic diagram scene compatible with render_visual_plan.

    Returns a plan dict: {canvas, elements, narration, ...} whose elements are
    plain rect/circle/polygon/path/text/arrow entries the existing renderer
    already understands (plus skin + rough-sketch filter compatibility).
    """
    nodes = [n for n in spec.get("nodes", []) if isinstance(n, dict)]
    edges = [e for e in spec.get("edges", []) if isinstance(e, dict)]
    if not nodes:
        return {}

    skin = _skin(style)
    rng = _rng(job_id or narration[:16])

    shape_pick = rng.choice(_NODE_SHAPES)
    edge_style = rng.choice(_EDGE_STYLES)
    # per-job accent variation: root/first-layer nodes get accent fill
    layers = _assign_layers(nodes, edges)
    accent_layers = {0}
    if len(set(layers.values())) <= 1:
        accent_layers = {0}  # single-layer compare → first half accented
    # map-ish / comparison: accent the two halves
    kind = str(spec.get("kind", "flow")).strip().lower()
    if kind == "compare":
        accent_layers = set()  # no layer accent; halves get accents instead

    positions = layout_nodes(nodes, edges, rng)

    elements: List[Dict[str, Any]] = []
    idx = 0

    # node rects + labels
    for n in nodes:
        nid = str(n.get("id", ""))
        label = str(n.get("label", "") or nid)[:60]
        role = str(n.get("role", "")).strip().lower()
        cx, cy = positions.get(nid, (CANVAS_WIDTH / 2, CANVAS_HEIGHT / 2))
        fill = skin["fill"]
        # role-based accent: root, start/end, key
        if layers.get(nid, 0) in accent_layers or role in {"root", "start",
                                                           "key", "output"}:
            fill = skin["accent"]
        if kind == "compare":
            half = "left" if cx < CANVAS_WIDTH / 2 else "right"
            if half == "left" and role in {"group_a", "a", "left"}:
                fill = skin["accent"]
            elif half == "right" and role in {"group_b", "b", "right"}:
                fill = skin["fill"]  # keep contrast between halves

        shape, a1, a2 = _node_shape(shape_pick, cx, cy)
        elem_id = f"diag_{idx}"
        if shape == "circle":
            elements.append({
                "type": "circle", "x": cx, "y": cy, "radius": float(a1),
                "fill": fill, "stroke": skin["stroke"], "stroke_width": 2,
                "opacity": 1, "rotation": 0, "id": elem_id,
            })
        elif shape == "polygon":
            elements.append({
                "type": "polygon", "points": a1, "fill": fill,
                "stroke": skin["stroke"], "stroke_width": 2,
                "opacity": 1, "rotation": 0, "id": elem_id,
            })
        elif shape == "path":
            elements.append({
                "type": "path", "d": a1, "fill": fill,
                "stroke": skin["stroke"], "stroke_width": 2,
                "opacity": 1, "rotation": 0, "id": elem_id,
            })
        else:
            elements.append({
                "type": "rect", "x": cx - NODE_W / 2, "y": cy - NODE_H / 2,
                "width": NODE_W, "height": NODE_H, "rx": float(a1),
                "fill": fill, "stroke": skin["stroke"], "stroke_width": 2,
                "opacity": 1, "rotation": 0, "id": elem_id,
            })
        idx += 1
        # label (2 lines max), optically centered in the node:
        # the text baseline is y, so pull it up by ~36% of font size per line.
        lines = [label[:34], label[34:68]] if len(label) > 34 else [label]
        ty0 = cy - (len(lines) - 1) * 12 + 6
        for li, line in enumerate(lines):
            elements.append({
                "type": "text", "x": cx, "y": ty0 + li * 24,
                "text": line, "fill": skin["text"], "font_size": 18,
                "font_weight": "bold" if li == 0 else "normal",
                "align": "center", "opacity": 1, "rotation": 0,
                "id": f"diag_{idx}",
            })
            idx += 1

    # edges (arrows)
    for e in edges:
        s, t = str(e.get("from")), str(e.get("to"))
        if s not in positions or t not in positions:
            continue
        label = str(e.get("label", "") or "")[:40]
        d = _edge_path(positions[s], positions[t], edge_style, rng)
        elements.append({
            "type": "path", "d": d, "fill": "none",
            "stroke": skin["stroke"], "stroke_width": 2.2,
            "opacity": 1, "rotation": 0, "id": f"diag_{idx}",
        })
        # arrowhead tip lands at the destination node's BOUNDARY (not center),
        # facing along the edge direction.
        dx_, dy_ = (positions[t][0] - positions[s][0],
                    positions[t][1] - positions[s][1])
        ang_ = math.degrees(math.atan2(dy_, dx_))
        tip_ = _clip_to_bounds(positions[t], positions[s],
                               NODE_W / 2 + 4.0, NODE_H / 2 + 4.0)
        _add_arrowhead(elements, tip_[0], tip_[1], ang_, skin["stroke"], idx)
        idx += 1
        if label:
            mx = (positions[s][0] + positions[t][0]) / 2
            my = (positions[s][1] + positions[t][1]) / 2
            elements.append({
                "type": "text", "x": mx, "y": my - 12, "text": label,
                "fill": skin["text"], "font_size": 14,
                "font_weight": "normal", "align": "center",
                "opacity": 1, "rotation": 0, "id": f"diag_{idx}",
            })
            idx += 1

    # cycle/flow: closing arrow hint for cycles
    if kind == "cycle" and len(edges) >= 2:
        ids = [str(n["id"]) for n in nodes]
        if ids and ids[0] in positions and ids[-1] in positions:
            d = _edge_path(positions[ids[-1]], positions[ids[0]],
                           edge_style, rng)
            elements.append({
                "type": "path", "d": d, "fill": "none",
                "stroke": skin["accent"], "stroke_width": 2.2,
                "dash": "6 5", "opacity": 1, "rotation": 0,
                "id": f"diag_{idx}",
            })
            ang = math.degrees(math.atan2(positions[ids[0]][1] - positions[ids[-1]][1],
                                          positions[ids[0]][0] - positions[ids[-1]][0]))
            tip = _clip_to_bounds(positions[ids[0]], positions[ids[-1]],
                                  NODE_W / 2 + 4.0, NODE_H / 2 + 4.0)
            _add_arrowhead(elements, tip[0], tip[1], ang, skin["accent"], idx)
            idx += 1

    plan: Dict[str, Any] = {
        "canvas": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "elements": elements,
        "narration": str(spec.get("narration", narration)),
        "source": "diagram",
        "diagram_kind": kind,
        "layout": {"scale": 1.0, "translate_x": 0.0, "translate_y": 0.0},
    }
    return plan


def sanitize_diagram_spec(raw: Any) -> Dict[str, Any]:
    """Validate a planner-supplied diagram spec. Invalid entries are dropped
    (cheap) instead of triggering a repair call (expensive)."""
    if not isinstance(raw, dict):
        return {}
    nodes, edges = [], []
    for n in raw.get("nodes", []) or []:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        label = n.get("label")
        if not (isinstance(nid, (str, int)) and str(nid).strip()
                and isinstance(label, str) and label.strip()):
            continue
        node = {"id": str(nid), "label": str(label)[:120],
                "role": str(n.get("role", ""))[:24]}
        nodes.append(node)
    seen_ids = {n["id"] for n in nodes}
    for e in raw.get("edges", []) or []:
        if not isinstance(e, dict):
            continue
        f, t = e.get("from"), e.get("to")
        if str(f) in seen_ids and str(t) in seen_ids and str(f) != str(t):
            edge = {"from": str(f), "to": str(t)}
            lab = e.get("label")
            if isinstance(lab, str) and lab.strip():
                edge["label"] = lab[:60]
            edges.append(edge)
    kind = str(raw.get("kind", "flow")).strip().lower()
    if kind not in DIAGRAM_TYPES:
        kind = "flow"
    return {"nodes": nodes, "edges": edges, "kind": kind}


def has_diagram(plan: Dict[str, Any]) -> bool:
    """True when the planner asked for a deterministic diagram scene."""
    spec = plan.get("diagram")
    return isinstance(spec, dict) and bool(spec.get("nodes"))


__all__ = [
    "DIAGRAM_TYPES",
    "build_diagram_scene",
    "has_diagram",
    "sanitize_diagram_spec",
]
