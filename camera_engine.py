"""Camera / pan-zoom layer for the Simi-like explainer generator.

Feature D (approved design): a 2.5D "camera" that moves across each scene
while the narration plays. Two surfaces:

1. Animated scenes (SVG rasterized per frame at fps):
   `camera_transform(progress, duration, spec, element_bboxes)` returns an SVG
   transform string applied to a wrapper <g> around the whole scene group.
   The transform is a pure affine map computed in Python — no extra LLM work,
   no re-layout, ~0 tokens, and the existing frame-deduplication cache safely
   key-bypasses it (camera progress changes every frame, so frames are never
   reused — intentional, the motion IS the video).

2. Static scenes (single PNG + ffmpeg loop):
   `zoompan_filter(duration, spec)` returns an ffmpeg zoompan filter spec that
   performs Ken-Burns motion on the rasterized PNG — no re-rasterization,
   essentially free CPU (zoompan is a fast resample).

Camera specs (from the planner's optional `camera` field per scene, ~20 extra
tokens; defaults to kenburns when absent and CAMERA_ENABLED):

  {"type": "push" | "pan" | "kenburns",
   "to_e": <element index or null>,      # optional focus target
   "amount": 0.0..0.35}                  # max zoom factor above 1.0

The transform is ALWAYS computed relative to the same canvas, clamped so the
camera can never show outside the canvas (edge-locked), and never inverted
(zoom >= 1.0). When a focus target bbox is given, the camera drifts toward
it and keeps it centered at peak zoom.
"""
import math
from typing import Any, Dict, List, Optional, Tuple

# Camera is on by default; jobs can disable via CAMERA=0 env var.
import os as _ce_os
CAMERA_ENABLED = str(_ce_os.environ.get("CAMERA", "1")).strip().lower() not in {
    "0", "false", "no", "off",
}


def camera_enabled() -> bool:
    """Resolve camera enablement for the current job, if one is active."""
    from job_runtime import current_runtime

    runtime = current_runtime()
    return runtime.camera_enabled if runtime is not None else CAMERA_ENABLED


def sanitize_camera_spec(spec: Any) -> Optional[Dict[str, Any]]:
    """Validate an optional planner camera spec. Returns None when unusable."""
    if not isinstance(spec, dict):
        return None
    kind = str(spec.get("type", "kenburns")).lower()
    if kind not in {"push", "pan", "kenburns", "zoom"}:
        kind = "push"
    amount = float(spec.get("amount", 0.18))
    amount = max(0.02, min(0.35, amount))
    to_e = spec.get("to_e")
    if to_e is None or not isinstance(to_e, int):
        try:
            to_e = int(to_e) if to_e is not None else None
        except (TypeError, ValueError):
            to_e = None
    return {"type": kind, "to_e": to_e, "amount": amount}


def _edge_lock(zoom: float, cx: float, cy: float,
               cw: float, ch: float) -> Tuple[float, float]:
    """Clamp camera center so the zoomed viewport never shows outside the
    canvas. The zoomed viewport has size (cw/zoom, ch/zoom); the camera center
    must stay inside [viewport/2, canvas-viewport/2]."""
    hw, hh = cw / (2.0 * zoom), ch / (2.0 * zoom)
    return (max(hw, min(cw - hw, cx)),
            max(hh, min(ch - hh, cy)))


