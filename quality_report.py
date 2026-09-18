"""Deterministic, token-free quality diagnostics for generated videos.

The report is intentionally built from saved visual plans and rendered files,
not from another LLM call.  Each quality-improvement stage can extend this
schema while existing fields remain backward compatible for comparison runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REPORT_VERSION = "2.1"
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")
VIDEO_WIDTH = max(320, int(os.environ.get("VIDEO_WIDTH", "1280")))
VIDEO_HEIGHT = max(180, int(os.environ.get("VIDEO_HEIGHT", "720")))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _dimensions(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    """Return first-video-stream dimensions without raising report failures."""
    if path is None or not Path(path).is_file():
        return None
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,sample_aspect_ratio,display_aspect_ratio",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            return {"probe_error": result.stderr.strip()[:500]}
        streams = json.loads(result.stdout or "{}").get("streams") or []
        if not streams:
            return {"probe_error": "no video stream"}
        stream = streams[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        return {
            "width": width,
            "height": height,
            "aspect_ratio": round(width / height, 6) if height else None,
            "sample_aspect_ratio": stream.get("sample_aspect_ratio"),
            "display_aspect_ratio": stream.get("display_aspect_ratio"),
        }
    except Exception as exc:  # diagnostics must never fail a video job
        return {"probe_error": str(exc)[:500]}


def _media_duration(path: Optional[Path]) -> Optional[float]:
    if path is None or not Path(path).is_file():
        return None
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            return None
        return round(float((result.stdout or "").strip()), 6)
    except Exception:
        return None


def _svg_dimensions(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None or not Path(path).is_file():
        return None
    try:
        head = Path(path).read_text(encoding="utf-8")[:4096]
        width_match = re.search(r'<svg\b[^>]*\bwidth="([0-9.]+)', head, re.I)
        height_match = re.search(r'<svg\b[^>]*\bheight="([0-9.]+)', head, re.I)
        view_match = re.search(r'<svg\b[^>]*\bviewBox="([^"]+)"', head, re.I)
        width = int(round(float(width_match.group(1)))) if width_match else None
        height = int(round(float(height_match.group(1)))) if height_match else None
        view_box = None
        if view_match:
            values = [float(x) for x in re.findall(r"[-+]?[0-9]*\.?[0-9]+", view_match.group(1))]
            if len(values) >= 4:
                view_box = values[:4]
        return {
            "width": width,
            "height": height,
            "aspect_ratio": round(width / height, 6) if width and height else None,
            "view_box": view_box,
        }
    except Exception as exc:
        return {"parse_error": str(exc)[:500]}


def _scene_id(path: Path) -> int:
    match = re.search(r"scene_(\d+)", str(path))
    return int(match.group(1)) if match else 0


def _plan_paths(job_dir: Path) -> List[Path]:
    found = list((job_dir / "scenes").glob("scene_*.visual_plan.json"))
    found.extend((job_dir / "segments").glob("scene_*/visual_plan.json"))
    return sorted(set(found), key=lambda p: (_scene_id(p), str(p)))


def _related_assets(plan_path: Path) -> Tuple[Optional[Path], Optional[Path]]:
    if plan_path.name == "visual_plan.json":
        return plan_path.with_name("scene.svg"), plan_path.with_name("scene.png")
    stem = plan_path.name.replace(".visual_plan.json", "")
    return plan_path.with_name(stem + ".svg"), plan_path.with_name(stem + ".png")


def _transformed_bounds(plan: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    try:
        # Reuse the same generic bounds implementation as the visual
        # normalizer, so diagnostics measure what the renderer understands.
        from visual_engine import plan_bounds_raw

        bounds = plan_bounds_raw(plan)
    except Exception:
        bounds = None
    if not bounds:
        return None
    layout = plan.get("layout") if isinstance(plan.get("layout"), dict) else {}
    scale = _number(layout.get("scale"), 1.0)
    tx = _number(layout.get("translate_x"), 0.0)
    ty = _number(layout.get("translate_y"), 0.0)
    x1, y1, x2, y2 = bounds
    return x1 * scale + tx, y1 * scale + ty, x2 * scale + tx, y2 * scale + ty


def _transformed_foreground_bounds(plan: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    try:
        from visual_engine import foreground_plan_bounds_raw

        bounds = foreground_plan_bounds_raw(plan)
    except Exception:
        bounds = None
    if not bounds:
        return None
    layout = plan.get("layout") if isinstance(plan.get("layout"), dict) else {}
    scale = _number(layout.get("scale"), 1.0)
    tx = _number(layout.get("translate_x"), 0.0)
    ty = _number(layout.get("translate_y"), 0.0)
    x1, y1, x2, y2 = bounds
    return x1 * scale + tx, y1 * scale + ty, x2 * scale + tx, y2 * scale + ty


def _scene_report(plan_path: Path) -> Dict[str, Any]:
    warnings: List[str] = []
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "scene_id": _scene_id(plan_path),
            "visual_plan_file": str(plan_path),
            "warnings": [f"visual_plan_unreadable: {exc}"],
        }

    canvas = plan.get("canvas") if isinstance(plan.get("canvas"), dict) else {}
    width = int(_number(canvas.get("width"), 0))
    height = int(_number(canvas.get("height"), 0))
    elements = plan.get("elements") if isinstance(plan.get("elements"), list) else []
    bounds = _transformed_bounds(plan)
    foreground_bounds = _transformed_foreground_bounds(plan)
    bounds_dict = None
    utilization = None
    clipped = False
    if bounds and width and height:
        x1, y1, x2, y2 = bounds
        bounds_dict = {
            "x": round(x1, 3),
            "y": round(y1, 3),
            "width": round(max(0.0, x2 - x1), 3),
            "height": round(max(0.0, y2 - y1), 3),
            "right": round(x2, 3),
            "bottom": round(y2, 3),
        }
        utilization = {
            "bounding_box_area_ratio": round(
                max(0.0, x2 - x1) * max(0.0, y2 - y1) / max(1.0, width * height), 6
            ),
            "horizontal_span_ratio": round(max(0.0, x2 - x1) / width, 6),
            "vertical_span_ratio": round(max(0.0, y2 - y1) / height, 6),
        }
        clipping_bounds = foreground_bounds if plan.get("_foreground", {}).get("auto_framed") and foreground_bounds else bounds
        cx1, cy1, cx2, cy2 = clipping_bounds
        clipped = cx1 < -0.5 or cy1 < -0.5 or cx2 > width + 0.5 or cy2 > height + 0.5
        if clipped:
            warnings.append("content_outside_canvas")

    if (width, height) != (VIDEO_WIDTH, VIDEO_HEIGHT):
        warnings.append("visual_canvas_does_not_match_configured_video")

    svg_path, png_path = _related_assets(plan_path)
    svg_info = _svg_dimensions(svg_path)
    png_info = _dimensions(png_path)
    if svg_info and (svg_info.get("width"), svg_info.get("height")) != (VIDEO_WIDTH, VIDEO_HEIGHT):
        warnings.append("svg_dimensions_do_not_match_configured_video")
    if png_info and not png_info.get("probe_error") and (
        png_info.get("width"), png_info.get("height")
    ) != (VIDEO_WIDTH, VIDEO_HEIGHT):
        warnings.append("raster_dimensions_do_not_match_configured_video")

    text_sizes = [
        _number(e.get("font_size")) for e in elements
        if isinstance(e, dict) and str(e.get("type", "")).lower() == "text"
    ]
    layout_scale = _number((plan.get("layout") or {}).get("scale"), 1.0)
    effective_text_sizes = [size * layout_scale for size in text_sizes]
    legibility_meta = plan.get("_legibility") if isinstance(plan.get("_legibility"), dict) else {}
    calculated_below_minimum = [
        str(element.get("id", "text"))[:80]
        for element in elements
        if isinstance(element, dict)
        and str(element.get("type", "")).lower() == "text"
        and _number(element.get("font_size"), 18) * layout_scale < 18.0
        and not (
            bool(element.get("minor_annotation"))
            and _number(element.get("font_size"), 18) * layout_scale >= 16.0
        )
    ]
    below_minimum_ids = list(
        legibility_meta.get("text_below_minimum_ids", calculated_below_minimum)
    )
    rendered_legibility = {
        "layout_scale": round(layout_scale, 6),
        "minimum_effective_font_size": round(min(effective_text_sizes), 3) if effective_text_sizes else None,
        "maximum_effective_font_size": round(max(effective_text_sizes), 3) if effective_text_sizes else None,
        "text_below_14px_ids": list(legibility_meta.get("text_below_14px_ids", [])),
        "minimum_required_font_size": 18.0,
        "text_below_minimum_ids": below_minimum_ids,
        "pass": not below_minimum_ids,
    }
    continuity = plan.get("_continuity") if isinstance(plan.get("_continuity"), dict) else {
        "declared_mode": "unknown",
        "group": None,
        "base_scene_id": None,
        "applied": False,
        "reused_element_count": 0,
        "carried_element_count": 0,
        "reused_ids": [],
        "carried_ids": [],
        "duplicate_carries_suppressed": [],
        "orphan_support_carries_suppressed": [],
        "reason": "legacy_plan_without_continuity_diagnostics",
    }
    storyboard = plan.get("_storyboard") if isinstance(plan.get("_storyboard"), dict) else {
        "compiled": False,
        "layout": None,
        "item_count": 0,
        "relation_count": 0,
        "coordinate_fields_from_llm": None,
        "compiler": None,
        "reason": "legacy_or_fallback_plan_without_storyboard_diagnostics",
    }
    freeform_repair = plan.get("_freeform_repair") if isinstance(plan.get("_freeform_repair"), dict) else {
        "applied": False,
        "repairer": None,
        "composition_preserved": None,
        "reason": "not_a_repaired_freeform_plan",
    }
    graphic_style = plan.get("_graphic_style") if isinstance(plan.get("_graphic_style"), dict) else {
        "applied": False,
        "adapter": None,
        "layout_preserved": None,
        "reason": "legacy_plan_without_graphic_style_diagnostics",
    }
    foreground_meta = plan.get("_foreground") if isinstance(plan.get("_foreground"), dict) else {}
    foreground_report = None
    if foreground_bounds and width and height:
        fx1, fy1, fx2, fy2 = foreground_bounds
        fw, fh = max(0.0, fx2 - fx1), max(0.0, fy2 - fy1)
        center_x, center_y = (fx1 + fx2) / 2, (fy1 + fy2) / 2
        foreground_report = {
            "x": round(fx1, 3), "y": round(fy1, 3),
            "width": round(fw, 3), "height": round(fh, 3),
            "area_ratio": round(fw * fh / max(1.0, width * height), 6),
            "horizontal_span_ratio": round(fw / width, 6),
            "vertical_span_ratio": round(fh / height, 6),
            "center_offset_x_ratio": round(abs(center_x - width / 2) / width, 6),
            "center_offset_y_ratio": round(abs(center_y - height / 2) / height, 6),
            "auto_framed": bool(foreground_meta.get("auto_framed")),
            "scale_boost": foreground_meta.get("scale_boost", 1.0),
        }
    canonicalization = (
        plan.get("_canonicalization")
        if isinstance(plan.get("_canonicalization"), dict) else {}
    )
    quality_gate = (
        plan.get("_quality_gate") if isinstance(plan.get("_quality_gate"), dict) else {}
    )
    selection = (
        plan.get("_candidate_selection")
        if isinstance(plan.get("_candidate_selection"), dict) else {}
    )
    raw_defects = []
    if canonicalization.get("translated_fields"):
        raw_defects.append("raw_schema_aliases_required_translation")
    if canonicalization.get("unknown_fields"):
        raw_defects.append("raw_unknown_fields_preserved")
    repaired_defects = []
    if freeform_repair.get("applied"):
        repaired_defects.append("local_layout_repair_applied")
    if str(plan.get("source")) == "semantic_fallback":
        repaired_defects.append("rich_candidate_replaced_by_semantic_fallback")
    if plan.get("_render_salvage"):
        repaired_defects.append("single_render_element_isolated")
    unresolved = list(quality_gate.get("blocking_issues", []) or [])
    if clipped:
        unresolved.append("content_outside_canvas")
    if rendered_legibility["pass"] is False:
        unresolved.append("text_below_18px")
    defect_attribution = {
        "raw_llm_defect": raw_defects,
        "local_transformation_defect": [],
        "repaired_defect": repaired_defects,
        "published_unresolved_defect": sorted(set(str(value) for value in unresolved)),
        "false_positive_diagnostic": [],
        "final_candidate_selection": selection,
    }
    return {
        "scene_id": _scene_id(plan_path),
        "visual_plan_file": str(plan_path),
        "canvas": {
            "width": width,
            "height": height,
            "aspect_ratio": round(width / height, 6) if height else None,
            "matches_configured_video": (width, height) == (VIDEO_WIDTH, VIDEO_HEIGHT),
        },
        "svg": svg_info,
        "raster": png_info,
        "layout": plan.get("layout") or {},
        "content_bounds_after_layout": bounds_dict,
        "canvas_utilization": utilization,
        "foreground_composition": foreground_report,
        "element_count": len(elements),
        "element_types": {
            kind: sum(
                1 for e in elements
                if isinstance(e, dict) and str(e.get("type", "")).lower() == kind
            )
            for kind in sorted({
                str(e.get("type", "")).lower() for e in elements if isinstance(e, dict)
            })
            if kind
        },
        "font_size": {
            "minimum": round(min(text_sizes), 3) if text_sizes else None,
            "maximum": round(max(text_sizes), 3) if text_sizes else None,
            "average": round(sum(text_sizes) / len(text_sizes), 3) if text_sizes else None,
        },
        "rendered_legibility": rendered_legibility,
        "visual_source": str(plan.get("source", "legacy_elements")),
        "semantic_storyboard": storyboard,
        "freeform_repair": freeform_repair,
        "graphic_style": graphic_style,
        "pre_render_quality_gate": plan.get("_quality_gate")
        if isinstance(plan.get("_quality_gate"), dict) else {},
        "beat_synchronization": plan.get("_beat_sync")
        if isinstance(plan.get("_beat_sync"), dict) else {},
        "runtime_config": plan.get("_runtime") if isinstance(plan.get("_runtime"), dict) else {},
        "explanation": plan.get("_explanation") if isinstance(plan.get("_explanation"), dict) else {},
        "continuity": continuity,
        "schema_canonicalization": canonicalization,
        "candidate_selection": selection,
        "defect_attribution": defect_attribution,
        "content_clipped": clipped,
        "warnings": sorted(set(warnings)),
    }


def write_canvas_evaluation(
    *,
    job_id: str,
    mode: str,
    job_dir: Path,
    output_dir: Path,
    final_video: Optional[Path],
    request_data: Optional[Dict[str, Any]] = None,
    plan_data: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write outputs/<job>/evaluation.json and return its path."""
    job_dir = Path(job_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    scenes = [_scene_report(path) for path in _plan_paths(job_dir)]
    final_info = _dimensions(final_video)
    final_duration = _media_duration(final_video)
    runtime_path = job_dir / "runtime_diagnostics.json"
    try:
        runtime_config = json.loads(runtime_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        runtime_config = {}
    duration_details_path = job_dir / "duration_diagnostics.json"
    try:
        measured_duration_details = json.loads(duration_details_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        measured_duration_details = {}
    try:
        from run_log import get_summary

        llm_usage = get_summary(job_id)
    except Exception as exc:
        llm_usage = {"job_id": job_id, "error": str(exc)[:300]}
    expected_ratio = VIDEO_WIDTH / VIDEO_HEIGHT
    configured_is_16_9 = abs(expected_ratio - (16 / 9)) < 0.001
    final_matches = bool(
        final_info
        and not final_info.get("probe_error")
        and (final_info.get("width"), final_info.get("height")) == (VIDEO_WIDTH, VIDEO_HEIGHT)
    )
    all_scene_canvases_match = bool(scenes) and all(
        scene.get("canvas", {}).get("matches_configured_video") for scene in scenes
    )
    all_scene_rasters_match = bool(scenes) and all(
        scene.get("raster")
        and not scene["raster"].get("probe_error")
        and (scene["raster"].get("width"), scene["raster"].get("height"))
        == (VIDEO_WIDTH, VIDEO_HEIGHT)
        for scene in scenes
    )
    clipped_scene_ids = [scene["scene_id"] for scene in scenes if scene.get("content_clipped")]
    missing_anchor_scenes = [
        scene for scene in scenes
        if scene.get("explanation", {}).get("anchor_coverage_pass") is False
    ]
    anchor_evaluated_scenes = [
        scene for scene in scenes
        if int(scene.get("explanation", {}).get("anchor_count", 0)) > 0
    ]
    continued_scenes = [
        scene for scene in scenes
        if scene.get("continuity", {}).get("declared_mode") == "continue"
    ]
    applied_continuity = [
        scene for scene in continued_scenes
        if scene.get("continuity", {}).get("applied")
    ]
    visible_reuse_scenes = [
        scene for scene in applied_continuity
        if int(scene.get("continuity", {}).get("reused_element_count", 0))
        + int(scene.get("continuity", {}).get("carried_element_count", 0)) > 0
    ]
    if not continued_scenes:
        problem_2_status = "not_evaluable_no_continued_scenes"
    elif len(applied_continuity) != len(continued_scenes):
        problem_2_status = "fail_continuity_not_applied"
    elif len(visible_reuse_scenes) != len(continued_scenes):
        problem_2_status = "needs_review_no_matched_elements"
    else:
        problem_2_status = "pass"

    compiled_storyboard_scenes = [
        scene for scene in scenes
        if scene.get("semantic_storyboard", {}).get("compiled")
        and scene.get("visual_source") == "semantic_storyboard"
    ]
    coordinate_free_storyboard_scenes = [
        scene for scene in compiled_storyboard_scenes
        if scene.get("semantic_storyboard", {}).get("coordinate_fields_from_llm") == 0
    ]
    v2_storyboard_scenes = [
        scene for scene in coordinate_free_storyboard_scenes
        if scene.get("semantic_storyboard", {}).get("compiler") == "deterministic_v2"
    ]
    overlapping_storyboard_scenes = [
        scene for scene in v2_storyboard_scenes
        if int(scene.get("semantic_storyboard", {}).get("node_overlap_count", 0)) > 0
    ]
    card_only_storyboard_scenes = [
        scene for scene in v2_storyboard_scenes
        if scene.get("semantic_storyboard", {}).get("card_only")
    ]
    repaired_freeform_scenes = [
        scene for scene in scenes
        if scene.get("visual_source") == "freeform_elements"
        and scene.get("freeform_repair", {}).get("applied")
        and scene.get("freeform_repair", {}).get("composition_preserved")
    ]
    supported_hybrid_scenes = {
        scene["scene_id"] for scene in v2_storyboard_scenes + repaired_freeform_scenes
    }
    sparse_foreground_scenes = [
        scene for scene in scenes
        if scene.get("foreground_composition")
        and float(scene["foreground_composition"].get("area_ratio", 0)) < 0.12
    ]
    illegible_layout_scenes = [
        scene for scene in scenes
        if (scene.get("rendered_legibility") or {}).get("pass") is False
    ]
    shallow_foreground_scenes = [
        scene for scene in scenes
        if scene.get("foreground_composition")
        and float(scene["foreground_composition"].get("area_ratio", 0)) < 0.28
        and float(scene["foreground_composition"].get("vertical_span_ratio", 0)) < 0.28
    ]
    offcenter_foreground_scenes = [
        scene for scene in scenes
        if scene.get("foreground_composition")
        and max(
            float(scene["foreground_composition"].get("center_offset_x_ratio", 0)),
            float(scene["foreground_composition"].get("center_offset_y_ratio", 0)),
        ) > 0.30
    ]
    autoframed_scenes = [
        scene for scene in scenes
        if (scene.get("foreground_composition") or {}).get("auto_framed")
    ]
    missing_continuity_base_scenes = [
        scene for scene in scenes
        if scene.get("continuity", {}).get("reason") == "continuity_base_unavailable"
    ]
    missing_semantic_evidence_scenes = [
        scene for scene in scenes
        if scene.get("explanation", {}).get("evidence_coverage_pass") is False
    ]
    if not scenes:
        problem_3_status = "not_evaluable_no_scenes"
    elif len(supported_hybrid_scenes) != len(scenes):
        problem_3_status = "fail_unrepaired_or_fallback_visual_plan_present"
    elif overlapping_storyboard_scenes:
        problem_3_status = "fail_semantic_node_overlap"
    elif missing_semantic_evidence_scenes:
        problem_3_status = "fail_required_visual_evidence_missing"
    elif illegible_layout_scenes:
        problem_3_status = "fail_rendered_text_below_legibility_threshold"
    elif shallow_foreground_scenes:
        problem_3_status = "fail_shallow_foreground_composition"
    elif sparse_foreground_scenes:
        problem_3_status = "fail_sparse_foreground_composition"
    elif offcenter_foreground_scenes:
        problem_3_status = "fail_offcenter_foreground_composition"
    elif len(scenes) >= 2 and not repaired_freeform_scenes and len(card_only_storyboard_scenes) == len(scenes):
        problem_3_status = "fail_uniform_card_only_video"
    else:
        problem_3_status = "pass"

    styled_scenes = [
        scene for scene in scenes
        if scene.get("graphic_style", {}).get("applied")
        and scene.get("graphic_style", {}).get("layout_preserved")
        and int(scene.get("graphic_style", {}).get("extra_llm_calls", 0)) == 0
    ]
    icon_families = sorted({
        str(scene.get("graphic_style", {}).get("icon_family", "")).strip()
        for scene in styled_scenes
        if str(scene.get("graphic_style", {}).get("icon_family", "")).strip()
    })
    icon_queries = sum(int(scene.get("graphic_style", {}).get("icon_query_count", 0)) for scene in scenes)
    resolved_icons = sum(int(scene.get("graphic_style", {}).get("icons_resolved", 0)) for scene in scenes)
    monogram_fallbacks = sum(
        int(scene.get("graphic_style", {}).get("icon_monogram_fallbacks", 0)) for scene in scenes
    )
    dot_fallbacks = sum(
        int(scene.get("graphic_style", {}).get("generic_dot_fallbacks", 0)) for scene in scenes
    )
    icon_resolution_ratio = round(resolved_icons / icon_queries, 6) if icon_queries else 1.0
    icon_fallback_ratio = round(monogram_fallbacks / icon_queries, 6) if icon_queries else 0.0
    unresolved_overlap_scenes = [
        scene for scene in scenes
        if int(scene.get("freeform_repair", {}).get("unresolved_text_overlap_count", 0)) > 0
    ]
    unresolved_cross_type_scenes = [
        scene for scene in scenes
        if int(scene.get("freeform_repair", {}).get("unresolved_cross_type_collision_count", 0)) > 0
    ]
    foreground_occlusion_scenes = [
        scene for scene in scenes
        if int(scene.get("freeform_repair", {}).get("foreground_occlusion_count", 0)) > 0
    ]
    detached_label_scenes = [
        scene for scene in scenes
        if int(scene.get("freeform_repair", {}).get("unresolved_detached_label_count", 0)) > 0
    ]
    empty_container_scenes = [
        scene for scene in scenes
        if int(scene.get("freeform_repair", {}).get("empty_container_count", 0)) > 0
    ]
    low_density_container_scenes = [
        scene for scene in scenes
        if int(scene.get("freeform_repair", {}).get("low_density_container_count", 0)) > 0
    ]
    low_icon_resolution_scenes = [
        scene for scene in scenes
        if int(scene.get("graphic_style", {}).get("icon_query_count", 0)) >= 2
        and (
            int(scene.get("graphic_style", {}).get("icons_resolved", 0))
            / max(1, int(scene.get("graphic_style", {}).get("icon_query_count", 0)))
        ) < 0.50
    ]
    low_icon_resolution = (
        icon_queries >= 2 and icon_resolution_ratio < 0.65
    ) or bool(low_icon_resolution_scenes)
    excessive_icon_fallback = icon_queries >= 3 and icon_fallback_ratio > 0.34
    problem_4_failures: List[str] = []
    if not scenes:
        problem_4_failures.append("not_evaluable_no_scenes")
    else:
        checks = [
            (len(styled_scenes) != len(scenes), "fail_art_direction_not_applied_to_every_scene"),
            (len(icon_families) > 1, "fail_mixed_icon_families"),
            (bool(dot_fallbacks), "fail_generic_dot_icon_fallback_present"),
            (bool(monogram_fallbacks), "fail_icon_monogram_fallback_present"),
            (low_icon_resolution, "fail_icon_resolution_below_quality_threshold"),
            (excessive_icon_fallback, "fail_excessive_icon_fallbacks"),
            (bool(unresolved_overlap_scenes), "fail_unresolved_text_collisions"),
            (bool(unresolved_cross_type_scenes), "fail_unresolved_cross_type_collisions"),
            (bool(foreground_occlusion_scenes), "fail_foreground_occlusion"),
            (bool(detached_label_scenes), "fail_detached_semantic_labels"),
            (bool(empty_container_scenes), "fail_empty_visual_containers"),
            (bool(low_density_container_scenes), "fail_low_density_visual_containers"),
            (bool(illegible_layout_scenes), "fail_rendered_text_below_legibility_threshold"),
            (bool(shallow_foreground_scenes), "fail_shallow_foreground_composition"),
        ]
        problem_4_failures.extend(code for failed, code in checks if failed)
    problem_4_status = problem_4_failures[0] if problem_4_failures else "pass"

    runtime_fingerprint = str(runtime_config.get("fingerprint", ""))
    scene_runtime_fingerprints = [
        str(scene.get("runtime_config", {}).get("fingerprint", ""))
        for scene in scenes
    ]
    problem_6_pass = bool(
        runtime_config
        and runtime_config.get("process_environment_mutated") is False
        and runtime_config.get("quality_mode") in {"fast", "balanced", "rich"}
        and str(runtime_config.get("planner_model", "")).strip()
    )
    if not runtime_config:
        problem_6_status = "not_evaluable_missing_runtime_diagnostics"
    elif not problem_6_pass:
        problem_6_status = "fail_planner_model_not_job_scoped"
    else:
        problem_6_status = "pass"

    problem_7_pass = bool(
        scenes
        and runtime_fingerprint
        and runtime_config.get("isolation_scope") == "immutable_job_context"
        and all(value == runtime_fingerprint for value in scene_runtime_fingerprints)
    )
    if not runtime_config or not scenes:
        problem_7_status = "not_evaluable_missing_runtime_or_scenes"
    elif not problem_7_pass:
        problem_7_status = "fail_scene_runtime_configuration_mismatch"
    else:
        problem_7_status = "pass"

    target_duration = _number((request_data or {}).get("target_duration_seconds"), 0.0)
    duration_error_seconds = (
        round(final_duration - target_duration, 6)
        if final_duration is not None and target_duration > 0
        else None
    )
    duration_error_ratio = (
        round(abs(duration_error_seconds) / target_duration, 6)
        if duration_error_seconds is not None and target_duration > 0
        else None
    )
    duration_tolerance_seconds = max(
        float(os.environ.get("DURATION_ACCEPTANCE_SECONDS", "5")),
        target_duration * float(os.environ.get("DURATION_ACCEPTANCE_RATIO", "0.10")),
    ) if target_duration > 0 else None
    duration_integrity_pass = bool(
        runtime_config
        and final_duration is not None
        and runtime_config.get("process_environment_mutated") is False
        and runtime_config.get("audio_source") in {"raw_tts", "raw_tts_plus_boundary_silence"}
        and runtime_config.get("tempo_adjustment_applied") is False
        and runtime_config.get("calibration_sample_policy") == "raw_tts_only"
    )
    duration_target_pass = bool(
        duration_error_seconds is not None
        and duration_tolerance_seconds is not None
        and abs(duration_error_seconds) <= duration_tolerance_seconds
    )
    raw_speech_seconds = _number(measured_duration_details.get("raw_speech_seconds"), 0.0)
    boundary_pause_seconds = _number(measured_duration_details.get("boundary_pause_seconds"), 0.0)
    spoken_coverage_ratio = (
        round(raw_speech_seconds / target_duration, 6)
        if raw_speech_seconds > 0 and target_duration > 0 else None
    )
    silence_ratio = (
        round(boundary_pause_seconds / final_duration, 6)
        if boundary_pause_seconds > 0 and final_duration and final_duration > 0 else 0.0
    )
    minimum_spoken_coverage = float(os.environ.get("MIN_SPOKEN_COVERAGE_RATIO", "0.85"))
    maximum_silence_ratio = float(os.environ.get("MAX_ACCEPTABLE_SILENCE_RATIO", "0.08"))
    duration_content_pass = bool(
        not measured_duration_details
        or (
            spoken_coverage_ratio is not None
            and spoken_coverage_ratio >= minimum_spoken_coverage
            and silence_ratio <= maximum_silence_ratio
        )
    )
    duration_quality_pass = duration_integrity_pass and duration_target_pass and duration_content_pass
    if final_duration is None or target_duration <= 0:
        problem_8_status = "not_evaluable_missing_duration"
    elif not duration_integrity_pass:
        problem_8_status = "fail_duration_integrity"
    elif not duration_content_pass:
        problem_8_status = "fail_excessive_silence_or_insufficient_spoken_content"
    elif not duration_target_pass:
        problem_8_status = "needs_calibration_target_drift"
    else:
        problem_8_status = "pass"

    plan_data = plan_data if isinstance(plan_data, dict) else {}
    explanation_diagnostics = (
        plan_data.get("explanation_diagnostics")
        if isinstance(plan_data.get("explanation_diagnostics"), dict) else {}
    )
    report = {
        "report_version": REPORT_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "mode": str(mode),
        "focus_problem": "job_isolation_and_raw_voice_duration_integrity",
        "request": dict(request_data or {}),
        "explanation": {
            "audience_profile": plan_data.get("audience_profile", {}),
            "strategy": plan_data.get("explanation_strategy", {}),
            "duration_teaching_contract": plan_data.get("duration_teaching_contract", {}),
            "planning_token_budget": plan_data.get("planning_token_budget", {}),
            "factuality_contract": plan_data.get("factuality_contract", {}),
            "visual_sequence_blueprint": plan_data.get("visual_sequence_blueprint", {}),
            "diagnostics": explanation_diagnostics,
            "scenes": [
                {
                    "scene_id": scene.get("id"),
                    "teaching_role": scene.get("teaching_role"),
                    "learner_takeaway": scene.get("learner_takeaway"),
                    "builds_on": scene.get("builds_on"),
                    "semantic_contract": scene.get("semantic_contract", {}),
                }
                for scene in plan_data.get("scenes", [])
                if isinstance(scene, dict)
            ],
        },
        "configured_video": {
            "width": VIDEO_WIDTH,
            "height": VIDEO_HEIGHT,
            "aspect_ratio": round(expected_ratio, 6),
            "is_16_9": configured_is_16_9,
        },
        "final_video": dict(final_info or {}, duration_seconds=final_duration),
        "runtime_config": runtime_config,
        "duration_diagnostics": {
            "target_seconds": target_duration or None,
            "actual_seconds": final_duration,
            "error_seconds": duration_error_seconds,
            "absolute_error_seconds": abs(duration_error_seconds) if duration_error_seconds is not None else None,
            "error_ratio": duration_error_ratio,
            "acceptance_tolerance_seconds": duration_tolerance_seconds,
            "target_pass": duration_target_pass,
            "spoken_content_pass": duration_content_pass,
            "duration_quality_pass": duration_quality_pass,
            "raw_speech_seconds": raw_speech_seconds or None,
            "spoken_coverage_ratio": spoken_coverage_ratio,
            "minimum_spoken_coverage_ratio": minimum_spoken_coverage,
            "boundary_pause_seconds": boundary_pause_seconds,
            "silence_ratio": silence_ratio,
            "maximum_acceptable_silence_ratio": maximum_silence_ratio,
            "audio_source": runtime_config.get("audio_source", "raw_tts_plus_boundary_silence"),
            "tempo_adjustment_applied": False,
            "calibration_sample_policy": "raw_tts_only",
            **measured_duration_details,
        },
        "llm_usage": llm_usage,
        "summary": {
            "scene_count": len(scenes),
            "explanation_contract_present": bool(explanation_diagnostics),
            "audience_profile_complete": explanation_diagnostics.get("audience_profile_complete"),
            "explanation_strategy_complete": explanation_diagnostics.get("strategy_complete"),
            "all_scenes_have_teaching_takeaways": explanation_diagnostics.get("all_scenes_have_takeaways"),
            "all_scenes_have_semantic_claims": explanation_diagnostics.get("all_scenes_have_claims"),
            "all_scenes_have_visual_evidence": explanation_diagnostics.get("all_scenes_have_visual_evidence"),
            "all_exact_anchors_grounded_in_narration": explanation_diagnostics.get("all_exact_anchors_grounded_in_narration"),
            "visual_anchor_coverage_pass": (
                not missing_anchor_scenes if anchor_evaluated_scenes else None
            ),
            "visual_anchor_evaluated_scene_ids": [
                scene["scene_id"] for scene in anchor_evaluated_scenes
            ],
            "missing_visual_anchor_scene_ids": [scene["scene_id"] for scene in missing_anchor_scenes],
            "semantic_reference_binding_scene_ids": [
                scene["scene_id"] for scene in scenes
                if scene.get("explanation", {}).get("semantic_binding_mode") == "semantic_references"
            ],
            "legacy_lexical_binding_scene_ids": [
                scene["scene_id"] for scene in scenes
                if scene.get("explanation", {}).get("semantic_binding_mode") == "legacy_lexical_fallback"
            ],
            "all_scenes_use_open_world_semantic_bindings": bool(scenes) and all(
                scene.get("explanation", {}).get("semantic_binding_mode") == "semantic_references"
                for scene in scenes
            ),
            "pre_render_quality_gate_pass_scene_ids": [
                scene["scene_id"] for scene in scenes
                if scene.get("pre_render_quality_gate", {}).get("status") == "pass"
            ],
            "pre_render_quality_gate_partial_scene_ids": [
                scene["scene_id"] for scene in scenes
                if scene.get("pre_render_quality_gate", {}).get("status") == "partial"
            ],
            "pre_render_quality_gate_failed_scene_ids": [
                scene["scene_id"] for scene in scenes
                if scene.get("pre_render_quality_gate", {}).get("status") == "fail"
            ],
            "configured_canvas_is_16_9": configured_is_16_9,
            "all_scene_canvases_match_video": all_scene_canvases_match,
            "all_scene_rasters_match_video": all_scene_rasters_match,
            "final_video_matches_configured_dimensions": final_matches,
            "clipped_scene_ids": clipped_scene_ids,
            "problem_1_pass": bool(
                configured_is_16_9
                and all_scene_canvases_match
                and all_scene_rasters_match
                and final_matches
            ),
            "declared_continuation_scene_ids": [scene["scene_id"] for scene in continued_scenes],
            "applied_continuation_scene_ids": [scene["scene_id"] for scene in applied_continuity],
            "visible_reuse_scene_ids": [scene["scene_id"] for scene in visible_reuse_scenes],
            "reused_element_total": sum(
                int(scene.get("continuity", {}).get("reused_element_count", 0)) for scene in scenes
            ),
            "carried_element_total": sum(
                int(scene.get("continuity", {}).get("carried_element_count", 0)) for scene in scenes
            ),
            "duplicate_carries_suppressed_total": sum(
                len(scene.get("continuity", {}).get("duplicate_carries_suppressed", []))
                for scene in scenes
            ),
            "problem_2_status": problem_2_status,
            "problem_2_pass": problem_2_status == "pass",
            "semantic_storyboard_scene_ids": [
                scene["scene_id"] for scene in compiled_storyboard_scenes
            ],
            "coordinate_free_storyboard_scene_ids": [
                scene["scene_id"] for scene in coordinate_free_storyboard_scenes
            ],
            "storyboard_layouts": {
                str(scene["scene_id"]): scene.get("semantic_storyboard", {}).get("layout")
                for scene in compiled_storyboard_scenes
            },
            "storyboard_item_total": sum(
                int(scene.get("semantic_storyboard", {}).get("item_count", 0))
                for scene in scenes
            ),
            "storyboard_relation_total": sum(
                int(scene.get("semantic_storyboard", {}).get("relation_count", 0))
                for scene in scenes
            ),
            "storyboard_v2_scene_ids": [scene["scene_id"] for scene in v2_storyboard_scenes],
            "repaired_freeform_scene_ids": [scene["scene_id"] for scene in repaired_freeform_scenes],
            "hybrid_quality_path_scene_ids": sorted(supported_hybrid_scenes),
            "freeform_repair_totals": {
                field: sum(int(scene.get("freeform_repair", {}).get(field, 0)) for scene in repaired_freeform_scenes)
                for field in (
                    "duplicate_elements_removed", "duplicate_ids_renamed",
                    "font_sizes_raised", "connector_endpoints_repaired",
                    "background_layers_repaired", "text_contrast_repaired",
                    "text_overlaps_repaired", "orphan_support_overlays_removed",
                    "duplicate_environment_supports_removed", "outline_fills_removed",
                )
            },
            "auto_framed_scene_ids": [scene["scene_id"] for scene in autoframed_scenes],
            "sparse_foreground_scene_ids": [scene["scene_id"] for scene in sparse_foreground_scenes],
            "shallow_foreground_scene_ids": [scene["scene_id"] for scene in shallow_foreground_scenes],
            "rendered_text_below_legibility_scene_ids": [scene["scene_id"] for scene in illegible_layout_scenes],
            "offcenter_foreground_scene_ids": [scene["scene_id"] for scene in offcenter_foreground_scenes],
            "continuity_base_unavailable_scene_ids": [scene["scene_id"] for scene in missing_continuity_base_scenes],
            "storyboard_overlap_scene_ids": [scene["scene_id"] for scene in overlapping_storyboard_scenes],
            "card_only_storyboard_scene_ids": [scene["scene_id"] for scene in card_only_storyboard_scenes],
            "storyboard_representation_totals": {
                representation: sum(
                    int(scene.get("semantic_storyboard", {}).get("representation_counts", {}).get(representation, 0))
                    for scene in scenes
                )
                for representation in ("bar", "card", "circle", "icon", "label", "pill", "ring")
            },
            "problem_3_status": problem_3_status,
            "problem_3_pass": problem_3_status == "pass",
            "graphic_styled_scene_ids": [scene["scene_id"] for scene in styled_scenes],
            "graphic_theme_by_scene": {
                str(scene["scene_id"]): scene.get("graphic_style", {}).get("theme")
                for scene in styled_scenes
            },
            "icon_families_used": icon_families,
            "icon_query_total": icon_queries,
            "icon_resolved_total": resolved_icons,
            "icon_monogram_fallback_total": monogram_fallbacks,
            "generic_dot_fallback_total": dot_fallbacks,
            "icon_resolution_ratio": icon_resolution_ratio,
            "icon_fallback_ratio": icon_fallback_ratio,
            "low_icon_resolution_scene_ids": [scene["scene_id"] for scene in low_icon_resolution_scenes],
            "icon_resolution_attempt_total": sum(
                int(scene.get("graphic_style", {}).get("icon_resolution_attempts", 0)) for scene in scenes
            ),
            "icon_query_reduction_total": sum(
                int(scene.get("graphic_style", {}).get("icon_query_reductions", 0)) for scene in scenes
            ),
            "text_overlap_repaired_total": sum(
                int(scene.get("freeform_repair", {}).get("text_overlaps_repaired", 0)) for scene in scenes
            ),
            "unresolved_text_overlap_scene_ids": [scene["scene_id"] for scene in unresolved_overlap_scenes],
            "cross_type_collision_repaired_total": sum(
                int(scene.get("freeform_repair", {}).get("cross_type_collisions_repaired", 0))
                for scene in scenes
            ),
            "unresolved_cross_type_collision_scene_ids": [
                scene["scene_id"] for scene in unresolved_cross_type_scenes
            ],
            "missing_semantic_evidence_scene_ids": [
                scene["scene_id"] for scene in missing_semantic_evidence_scenes
            ],
            "orphan_support_overlay_removed_total": sum(
                int(scene.get("freeform_repair", {}).get("orphan_support_overlays_removed", 0)) for scene in scenes
            ),
            "orphan_support_carry_suppressed_total": sum(
                len(scene.get("continuity", {}).get("orphan_support_carries_suppressed", []))
                for scene in scenes
            ),
            "duplicate_environment_support_removed_total": sum(
                int(scene.get("freeform_repair", {}).get("duplicate_environment_supports_removed", 0))
                for scene in scenes
            ),
            "outline_fill_removed_total": sum(
                int(scene.get("freeform_repair", {}).get("outline_fills_removed", 0)) for scene in scenes
            ),
            "foreground_occlusion_scene_ids": [scene["scene_id"] for scene in foreground_occlusion_scenes],
            "foreground_occlusion_total": sum(
                int(scene.get("freeform_repair", {}).get("foreground_occlusion_count", 0))
                for scene in scenes
            ),
            "detached_label_repaired_total": sum(
                int(scene.get("freeform_repair", {}).get("detached_labels_repaired", 0))
                for scene in scenes
            ),
            "unresolved_detached_label_scene_ids": [scene["scene_id"] for scene in detached_label_scenes],
            "empty_container_scene_ids": [scene["scene_id"] for scene in empty_container_scenes],
            "low_density_container_scene_ids": [scene["scene_id"] for scene in low_density_container_scenes],
            "container_text_constrained_total": sum(
                int(scene.get("freeform_repair", {}).get("container_text_constraints_applied", 0))
                for scene in scenes
            ),
            "container_text_truncated_scene_ids": [
                scene["scene_id"] for scene in scenes
                if int(scene.get("freeform_repair", {}).get("container_text_truncated_count", 0)) > 0
            ],
            "visible_meta_text_removed_total": sum(
                int(scene.get("freeform_repair", {}).get("visible_meta_text_removed", 0))
                for scene in scenes
            ),
            "occluding_layers_lowered_total": sum(
                int(scene.get("freeform_repair", {}).get("occluding_layers_lowered", 0))
                for scene in scenes
            ),
            "carried_layer_reorder_total": sum(
                int(scene.get("continuity", {}).get("carried_layer_reorders", 0)) for scene in scenes
            ),
            "depth_effect_total": sum(
                int(scene.get("graphic_style", {}).get("depth_effects_applied", 0)) for scene in scenes
            ),
            "outer_container_softened_total": sum(
                int(scene.get("graphic_style", {}).get("outer_containers_softened", 0)) for scene in scenes
            ),
            "arrowhead_harmonized_total": sum(
                int(scene.get("graphic_style", {}).get("arrowheads_harmonized", 0)) for scene in scenes
            ),
            "typography_harmonized_total": sum(
                int(scene.get("graphic_style", {}).get("typography_harmonized", 0)) for scene in scenes
            ),
            "problem_4_status": problem_4_status,
            "problem_4_failures": problem_4_failures,
            "problem_4_pass": problem_4_status == "pass",
            "problem_6_status": problem_6_status,
            "problem_6_pass": problem_6_pass,
            "runtime_fingerprint": runtime_fingerprint or None,
            "scene_runtime_fingerprints": scene_runtime_fingerprints,
            "problem_7_status": problem_7_status,
            "problem_7_pass": problem_7_pass,
            "duration_integrity_pass": duration_integrity_pass,
            "duration_target_pass": duration_target_pass,
            "duration_content_pass": duration_content_pass,
            "duration_quality_pass": duration_quality_pass,
            "problem_8_status": problem_8_status,
            "problem_8_pass": problem_8_status == "pass",
        },
        "scenes": scenes,
    }
    path = output_dir / "evaluation.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


__all__ = ["write_canvas_evaluation"]