def camera_center_at(progress: float, canvas_w: float, canvas_h: float,
                     spec: Dict[str, Any],
                     target_bbox: Optional[Tuple[float, float, float, float]],
                     seed: int = 0) -> Tuple[float, float, float]:
    """Return (cx, cy, zoom) for the given timeline progress in [0, 1].

    Motion curves:
    - push: start centered at zoom 1.0, ease toward peak zoom focused on the
      target bbox (or 55% across, random-ish side if no target).
    - pan: peak zoom in the middle, camera sweeps horizontally, ending near
      the target (or 70% across) at zoom 1.0 — a classic drift-in-then-out.
    - kenburns: slow steady zoom + gentle diagonal drift, ends zoomed.
    """
    amount = spec.get("amount", 0.18)
    kind = spec.get("type", "push")
    p = max(0.0, min(1.0, progress))

    # deterministic pseudo-random offset per seed (different scenes vary)
    off = (seed % 7) / 7.0
    if target_bbox:
        # bbox is (x, y, w, h): center is x + w/2, y + h/2
        tx = target_bbox[0] + target_bbox[2] / 2.0
        ty = target_bbox[1] + target_bbox[3] / 2.0
    else:
        tx = canvas_w * (0.45 + 0.25 * ((off + 0.31) % 1.0))
        ty = canvas_h * (0.42 + 0.22 * ((off + 0.67) % 1.0))

    def ease(t: float) -> float:
        return t * t * (3 - 2 * t)  # smoothstep

    if kind == "push":
        z = 1.0 + amount * ease(p)
        cx = (canvas_w / 2.0) * (1 - ease(p)) + tx * ease(p)
        cy = (canvas_h / 2.0) * (1 - ease(p)) + ty * ease(p)
    elif kind == "pan":
        z = 1.0 + amount * math.sin(math.pi * p)
        start_x = canvas_w * (0.30 + 0.10 * off)
        cx = start_x * (1 - ease(p)) + tx * ease(p)
        cy = (canvas_h / 2.0) * (1 - ease(p)) + ty * ease(p)
    else:  # kenburns / zoom
        z = 1.0 + amount * p
        cx = (canvas_w / 2.0) + (tx - canvas_w / 2.0) * ease(p) * 0.6
        cy = (canvas_h / 2.0) + (ty - canvas_h / 2.0) * ease(p) * 0.6
    cx, cy = _edge_lock(z, cx, cy, canvas_w, canvas_h)
    return cx, cy, z


def camera_transform(progress: float, duration: float,
                     spec: Dict[str, Any], canvas_w: float, canvas_h: float,
                     element_bboxes: Optional[List[Tuple[float, float, float, float]]] = None,
                     seed: int = 0) -> str:
    """SVG transform string for a wrapper <g> at timeline time `progress`
    (seconds) of a `duration`-second scene.

    The transform is 'scale z' around (cx, cy) expressed as:
    translate(cx, cy) scale(z) translate(-cx, -cy)
    which scales about the camera center, keeping it on screen.
    """
    t = progress / max(1e-3, duration)
    bbox = None
    to_e = spec.get("to_e")
    if to_e is not None and element_bboxes and 0 <= to_e < len(element_bboxes):
        bbox = element_bboxes[to_e]
    cx, cy, z = camera_center_at(t, canvas_w, canvas_h, spec, bbox, seed)
    return f"translate({cx:.2f}, {cy:.2f}) scale({z:.4f}) translate({-cx:.2f}, {-cy:.2f})"


# ---------------------------------------------------------------------------
# Static scenes: ffmpeg zoompan Ken-Burns
# ---------------------------------------------------------------------------

def zoompan_filter(duration: float, spec: Dict[str, Any],
                   canvas_w: float = 1280.0, canvas_h: float = 720.0,
                   fps: int = 4) -> Optional[str]:
    """ffmpeg zoompan filter string implementing the same camera motion on a
    single rasterized PNG. Returns None when the spec is disabled/no-op.

    zoompan's zoom expression 'z+dz' integrates per frame; expressions use
    frame-number math to reproduce the smoothstep curves.
    """
    if not spec:
        return None
    amount = float(spec.get("amount", 0.18))
    kind = str(spec.get("type", "push")).lower()
    n = int(round(duration * fps))
    if n < 2:
        return None
    # peak zoom factor and per-frame dz
    peak = 1.0 + amount
    if kind in {"push", "zoom"}:
        dz = (peak - 1.0) / n
        z_expr = f"1+{dz:.6f}*on"
    elif kind == "pan":
        # ramp up in first half, back down in second half
        half = n / 2
        dz_up = (peak - 1.0) / half
        z_expr = f"if(lt(on\\,{half}\\,),1+{dz_up:.6f}*on,1+{dz_up:.6f}*{half}*(1-(on-{half})/{half}))"
    else:
        dz = (peak - 1.0) / n
        z_expr = f"1+{dz:.6f}*on"
    return (
        f"zoompan=z='{z_expr}':d={n}:x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2'"
        f":s={int(canvas_w)}x{int(canvas_h)}:fps={fps}"
    )
