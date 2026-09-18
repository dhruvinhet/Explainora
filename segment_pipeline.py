"""
segment_pipeline.py — Overlapped, streaming segment pipeline.

The heart of the "real-time" upgrade. Instead of:
    script -> ALL TTS -> ALL visuals -> ALL encoding -> final.mp4
we run:
    script(scene1) -> scene1 TTS+visual+encode  -> emit segment 1 -> player starts
    script(scene2) -> scene2 TTS+visual+encode  -> emit segment 2 -> player continues
    ...                                                         -> final.mp4

Design:
- One Python thread per job (same as before) but inside it, scene tasks run
  concurrently via a ThreadPoolExecutor: TTS and visual generation for a
  scene overlap with encoding of previous scenes and planning of later ones.
- Each scene's assets land in WORK_DIR/{job}/segments/scene_NNN/.
- Progress and segment events are pushed to the browser over Flask-SocketIO
  when a socketio instance is provided (otherwise plain prints).
- Duration is driven by raw narration audio. The calibrator predicts scene
  duration and learns only from unmodified TTS observations; audio is never
  tempo-stretched to hide target drift.

Exports:
    run_streaming_pipeline(job_id, payload, socketio) -> Dict result info
"""

import json
import html
import time
import os
import re
import threading
import textwrap
import traceback
import uuid
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from duration_calibrator import (
    estimate_seconds,
    estimate_text_seconds,
    estimate_words,
    record_text_sample,
    calibration_summary,
)
from tts_engine import audio_duration_seconds, pad_wav_to_duration, synthesize
from explanation_quality import assess_visual_anchor_coverage
from visual_engine import (
    PLANNER_MODEL, create_visual_plan,
    compile_static_beat_states, materialize_static_visual_state,
    normalize_visual_plan, render_visual_plan, beat_visual_timeline_instruction,
    evaluate_pre_render_quality_gate,
)
from job_runtime import JobRuntimeConfig, activate_runtime
from graphics_style import apply_graphic_style
from freeform_repair import repair_post_continuity_plan
from video_engine import render_scene_video_sequence, svg_to_png
from fast_render import render_scene_video_fast
from incremental_video_assembler import IncrementalVideoAssembler
from diagram_engine import build_diagram_scene, has_diagram, sanitize_diagram_spec
from animated_scene import (
    ANIM_ENABLED,
    ANIM_SYSTEM_PROMPT,
    parse_animations_from_plan,
    render_animated_segment,
    fast_svg_animation_available,
)
from continuity_engine import ContinuityCoordinator, continuity_planner_instruction
from visual_quality import CandidateRegistry, semantic_fallback_plan
from run_log import log_local_transform

from director import create_video_plan

from dotenv import load_dotenv

load_dotenv()

_BEAT_STOP_WORDS = {
    "about", "after", "again", "also", "because", "before", "being", "from",
    "have", "into", "just", "only", "show", "that", "their", "then", "there",
    "these", "they", "this", "through", "visual", "what", "when", "where", "which",
    "with", "your",
}


def _beat_tokens(value: Any) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", str(value).casefold())
        if len(token) >= 3 and token not in _BEAT_STOP_WORDS
    }


def _semantic_element_beat_map(
    elements: List[Dict[str, Any]], beats: List[Dict[str, Any]]
) -> Dict[int, int]:
    """Bind fallback animation elements to the beat that describes them."""
    if not beats:
        return {index: 0 for index in range(len(elements))}
    beat_terms = [
        _beat_tokens(f"{beat.get('narration', '')} {beat.get('visual_action', '')}")
        for beat in beats
    ]
    result: Dict[int, int] = {}
    for index, element in enumerate(elements):
        element_terms = _beat_tokens(" ".join([
            str(element.get("id", "")), str(element.get("text", "")),
            str(element.get("query", "")), str(element.get("owner_id", "")),
            " ".join(map(str, element.get("semantic_refs", []) or [])),
        ]))
        scores = [len(element_terms & terms) for terms in beat_terms]
        best = max(scores, default=0)
        result[index] = scores.index(best) if best else min(
            len(beats) - 1,
            int(index / max(1, len(elements)) * len(beats)),
        )

    # Every spoken beat owns at least one reveal. Reassign the weakest generic
    # elements only when semantic matching left a beat empty.
    covered = set(result.values())
    for missing in (beat for beat in range(len(beats)) if beat not in covered):
        candidate = min(
            result,
            key=lambda idx: (
                len(_beat_tokens(" ".join([
                    str(elements[idx].get("id", "")),
                    str(elements[idx].get("text", "")),
                ])) & beat_terms[result[idx]]),
                abs(idx - round(missing * max(1, len(elements) - 1) / max(1, len(beats) - 1))),
            ),
        )
        result[candidate] = missing
        covered.add(missing)
    return result

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(os.environ.get("WORK_DIR", BASE_DIR / "work"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Feature B: word-level narration alignment via Groq Whisper.
# Cheap, deterministic, zero LLM tokens. Off by default in env; the app
# enables it from the streaming payload (and it is harmless when the pool
# has no usable keys — the pipeline keeps its fallback timing).
# Feature C: deterministic diagram scenes (planner can request a diagram;
# the diagram engine renders it from data). On by default; diagrams also
# save tokens because the planner's diagram spec replaces a full element list.
# Feature E: quality dial — 'fast' | 'balanced' | 'rich', resolved per job
# from payload.quality_mode. 'fast' uses the smaller scene-planner model and
# a reduced element budget to cut LLM cost by roughly a third.
def _run_word_timestamps(wav: Path, job_id: str) -> None:
    """Background job: Whisper-align a narration WAV and persist words.json.

    Never raises: a failure simply leaves the scene without word timestamps,
    and the renderer falls back to narration-relative timing.
    """
    try:
        from tts_engine import save_word_timestamps, word_timestamps
        words = word_timestamps(wav, job_id=job_id)
        if words:
            save_word_timestamps(wav, words)
    except Exception:  # noqa: BLE001
        pass


def _should_use_deterministic_diagram(
    runtime: Optional[JobRuntimeConfig], visual_plan: Dict[str, Any]
) -> bool:
    """Keep object-rich Clean diagrams; compile graphs for other presets."""
    return bool(
        runtime is not None
        and runtime.diagrams
        and runtime.style != "clean-diagrams"
        and has_diagram(visual_plan)
    )


def _parse_bool(value: Any) -> Optional[bool]:
    """Parse on/off values from JSON or form payloads (None if absent/unclear)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return None

SCENE_WORKERS = max(
    1,
    int(os.environ.get("SCENE_WORKERS", os.environ.get("MAX_VISUAL_WORKERS", "2"))),
)
try:
    from api_key_pool import recommended_provider_workers
    from llm_provider import get_llm_config

    SCENE_WORKERS = recommended_provider_workers(
        get_llm_config("planner").provider, SCENE_WORKERS
    )
except Exception:
    pass
DURATION_TOLERANCE = float(os.environ.get("DURATION_TOLERANCE_SECONDS", "20"))
MAX_ATTEMPTS_PER_SCENE = int(os.environ.get("MAX_ATTEMPTS_PER_SCENE", "2"))

EVENTS: List[str] = []  # telemetry only; never used as per-job configuration.


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _emit(socketio, event: str, payload: Dict[str, Any]) -> None:
    EVENTS.append(event)
    if socketio is not None:
        try:
            job_id = str(payload.get("job_id") or "")
            if job_id:
                socketio.emit(event, payload, to=f"job:{job_id}")
            else:
                # Events without a job boundary are diagnostic only and must
                # never be broadcast to every connected user.
                print(f"[event:{event}]", json.dumps(payload, default=str))
        except Exception as exc:
            print(f"SocketIO emit failed for {event}: {exc}")
    else:
        print(f"[event:{event}]", json.dumps(payload, default=str))


def _record_duration_observation(job_dir: Path, plan: Dict[str, Any], segments: List[Dict[str, Any]]) -> None:
    by_id = {int(segment["scene_id"]): segment for segment in segments}
    for scene in plan.get("scenes", []):
        segment = by_id.get(int(scene.get("id", 0)), {})
        raw_duration = float(segment.get("raw_duration_seconds", 0))
        if str(scene.get("narration", "")).strip() and raw_duration:
            record_text_sample(str(scene["narration"]), raw_duration, raw_tts=True, tempo_factor=1.0)


def _build_emergency_segment(
    job_id: str,
    scene: Dict[str, Any],
    segments_dir: Path,
    assembler: Optional["IncrementalVideoAssembler"],
    runtime: Optional[JobRuntimeConfig],
    original_error: BaseException,
) -> Dict[str, Any]:
    """Finish one scene with local-only assets after its normal path fails."""
    scene_id = int(scene["id"])
    seg_dir = segments_dir / f"scene_{scene_id:03d}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    narration = str(scene.get("narration", "")).strip() or "This scene summarizes the main idea."
    raw_wav = seg_dir / "narration_raw.wav"
    wav = seg_dir / "narration.wav"
    if not wav.is_file():
        if not raw_wav.is_file():
            synthesize(narration, raw_wav, job_id=job_id)
        pad_wav_to_duration(
            raw_wav,
            wav,
            float(scene.get("target_scene_seconds", 0) or audio_duration_seconds(raw_wav)),
        )

    contract = scene.get("semantic_contract") if isinstance(scene.get("semantic_contract"), dict) else {}
    labels = [
        str(value).strip()[:28]
        for value in contract.get("required_entities", [])
        if str(value).strip()
    ][:3]
    if not labels:
        labels = ["Main idea"]
    title = str(scene.get("teaching_beat") or scene.get("title") or f"Scene {scene_id}")[:70]
    claim = str(contract.get("claim") or narration).strip()
    claim_lines = textwrap.wrap(claim, width=58, break_long_words=False)[:3]
    node_width = 260
    gap = 46
    total_width = len(labels) * node_width + max(0, len(labels) - 1) * gap
    start_x = (1280 - total_width) / 2
    nodes = []
    connectors = []
    for index, label in enumerate(labels):
        x = start_x + index * (node_width + gap)
        nodes.append(
            f'<rect x="{x:.1f}" y="305" width="{node_width}" height="130" rx="22" '
            'fill="#e8f1ff" stroke="#2463eb" stroke-width="3"/>'
            f'<text x="{x + node_width / 2:.1f}" y="380" text-anchor="middle" '
            'font-family="Arial, sans-serif" font-size="25" font-weight="700" fill="#172033">'
            f'{html.escape(label)}</text>'
        )
        if index:
            x1 = x - gap + 8
            x2 = x - 8
            connectors.append(
                f'<line x1="{x1:.1f}" y1="370" x2="{x2:.1f}" y2="370" '
                'stroke="#2463eb" stroke-width="4" stroke-linecap="round"/>'
            )
    claim_svg = "".join(
        f'<text x="640" y="{505 + index * 34}" text-anchor="middle" '
        'font-family="Arial, sans-serif" font-size="23" fill="#344054">'
        f'{html.escape(line)}</text>'
        for index, line in enumerate(claim_lines)
    )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">'
        '<rect width="1280" height="720" fill="#f8fafc"/>'
        f'<text x="640" y="105" text-anchor="middle" font-family="Arial, sans-serif" '
        f'font-size="38" font-weight="700" fill="#101828">{html.escape(title)}</text>'
        '<text x="640" y="164" text-anchor="middle" font-family="Arial, sans-serif" '
        'font-size="19" fill="#667085">Key ideas from this part</text>'
        + "".join(connectors) + "".join(nodes) + claim_svg + '</svg>'
    )
    svg_path = seg_dir / "scene.svg"
    png_path = seg_dir / "scene.png"
    mp4_path = seg_dir / "segment.mp4"
    svg_path.write_text(svg, encoding="utf-8")
    svg_to_png(svg, png_path)
    render_scene_video_fast(
        png_path,
        wav,
        mp4_path,
        caption_text=narration,
        captions=runtime.captions if runtime is not None else None,
    )
    duration = audio_duration_seconds(mp4_path)
    stream_total = assembler.add_scene(mp4_path, scene_id=scene_id) if assembler else 0.0
    diagnostic = {
        "source": "local_emergency_segment",
        "reason": f"{type(original_error).__name__}: {original_error}"[:1000],
        "required_entities": labels,
    }
    (seg_dir / "visual_plan.json").write_text(
        json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "scene_id": scene_id,
        "duration_seconds": float(duration),
        "raw_duration_seconds": float(audio_duration_seconds(raw_wav)) if raw_wav.is_file() else float(duration),
        "boundary_pause_seconds": max(0.0, float(duration) - (audio_duration_seconds(raw_wav) if raw_wav.is_file() else float(duration))),
        "png_url": f"/segments/{job_id}/scene_{scene_id:03d}/scene.png",
        "mp4_url": f"/segments/{job_id}/scene_{scene_id:03d}/segment.mp4",
        "svg_url": f"/segments/{job_id}/scene_{scene_id:03d}/scene.svg",
        "narration": narration,
        "visual_prompt": str(scene.get("visual_prompt", narration)),
        "selected_beat_ids": scene.get("selected_beat_ids", []),
        "anim_mode": "local_emergency_static",
        "animation_count": 0,
        "beat_sync": {"rendered": True, "strategy": "local_emergency_static"},
        "stream_url": f"/segments/{job_id}/stream.mp4",
        "stream_total_seconds": round(max(0.0, stream_total), 3),
        "recovered_from_error": diagnostic["reason"],
    }


# ---------------------------------------------------------------------------
# per-scene segment builder
# ---------------------------------------------------------------------------

def _build_segment(
    job_id: str,
    scene: Dict[str, Any],
    scene_dir: Path,
    job_dir: Path,
    sketch: bool,
    socketio: Optional[Any],
    assembler: Optional["IncrementalVideoAssembler"] = None,
    style: Optional[str] = None,
    continuity: Optional[ContinuityCoordinator] = None,
    runtime: Optional[JobRuntimeConfig] = None,
    opening_scene_ready: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """TTS + visual + encode for one scene. Returns segment info."""
    scene_id = int(scene["id"])
    seg_dir = scene_dir / f"scene_{scene_id:03d}"
    seg_dir.mkdir(parents=True, exist_ok=True)

    narration = str(scene["narration"]).strip()
    visual_prompt = str(scene.get("visual_prompt", narration)).strip() or narration

    # ---- adaptive scene budget (per-scene share of the global budget) --------
    semantic_storyboards = os.environ.get(
        "VISUAL_PLANNING_MODE", "hybrid"
    ).strip().lower() == "semantic"
    # The compositor determines element order after the model response, so an
    # LLM cannot safely target element indices. The existing token-free local
    # animation synthesizer handles compiled storyboards instead.
    anim_prompt = ANIM_SYSTEM_PROMPT if ANIM_ENABLED and not semantic_storyboards else None
    state_instruction = continuity_planner_instruction(scene)
    beat_instruction = beat_visual_timeline_instruction(scene)
    anim_prompt = "\n\n".join(
        part for part in (anim_prompt, state_instruction, beat_instruction) if part
    ) or None
    budget = scene.get("element_budget")
    if ANIM_ENABLED and isinstance(budget, dict):
        # Important scenes get a larger slice; simple scenes get less.
        share = budget["allocated"]
        budget_block = (
            f"\nscene_budget: position {int(scene.get('id', 1))} of {budget['scene_count']}; "
            f"allocated element budget {share}; "
            f"hard maximum {budget['max']} elements."
        )
        if not semantic_storyboards:
            anim_prompt = "\n\n".join(
                part for part in (anim_prompt, budget_block) if part
            )

    # Reserve a visual-provider slot before waiting for local speech synthesis.
    # Visual planning and TTS are independent inputs to the later renderer, so
    # serializing them wasted the full TTS model startup time on every scene.
    scene_doc = str(scene.get("doc_excerpts", "")).strip()
    visual_executor = ThreadPoolExecutor(max_workers=1)
    def prepare_visual() -> Dict[str, Any]:
        with activate_runtime(runtime) if runtime is not None else nullcontext():
            return create_visual_plan(
                visual_prompt,
                anim_prompt=anim_prompt,
                doc_excerpts=scene_doc or None,
                job_id=job_id,
                semantic_constraints=scene.get("semantic_contract"),
                scene_id=scene_id,
            )
    visual_future = visual_executor.submit(prepare_visual)

    _emit(
        socketio,
        "scene_progress",
        {"job_id": job_id, "scene_id": scene_id, "stage": "started"},
    )

    # ---- audio (single pass, calibrated) --------------------------------
    raw_wav = Path(scene.get("_prepared_raw_wav") or (seg_dir / "narration_raw.wav"))
    wav = seg_dir / "narration.wav"
    prepared_tts = scene.get("_prepared_tts_future")
    if prepared_tts is not None:
        prepared_path, prepared_duration = prepared_tts.result()
        raw_wav = Path(prepared_path)
        raw_duration = float(prepared_duration)
    else:
        if not raw_wav.is_file():
            synthesize(narration, raw_wav, job_id=job_id)
        raw_duration = float(scene.get("_prepared_raw_duration") or audio_duration_seconds(raw_wav))
    planned_scene_seconds = float(
        scene.get("allocated_scene_seconds", scene.get("target_scene_seconds", raw_duration))
    )
    # Streaming cannot wait for every scene's audio before allocating a global
    # silence budget. Apply the same per-boundary cap locally so one prediction
    # miss never becomes a multi-second dead-air hold.
    max_boundary_pause = max(
        0.0, float(os.environ.get("MAX_SCENE_BOUNDARY_PAUSE_SECONDS", "0.9"))
    )
    target_scene_seconds = max(
        raw_duration, min(planned_scene_seconds, raw_duration + max_boundary_pause)
    )
    padding = pad_wav_to_duration(raw_wav, wav, target_scene_seconds)

    # ---- word-level timestamps (feature B) --------------------------------
    # Whisper alignment runs in a background thread so the render pipeline is
    # never blocked by it. If it succeeds, words.json lands in the scene dir
    # before encoding — the animated renderer consumes it for exact start
    # times; if it fails or isn't available, render falls back to
    # narration-relative timing (unchanged behavior).
    word_thread = None
    if runtime is not None and runtime.word_timestamps:
        try:
            import threading as _wt
            word_thread = _wt.Thread(
                target=_run_word_timestamps,
                args=(raw_wav, job_id),
                daemon=True,
            )
            word_thread.start()
        except Exception:  # noqa: BLE001
            pass

    duration = audio_duration_seconds(wav)
    predicted = estimate_text_seconds(narration)
    _emit(
        socketio,
        "scene_progress",
        {
            "job_id": job_id,
            "scene_id": scene_id,
            "stage": "tts_done",
            "duration_seconds": round(duration, 3),
            "raw_duration_seconds": round(raw_duration, 3),
            "boundary_pause_seconds": padding["boundary_pause_seconds"],
            "predicted_seconds": round(predicted, 3),
        },
    )

    # ---- narration integrity (no tempo correction) --------------------------
    # The narration wav is the SCRIPT — changing its speed destroys content:
    # speeding it up loses words at the end when the video is shorter than the
    # (pre-adjustment) audio, and slowing it down produces unnatural speech.
    # Instead of fighting TTS length, every render path sizes the video from
    # the ACTUAL narration duration (audio-length invariant below), and the
    # duration calibrator learns from past runs so the script writer budgets
    # words correctly on future runs. Calibration feedback is still recorded.
    if target_scene_seconds and target_scene_seconds > 0:
        error_ratio = abs(raw_duration - float(scene.get("target_spoken_seconds", target_scene_seconds))) / max(1.0, target_scene_seconds)
        if error_ratio > 0.15:
            _emit(
                socketio,
                "scene_progress",
                {
                    "job_id": job_id,
                    "scene_id": scene_id,
                    "stage": "calibration_drift",
                    "actual_seconds": round(raw_duration, 2),
                    "predicted_seconds": round(target_scene_seconds, 2),
                },
            )

    # ---- visual -----------------------------------------------------------
    visual_plan = None
    svg, icons = "", []
    # Feature C: deterministic diagrams. When the planner asks for a diagram
    # (structured nodes/edges) we build it with the diagram engine — cheaper
    # in tokens, always well-formed, and cheaper to animate. The LLM SVG path
    # remains the fallback if no diagram was requested or the diagram renders
    # empty.
    try:
        visual_plan = visual_future.result()
        # Clean diagrams is an object-rich, labelled explanatory treatment.
        # Do not discard the planner's full composition in favour of the
        # deterministic node/edge renderer. Other presets may still opt into
        # that renderer for genuine structured diagrams.
        if _should_use_deterministic_diagram(runtime, visual_plan):
            try:
                diag_plan = build_diagram_scene(
                    sanitize_diagram_spec(visual_plan.get("diagram", {})),
                    job_id=job_id, narration=narration, style=style or None,
                )
                if diag_plan and diag_plan.get("elements"):
                    diag_plan["narration"] = narration
                    diag_plan["source"] = "diagram"
                    # The LLM animation list still animates these elements:
                    # diagram element indices map to the same "e" semantics.
                    visual_plan = diag_plan
            except Exception as diag_exc:
                print(f"Scene {scene_id} diagram build failed ({diag_exc}); "
                      "falling back to LLM SVG.")
    except Exception as exc:
        print(f"Scene {scene_id} visual generation failed: {exc}; using fallback.")
    finally:
        visual_executor.shutdown(wait=False, cancel_futures=False)
    if not visual_plan:
        visual_plan = semantic_fallback_plan(
            visual_prompt, scene.get("semantic_contract")
        )
        log_local_transform(
            job_id, "stream_visual_generation_exception", scene_id=scene_id,
            candidate_id="semantic_fallback", after=visual_plan,
            details={"reason": "planner_returned_no_usable_candidate"}, status="repaired",
        )

    if continuity is not None:
        pre_continuity_plan = visual_plan
        visual_plan = continuity.submit(scene, visual_plan)
        log_local_transform(
            job_id, "continuity_reconciliation", scene_id=scene_id,
            candidate_id="post_continuity", before=pre_continuity_plan,
            after=visual_plan,
            details={"decision": visual_plan.get("_continuity", {})}, status="ok",
        )

    beat_source_plan = visual_plan
    visual_plan = materialize_static_visual_state(visual_plan)
    # Issue 4: art direction is deliberately applied after continuity so both
    # newly generated and carried elements receive the same token-free graphic
    # language. It never changes coordinates or semantic content.
    visual_plan = repair_post_continuity_plan(
        visual_plan,
        spoken_context=" ".join(filter(None, (narration, visual_prompt))),
    )
    visual_plan = normalize_visual_plan(visual_plan)
    visual_plan = apply_graphic_style(visual_plan)
    visual_plan["_explanation"] = assess_visual_anchor_coverage(
        visual_plan, scene
    )
    visual_plan["_quality_gate"] = evaluate_pre_render_quality_gate(
        visual_plan, scene
    )
    if visual_plan["_quality_gate"].get("status") == "fail":
        original = visual_plan
        # Continuity is optional presentation state. If carrying a prior scene
        # creates a defect, retain the rich scene without that carry before
        # considering the provider-independent rescue diagram.
        independent = materialize_static_visual_state(pre_continuity_plan)
        independent = repair_post_continuity_plan(
            independent, spoken_context=str(scene.get("narration", ""))
        )
        independent = normalize_visual_plan(independent)
        independent = apply_graphic_style(independent)
        independent["_continuity"] = {
            "applied": False,
            "reason": "continuity_removed_to_preserve_rich_scene",
        }
        independent["_explanation"] = assess_visual_anchor_coverage(
            independent, scene
        )
        independent["_quality_gate"] = evaluate_pre_render_quality_gate(
            independent, scene
        )
        fallback = semantic_fallback_plan(
            visual_prompt, scene.get("semantic_contract")
        )
        fallback = repair_post_continuity_plan(fallback, spoken_context=narration)
        fallback = normalize_visual_plan(fallback)
        fallback = apply_graphic_style(fallback)
        fallback["_explanation"] = assess_visual_anchor_coverage(fallback, scene)
        fallback["_quality_gate"] = evaluate_pre_render_quality_gate(fallback, scene)
        registry = CandidateRegistry(
            scene.get("semantic_contract")
            if isinstance(scene.get("semantic_contract"), dict) else {}
        )
        registry.add("post_continuity", "post_continuity", original)
        registry.add("rich_without_continuity", "continuity_rollback", independent)
        registry.add("semantic_fallback", "active_gate_repair", fallback)
        selected = registry.best()
        quality_shadow_mode = os.environ.get(
            "VISUAL_QUALITY_SHADOW_MODE", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        active_gate_enabled = os.environ.get(
            "VISUAL_ACTIVE_QUALITY_GATE", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if selected and active_gate_enabled and not quality_shadow_mode:
            visual_plan = selected["plan"]
            visual_plan["_candidate_selection"] = {
                "selected": selected["candidate_id"], "quality": selected["quality"],
                "candidate_count": len(registry.candidates),
                "reason": "active_post_continuity_gate",
            }
        log_local_transform(
            job_id, "active_post_continuity_gate", scene_id=scene_id,
            candidate_id=(selected["candidate_id"] if selected else "post_continuity"),
            before=original, after=visual_plan,
            details={
                "shadow_mode": quality_shadow_mode,
                "active_gate_enabled": active_gate_enabled,
                "scores": [item["quality"] for item in registry.candidates],
            }, status=(
                "shadow" if quality_shadow_mode or not active_gate_enabled else "repaired"
            ),
        )
    if runtime is not None:
        runtime_meta = runtime.diagnostics()
        visual_plan["_runtime"] = {
            key: runtime_meta[key]
            for key in (
                "fingerprint", "quality_mode", "planner_model", "style",
                "audience",
                "captions", "word_timestamps", "diagrams", "camera_enabled",
                "camera_seed", "isolation_scope",
            )
        }

    # Render only after continuity reconciliation; otherwise the SVG/PNG would
    # still contain the planner's independent coordinates.
    try:
        svg, icons = render_visual_plan(visual_plan, sketch=sketch)
    except Exception as render_error:
        failed_plan = visual_plan
        visual_plan = semantic_fallback_plan(
            visual_prompt, scene.get("semantic_contract")
        )
        visual_plan = repair_post_continuity_plan(visual_plan, spoken_context=narration)
        visual_plan = normalize_visual_plan(visual_plan)
        visual_plan = apply_graphic_style(visual_plan)
        visual_plan["_explanation"] = assess_visual_anchor_coverage(visual_plan, scene)
        visual_plan["_quality_gate"] = evaluate_pre_render_quality_gate(visual_plan, scene)
        svg, icons = render_visual_plan(visual_plan, sketch=False)
        log_local_transform(
            job_id, "stream_svg_render_exception", scene_id=scene_id,
            candidate_id="semantic_fallback", before=failed_plan, after=visual_plan,
            details={"error": str(render_error)[:2000]}, status="repaired",
        )
    icon_provenance = (
        visual_plan.get("_graphic_style", {}).get("icon_resolutions", [])
        if isinstance(visual_plan.get("_graphic_style"), dict) else []
    )
    if icons or icon_provenance:
        log_local_transform(
            job_id, "icon_resolution", scene_id=scene_id,
            candidate_id=str(
                visual_plan.get("_candidate_selection", {}).get("selected", "final")
            ), after={"resolved_icons": icons, "provenance": icon_provenance},
            details={"resolved_count": len(icons)}, status="ok",
        )

    svg_path = seg_dir / "scene.svg"
    png_path = seg_dir / "scene.png"
    visual_plan_path = seg_dir / "visual_plan.json"
    svg_path.write_text(svg, encoding="utf-8")
    visual_plan_path.write_text(
        json.dumps(visual_plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Give scene 1 exclusive access to the expensive local raster/encode path.
    # Later scenes have already completed their remote planning by this point
    # and resume together as soon as the opening segment becomes playable.
    if scene_id > 1 and opening_scene_ready is not None:
        opening_scene_ready.wait(timeout=max(
            1.0, float(os.environ.get("OPENING_SCENE_RENDER_PRIORITY_SECONDS", "45"))
        ))
    svg_to_png(svg, png_path)

    # Visual planning overlaps Whisper, but animation cannot consume word
    # timing until words.json exists. Bound the wait so a provider outage never
    # stalls rendering indefinitely; successful calls normally finish while
    # the visual planner is running.
    if word_thread is not None:
        word_thread.join(
            timeout=max(
                1.0,
                float(os.environ.get("WORD_TIMESTAMPS_WAIT_SECONDS", "12")),
            )
        )

    # ---- object-level animation (optional LLM-typed animation list) -------
    animations: List[Dict[str, Any]] = []
    if ANIM_ENABLED:
        try:
            animations = parse_animations_from_plan(visual_plan, duration)
        except Exception as anim_exc:
            print(f"Scene {scene_id} animation parsing skipped: {anim_exc}")

    # A token-free timing gate replaces a missing or under-specified animation
    # list. One weak planner animation must not leave several spoken beats on a
    # static frame.
    beat_rows = [
        beat for beat in scene.get("narration_beats", [])
        if isinstance(beat, dict) and str(beat.get("narration", "")).strip()
    ]
    beat_count = max(1, len(beat_rows))
    synthesized_for_beats = False
    elements = visual_plan.get("elements", [])
    meaningful_types = {"icon", "circle", "ellipse", "rect", "line", "arrow", "path", "polyline", "polygon"}
    meaningful_covered = {
        str(animation.get("_beat_id", ""))
        for animation in animations
        if 0 <= int(animation.get("e", -1)) < len(elements)
        and str(elements[int(animation["e"])].get("type", "")).lower() in meaningful_types
    }
    required_beat_ids = {
        str(beat.get("beat_id", f"beat_{index + 1}"))
        for index, beat in enumerate(beat_rows)
    }
    if ANIM_ENABLED and (
        len(animations) < beat_count
        or not required_beat_ids.issubset(meaningful_covered)
    ):
        if elements:
            from animated_scene import FALLBACK_DURATIONS
            n = len(elements)
            synth = []
            if visual_plan.get("source") == "diagram":
                # Diagram scenes: nodes fade layer-by-layer, edges draw after
                # their source node — preserves the explanatory order the
                # auto-layout computed.
                nodes_idx = []
                edges_idx = []
                label_idx = set()
                for i, e in enumerate(elements):
                    if e.get("id", "").startswith("diag_"):
                        # diag ids alternate node/label and edge/arrowhead
                        pass
                    kind = str(e.get("type", "")).lower()
                    if kind == "text":
                        label_idx.add(i)
                    elif kind in {"path", "polygon"}:
                        edges_idx.append(i)
                    else:
                        nodes_idx.append(i)
                phase_nodes = duration * 0.5
                for j, i in enumerate(nodes_idx):
                    stagger = (j / max(1, len(nodes_idx))) * phase_nodes
                    synth.append({
                        "t": round(0.2 + stagger, 3),
                        "e": i,
                        "a": "pop",
                        "d": 0.35,
                    })
                # labels follow their nodes immediately, edges draw after
                t0_edges = 0.2 + phase_nodes
                for j, i in enumerate(sorted(label_idx)):
                    stagger = (j / max(1, len(label_idx))) * phase_nodes * 0.9
                    synth.append({
                        "t": round(0.3 + stagger, 3),
                        "e": i,
                        "a": "fade",
                        "d": 0.25,
                    })
                for j, i in enumerate(edges_idx):
                    stagger = (j / max(1, len(edges_idx))) * max(0.4, duration * 0.3)
                    synth.append({
                        "t": round(min(t0_edges, duration - 0.9) + stagger, 3),
                        "e": i,
                        "a": "draw",
                        "d": 0.55,
                    })
            else:
                for i, e in enumerate(elements):
                    if bool(e.get("persistent")):
                        continue
                    kind = str(e.get("type", "")).lower()
                    a = "draw" if kind in {"line", "arrow", "path", "polyline"} else "fade"
                    stagger = (i / max(1, n)) * max(0.4, duration * 0.6)
                    synth.append({
                        "t": round(0.15 + stagger, 3),
                        "e": i,
                        "a": a,
                        "d": FALLBACK_DURATIONS.get(a, 0.5),
                    })
            animations = synth
            synthesized_for_beats = True
            element_beats = _semantic_element_beat_map(elements, beat_rows)
            for animation in animations:
                phase = element_beats.get(int(animation.get("e", 0)), 0)
                animation["_beat_id"] = (
                    str(beat_rows[phase].get("beat_id", f"beat_{phase + 1}"))
                    if beat_rows else f"beat_{phase + 1}"
                )
            # If a text-heavy composition has fewer drawable marks than
            # narration beats, revisit an existing subject with a pulse.  This
            # keeps each spoken beat tied to a visible object instead of
            # satisfying coverage with a late label alone.
            meaningful_after = {
                str(animation.get("_beat_id", ""))
                for animation in animations
                if str(elements[int(animation.get("e", 0))].get("type", "")).lower()
                in meaningful_types
            }
            drawable = [
                index for index, element in enumerate(elements)
                if str(element.get("type", "")).lower() in meaningful_types
            ]
            for beat_index, beat in enumerate(beat_rows):
                beat_id = str(beat.get("beat_id", f"beat_{beat_index + 1}"))
                if beat_id in meaningful_after or not drawable:
                    continue
                target = min(drawable, key=lambda index: abs(index - beat_index))
                animations.append({
                    "t": 0.1,
                    "e": target,
                    "a": "pulse",
                    "d": FALLBACK_DURATIONS["pulse"],
                    "_beat_id": beat_id,
                })
            print(f"Scene {scene_id}: synthesized {len(synth)} deterministic animations.")
    anim_mode = "none"
    if animations:
        anim_mode = "animated"
        beat_ids = [
            str(beat.get("beat_id", f"beat_{index + 1}"))
            for index, beat in enumerate(beat_rows)
        ]
        beat_weights = [
            max(1, len(str(beat.get("narration", "")).split()))
            for beat in beat_rows
        ]
        beat_weight_total = max(1, sum(beat_weights))
        beat_starts = [sum(beat_weights[:index]) / beat_weight_total for index in range(len(beat_weights))]
        beat_ends = [sum(beat_weights[:index + 1]) / beat_weight_total for index in range(len(beat_weights))]
        for order, a in enumerate(animations):
            if str(a.get("_beat_id", "")) not in beat_ids:
                phase = min(
                    beat_count - 1,
                    int((order / max(1, len(animations))) * beat_count),
                )
                a["_beat_id"] = (
                    str(beat_rows[phase].get("beat_id", f"beat_{phase + 1}"))
                    if beat_rows else f"beat_{phase + 1}"
                )
            phase = beat_ids.index(str(a.get("_beat_id"))) if beat_ids else 0
            a["_beat_start_fraction"] = beat_starts[phase] if beat_starts else 0
            a["_beat_end_fraction"] = beat_ends[phase] if beat_ends else 1
            if "w" not in a and beat_rows and not synthesized_for_beats:
                anchor_words = [
                    token for token in re.findall(
                        r"[a-z0-9]+", str(beat_rows[phase].get("narration", "")).casefold()
                    )
                    if len(token) >= 3 and token not in _BEAT_STOP_WORDS
                ][:3]
                if anchor_words:
                    a["w"] = anchor_words
            a["_element"] = visual_plan.get("elements", [{}])[a["e"]]

        # A small late emphasis keeps each beat visually active through the
        # corresponding narration instead of revealing everything up front.
        drawable = [
            index for index, element in enumerate(elements)
            if str(element.get("type", "")).lower() in meaningful_types
        ]
        for beat_index, beat_id in enumerate(beat_ids):
            if not drawable:
                break
            target = drawable[min(beat_index, len(drawable) - 1)]
            animations.append({
                "t": 0.1,
                "e": target,
                "a": "pulse",
                "d": 0.55,
                "_beat_id": beat_id,
                "_beat_start_fraction": beat_starts[beat_index] if beat_starts else 0,
                "_beat_end_fraction": beat_ends[beat_index] if beat_ends else 1,
                "_element": elements[target],
                "_late_emphasis": True,
            })
    covered_beat_ids = sorted({
        str(animation.get("_beat_id", "")).strip()
        for animation in animations if str(animation.get("_beat_id", "")).strip()
    })
    meaningful_covered_beat_ids = sorted({
        str(animation.get("_beat_id", "")).strip()
        for animation in animations
        if str(animation.get("_beat_id", "")).strip()
        and 0 <= int(animation.get("e", -1)) < len(elements)
        and str(elements[int(animation["e"])].get("type", "")).lower() in meaningful_types
    })
    visual_plan["_beat_sync"] = {
        "version": 1,
        "narration_beat_count": len(beat_rows),
        "animation_count": len(animations),
        "covered_beat_ids": covered_beat_ids,
        "meaningful_covered_beat_ids": meaningful_covered_beat_ids,
        "coverage_pass": not beat_rows or required_beat_ids.issubset(
            set(meaningful_covered_beat_ids)
        ),
        "strategy": "deterministic_beat_windows" if synthesized_for_beats else "planner_mapped_to_beat_windows",
    }
    animation_renderer_ready = bool(
        anim_mode == "animated" and fast_svg_animation_available()
    )
    visual_plan["_beat_sync"]["rendered"] = animation_renderer_ready
    if anim_mode == "animated" and not animation_renderer_ready:
        visual_plan["_beat_sync"]["fallback_reason"] = (
            "fast_svg_rasterizer_unavailable_static_render_used"
        )
        anim_mode = "static_svg_rasterizer_unavailable"
    visual_plan_path.write_text(
        json.dumps(visual_plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    svg_anim_path = seg_dir / "scene_animated.svg"
    if anim_mode == "animated":
        svg_anim_path.write_text(svg, encoding="utf-8")

    _emit(
        socketio,
        "scene_progress",
        {"job_id": job_id, "scene_id": scene_id, "stage": "visual_done", "icons": icons},
    )

    # ---- segment MP4 --------------------------------------------------------
    mp4 = seg_dir / "segment.mp4"
    captions_override = runtime.captions if runtime is not None else None

    # ---- feature D: camera spec for this scene --------------------------------
    # Planner may add an optional 'camera' field per scene (~20 tokens/scene).
    # The renderer applies it as a 2D drift/zoom; static scenes fall back to
    # ffmpeg zoompan (same motion, no re-rasterization).
    from camera_engine import sanitize_camera_spec
    _cam = sanitize_camera_spec(visual_plan.get("camera")) if visual_plan else None
    _cam = _cam if _cam else None
    # Audio-length invariant: the narration wav for this scene (raw, tempo
    # manipulation is disabled) is measured HERE and the render paths are required to
    # produce a segment whose audio track is >= the narration length. When a
    # pipeline bug or a fallback path breaks that invariant, re-render once
    # through the reliable static path so the narration is never truncated.
    narration_audio_seconds = audio_duration_seconds(wav)
    static_state_paths: List[Path] = []
    if not animation_renderer_ready and len(beat_rows) > 1:
        try:
            states = compile_static_beat_states(beat_source_plan, beat_rows)
            beat_dir = seg_dir / "beat_states"
            if states:
                beat_dir.mkdir(parents=True, exist_ok=True)
            for index, state in enumerate(states, start=1):
                state = repair_post_continuity_plan(
                    state,
                    spoken_context=" ".join(filter(None, (narration, visual_prompt))),
                )
                state = normalize_visual_plan(state)
                state = apply_graphic_style(state)
                state_svg, _ = render_visual_plan(state, sketch=False)
                state_svg_path = beat_dir / f"beat_{index:02d}.svg"
                state_png_path = beat_dir / f"beat_{index:02d}.png"
                state_svg_path.write_text(state_svg, encoding="utf-8")
                svg_to_png(state_svg, state_png_path)
                static_state_paths.append(state_png_path)
        except Exception as static_state_error:  # noqa: BLE001
            print(
                f"Scene {scene_id} static beat-state render failed "
                f"({static_state_error}); using final static render."
            )
            static_state_paths = []
            visual_plan["_beat_sync"]["static_state_error"] = str(static_state_error)[:1000]
    if anim_mode == "animated":
        try:
            render_animated_segment(
                svg, visual_plan.get("elements", []), animations, wav, mp4,
                narration=narration, caption_text=narration,
                camera_spec=_cam, job_seed=runtime.camera_seed if runtime is not None else 0,
            )  # captions use CAPTIONS_ENABLED (env or per-job override)
        except Exception as anim_render_exc:
            print(
                f"Scene {scene_id} animated render failed "
                f"({anim_render_exc}); falling back to static."
            )
            render_scene_video_fast(png_path, wav, mp4, caption_text=narration, captions=captions_override, camera_spec=_cam)
            anim_mode = "static_fallback"
    elif len(static_state_paths) > 1:
        weights = [
            max(1, len(str(beat.get("narration", "")).split()))
            for beat in beat_rows
        ]
        weight_total = sum(weights)
        state_durations = [duration * weight / weight_total for weight in weights]
        render_scene_video_sequence(
            static_state_paths, state_durations, wav, mp4,
            caption_text=narration, captions=captions_override,
        )
        visual_plan["_beat_sync"].update({
            "rendered": True,
            "strategy": "cumulative_static_beat_states",
            "rendered_static_state_count": len(static_state_paths),
            "fallback_reason": None,
        })
        anim_mode = "static_beat_states"
    else:
        render_scene_video_fast(png_path, wav, mp4, caption_text=narration, captions=captions_override, camera_spec=_cam)
    visual_plan_path.write_text(
        json.dumps(visual_plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- invariant check: segment audio must cover the full narration ------
    try:
        seg_audio = audio_duration_seconds(mp4)
        if seg_audio + 0.4 < narration_audio_seconds:
            print(
                f"Scene {scene_id}: segment audio {seg_audio:.2f}s is shorter than "
                f"narration {narration_audio_seconds:.2f}s (narration would be "
                "truncated) — re-rendering the segment with the narration wav."
            )
            render_scene_video_fast(
                png_path, wav, mp4, caption_text=narration, captions=captions_override,
            )
            seg_audio = audio_duration_seconds(mp4)
            anim_mode = f"{anim_mode}_audio_fixed"
        _emit(
            socketio,
            "scene_progress",
            {
                "job_id": job_id,
                "scene_id": scene_id,
                "stage": "encoded",
                "video_seconds": round(audio_duration_seconds(mp4), 3),
                "audio_seconds": round(seg_audio, 3),
                "narration_seconds": round(narration_audio_seconds, 3),
                "anim_mode": anim_mode,
            },
        )
    except Exception as inv_exc:
        print(f"Scene {scene_id} segment invariant check failed: {inv_exc}")

    # Append this scene to the live stream.mp4 and re-emit with the new URL.
    try:
        total = 0.0
        if assembler is not None:
            total = assembler.add_scene(mp4, scene_id=scene_id)
        result_patch = {
            "stream_url": f"/segments/{job_id}/stream.mp4",
            "stream_total_seconds": round(max(0.0, total), 3),
        }
    except Exception as assembler_error:
        print(f"Scene {scene_id} live-assembly failed (continuing): {assembler_error}")
        result_patch = {}

    # ---- animated timeline metadata for the stream player -------------------
    timeline = _make_animation_timeline(
        visual_plan,
        duration,
        animations=animations,
        narration=narration,
        scene_dir=seg_dir,
    )
    timeline_path = seg_dir / "timeline.json"
    timeline_path.write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")

    result = {
        "scene_id": scene_id,
        "duration_seconds": float(duration),
        "raw_duration_seconds": float(raw_duration),
        "boundary_pause_seconds": float(padding["boundary_pause_seconds"]),
        "duration_overshoot_seconds": float(padding["overshoot_seconds"]),
        "svg_url": f"/segments/{job_id}/scene_{scene_id:03d}/scene.svg",
        "png_url": f"/segments/{job_id}/scene_{scene_id:03d}/scene.png",
        "wav_url": f"/segments/{job_id}/scene_{scene_id:03d}/narration.wav",
        "mp4_url": f"/segments/{job_id}/scene_{scene_id:03d}/segment.mp4",
        "timeline_url": f"/segments/{job_id}/scene_{scene_id:03d}/timeline.json",
        "visual_plan_url": f"/segments/{job_id}/scene_{scene_id:03d}/visual_plan.json",
        "narration": narration,
        "visual_prompt": visual_prompt,
        "icons": icons,
        "predicted_seconds": estimate_text_seconds(narration),
        "selected_beat_ids": scene.get("selected_beat_ids", []),
        "anim_mode": anim_mode,
        "animation_count": len(animations),
        "beat_sync": visual_plan.get("_beat_sync", {}),
    }
    result.update(result_patch)

    _emit(
        socketio,
        "segment_ready",
        {"job_id": job_id, "segment": result},
    )
    return result


def _make_animation_timeline(
    visual_plan: Dict[str, Any],
    total_seconds: float,
    animations: Optional[List[Dict[str, Any]]] = None,
    narration: str = "",
    scene_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Convert a static visual plan into a staggered animation timeline.

    Elements fade/draw in across the scene. Timing is proportional to
    narration length so playback feels synced to the voice.
    """
    elements = visual_plan.get("elements", [])
    if animations:
        from animated_scene import narrate_align_animations
        from tts_engine import load_word_timestamps

        words = load_word_timestamps(scene_dir) if scene_dir else None
        aligned = narrate_align_animations(
            animations, total_seconds, narration, words=words
        )
        entries = []
        for animation in aligned:
            index = int(animation.get("e", 0))
            element = elements[index] if 0 <= index < len(elements) else {}
            entries.append({
                "index": index,
                "type": str(element.get("type", "")).lower(),
                "animation": str(animation.get("a", "fade")),
                "appear_at_seconds": round(float(animation.get("t", 0)), 3),
                "duration_seconds": round(float(animation.get("d", 0.4)), 3),
                "layer": int(element.get("layer", 2)),
            })
        return {
            "total_seconds": round(total_seconds, 3),
            "word_aligned": bool(words),
            "word_count": len(words or []),
            "elements": entries,
        }

    n = len(elements)
    animated = []
    layer_times: Dict[int, float] = {0: 0.0, 1: 0.15, 2: 0.30, 3: 0.45}
    for i, e in enumerate(elements):
        layer = int(e.get("layer", 2))
        base = layer_times.get(layer, 0.3)
        # Without animation metadata, still span the spoken scene rather than
        # exhausting every visual event halfway through narration.
        stagger = (i / max(1, n - 1)) * max(0.2, total_seconds * 0.85)
        appear = min(total_seconds * 0.9, base + stagger)
        kind = str(e.get("type", "")).lower()
        animation = "draw" if kind in {"line", "arrow", "path", "polyline"} else "fade"
        animated.append(
            {
                "index": i,
                "type": kind,
                "animation": animation,
                "appear_at_seconds": round(appear, 3),
                "duration_seconds": 0.35 if animation == "fade" else 0.5,
                "layer": layer,
            }
        )
    return {
        "total_seconds": round(total_seconds, 3),
        "word_aligned": False,
        "word_count": 0,
        "elements": animated,
    }


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def run_streaming_pipeline(
    job_id: str,
    payload: Dict[str, Any],
    socketio: Optional[Any] = None,
    plan: Optional[Dict[str, Any]] = None,
    segment_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    plan_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    source_text: str = "",
    full_source_text: str = "",
    style: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the overlapped pipeline for one job; return the completed result.

    `plan` may be supplied pre-built (e.g. from a cached/streaming director
    run); otherwise it is created here.
    """
    prompt = str(payload["prompt"]).strip()
    target_seconds = float(payload["duration_seconds"])
    audience = str(payload.get("audience", "general learner")).strip() or "general learner"
    sketch = bool(payload.get("sketch", False))
    runtime = JobRuntimeConfig.from_payload(
        job_id,
        payload,
        style=style,
        default_planner_model=PLANNER_MODEL,
    )
    style = runtime.style

    job_dir = WORK_DIR / job_id
    segments_dir = job_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "runtime_diagnostics.json").write_text(
        json.dumps(runtime.diagnostics(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Live, watchable MP4 that grows with every finished scene. The browser
    # plays THIS single video (real controls, sequential playback, no audio
    # collisions), while the per-scene assets remain available for replay.
    assembler = IncrementalVideoAssembler(job_dir)

    # Persist the live stream info next to the job dir so the app layer
    # (and rejoin replays) can find it without importing this module.
    stream_info = {
        "stream_url": f"/segments/{job_id}/stream.mp4",
        "stream_path": str(assembler.stream_path),
    }
    (job_dir / "stream_info.json").write_text(
        json.dumps(stream_info), encoding="utf-8"
    )

    # ---- planning -----------------------------------------------------------
    _emit(socketio, "job_stage", {"job_id": job_id, "stage": "planning", "detail": "Writing the explanation"})
    if plan is None:
        plan = create_video_plan(
            prompt, target_seconds, audience,
            source_text=source_text, job_id=job_id,
        )

    scenes = plan["scenes"]

    if plan_cb is not None:
        try:
            plan_cb(plan)
        except Exception as exc:
            print(f"Plan callback failed: {exc}")

    # ---- document anchors: split the source into per-scene excerpts ----------
    # Constant cost: each planner call sees only its own short excerpt
    # (<=600 words), not the whole document.
    doc_excerpts: List[str] = []
    excerpt_source = full_source_text or source_text
    if excerpt_source:
        words = excerpt_source.split()
        per_scene_words = 600
        for i in range(len(scenes)):
            start = i * per_scene_words
            chunk = words[start: start + per_scene_words]
            doc_excerpts.append(" ".join(chunk) if chunk else "")
    for i, excerpt in enumerate(doc_excerpts):
        if i < len(scenes):
            scenes[i]["doc_excerpts"] = excerpt

    # ---- adaptive element budget (shared across all scenes) ------------------
    # Total tokens saved: the planner no longer makes every scene heavy.
    # Important scenes (scored by the director) spend more; simple ones less.
    n_scenes = len(scenes)
    global_budget = int(os.environ.get("VIDEO_ELEMENT_BUDGET", "80"))
    # Feature E: fast mode runs with a 40% smaller scene budget; the director
    # (one call per video) is untouched, so the saving is on the many
    # per-scene planner calls.
    if runtime.quality_mode == "fast":
        global_budget = max(24, int(global_budget * 0.6))
    base_share = max(6, round(global_budget / max(1, n_scenes)))
    for s in scenes:
        importance = float(s.get("importance", 0.5))
        lo = max(5, base_share - 4)
        hi = min(24, base_share + 6)
        share = base_share + (importance - 0.5) * (hi - lo)
        s["element_budget"] = {
            "scene_count": n_scenes,
            "total": global_budget,
            "allocated": max(lo, min(hi, round(share))),
            "min": lo,
            "max": hi,
        }

    # Duration compiler already assigned exact per-scene targets. Preserve them
    # so batch and streaming use the same narration/visual contract.
    total_words = sum(len(str(s.get("narration", "")).split()) for s in scenes)
    total_predicted = estimate_seconds(total_words)

    # Each scene now enters TTS, visual planning, and encoding immediately in
    # its own worker. Waiting for every TTS file here delayed scene 1 behind the
    # slowest narration even though its own audio was already usable. The
    # director's exact scene targets still control boundary padding.
    for scene in scenes:
        scene["allocated_scene_seconds"] = round(
            float(scene.get("target_scene_seconds", 0) or 0), 4
        )

    _emit(
        socketio,
        "plan_ready",
        {
            "job_id": job_id,
            "plan_summary": {
                "title": plan.get("title"),
                "scene_count": len(scenes),
                "narration_words": total_words,
                "predicted_total_seconds": round(total_predicted, 2),
            },
        },
    )

    _emit(socketio, "job_stage", {"job_id": job_id, "stage": "rendering", "detail": f"Building {len(scenes)} scenes in parallel"})

    # ---- overlapped scene building ------------------------------------------
    segments: List[Dict[str, Any]] = [None] * len(scenes)  # type: ignore[list-item]
    lock = threading.Lock()
    completed = [0]
    recovered = [0]
    unrecoverable: List[Dict[str, Any]] = []
    continuity = ContinuityCoordinator()
    opening_scene_ready = threading.Event()

    # Piper loads a large voice model for every process. A small FIFO pool gets
    # the opening scenes ready first and avoids starting one memory-heavy model
    # per scene. Visual planners still run for every scene in parallel inside
    # their scene workers, so remote I/O overlaps this local work.
    tts_workers = max(1, min(
        len(scenes), int(os.environ.get("TTS_WORKERS", "2"))
    ))

    def prepare_tts(scene: Dict[str, Any]) -> tuple[Path, float]:
        scene_id = int(scene["id"])
        seg_dir = segments_dir / f"scene_{scene_id:03d}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        raw_path = seg_dir / "narration_raw.wav"
        with activate_runtime(runtime):
            if not raw_path.is_file():
                synthesize(str(scene["narration"]).strip(), raw_path, job_id=job_id)
            return raw_path, audio_duration_seconds(raw_path)

    tts_pool = ThreadPoolExecutor(max_workers=tts_workers)
    for scene in scenes:
        scene["_prepared_tts_future"] = tts_pool.submit(prepare_tts, scene)

    def worker(scene: Dict[str, Any]):
        try:
            with activate_runtime(runtime):
                seg_info = _build_segment(
                    job_id, scene, segments_dir, job_dir, sketch, socketio,
                    assembler=assembler, style=style, continuity=continuity,
                    runtime=runtime,
                    opening_scene_ready=opening_scene_ready,
                )
        except Exception as scene_error:  # noqa: BLE001 - isolate scene failures
            scene_id = int(scene.get("id", 0) or 0)
            traceback.print_exc()
            _emit(
                socketio,
                "scene_progress",
                {
                    "job_id": job_id,
                    "scene_id": scene_id,
                    "stage": "recovering",
                    "detail": "Normal scene rendering failed; using the local safety renderer",
                },
            )
            try:
                with activate_runtime(runtime):
                    seg_info = _build_emergency_segment(
                        job_id, scene, segments_dir, assembler, runtime, scene_error
                    )
                with lock:
                    recovered[0] += 1
                log_local_transform(
                    job_id,
                    "scene_failure_recovery",
                    scene_id=scene_id,
                    candidate_id="local_emergency_segment",
                    details={"error": f"{type(scene_error).__name__}: {scene_error}"[:1000]},
                    status="recovered",
                )
            except Exception as recovery_error:  # noqa: BLE001
                traceback.print_exc()
                if scene_id == 1:
                    opening_scene_ready.set()
                with lock:
                    unrecoverable.append({
                        "scene_id": scene_id,
                        "normal_error": f"{type(scene_error).__name__}: {scene_error}"[:1000],
                        "recovery_error": f"{type(recovery_error).__name__}: {recovery_error}"[:1000],
                    })
                return None
        if int(scene.get("id", 0) or 0) == 1:
            opening_scene_ready.set()
        with lock:
            completed[0] += 1
            segments[int(scene["id"]) - 1] = seg_info
            _emit(
                socketio,
                "job_stage",
                {
                    "job_id": job_id,
                    "stage": "rendering",
                    "detail": f"Scene {completed[0]}/{len(scenes)} ready",
                    "progress": round(60 * completed[0] / len(scenes)),
                },
            )
        if segment_cb is not None:
            try:
                segment_cb(seg_info)
            except Exception as exc:
                print(f"Segment callback failed: {exc}")
        return seg_info

    try:
        with ThreadPoolExecutor(max_workers=min(SCENE_WORKERS, max(1, len(scenes)))) as pool:
            list(pool.map(worker, scenes))
    finally:
        tts_pool.shutdown(wait=True, cancel_futures=False)

    # Emergency scenes remain valid segments; only a failure of both the
    # normal and local-only paths leaves a None here.
    segments = [s for s in segments if s]

    if unrecoverable or len(segments) != len(scenes):
        raise RuntimeError(
            "One or more scenes could not be rendered after local recovery: "
            + json.dumps(unrecoverable, ensure_ascii=False)
        )

    # ---- calibration feedback ------------------------------------------------
    durations = [s["duration_seconds"] for s in segments]
    final_total = sum(durations)
    _record_duration_observation(job_dir, plan, segments)
    for scene in scenes:
        scene.pop("_prepared_raw_wav", None)
        scene.pop("_prepared_raw_duration", None)
        scene.pop("_prepared_tts_future", None)
    _emit(
        socketio,
        "job_stage",
        {"job_id": job_id, "stage": "calibration", "detail": "Updating duration model"},
    )

    # ---- final assembly --------------------------------------------------------
    _emit(socketio, "job_stage", {"job_id": job_id, "stage": "finalizing", "detail": "Assembling final MP4"})

    if not segments:
        raise RuntimeError(
            "No scenes could be built (every scene failed — most likely all API "
            "keys are rate-limited right now). The job failed gracefully; "
            "retrying later or adding more keys to GROQ_API_KEYS will resolve it."
        )

    final_mp4 = assembler.finish()
    if not Path(final_mp4).exists():
        raise RuntimeError(
            f"Live assembly did not produce {final_mp4} (every per-scene append "
            "failed). Check the log above for per-scene errors."
        )
    # Stable finalized location used by the app, download links, and late
    # viewers.  It is refreshed again below if duration correction produces a
    # different file.
    legacy_final = job_dir / "final.mp4"
    if Path(final_mp4).resolve() != legacy_final.resolve():
        import shutil
        shutil.copy2(final_mp4, legacy_final)
    # The assembled stream is already synchronized scene by scene. Never
    # stretch only its audio to hit the requested wall-clock target: doing so
    # makes narration progressively drift from the visuals and can leave an
    # audio-only tail. Duration calibration adjusts future scripts instead.
    actual_final = audio_duration_seconds(final_mp4)

    # Always publish the file represented by final_duration_seconds.  The old
    # code left final.mp4 pointing at the pre-correction stream while reporting
    # the corrected file's duration.
    if Path(final_mp4).resolve() != legacy_final.resolve():
        import shutil
        shutil.copy2(final_mp4, legacy_final)
    final_mp4 = legacy_final

    result = {
        "job_id": job_id,
        "status": "completed",
        "final_duration_seconds": actual_final,
        "target_duration_seconds": target_seconds,
        "scene_count": len(segments),
        "segments": segments,
        "calibration": calibration_summary(),
        "runtime_config": runtime.diagnostics(),
        "duration_integrity": {
            "audio_source": "raw_tts_plus_boundary_silence",
            "tempo_adjustment_applied": False,
            "calibration_samples_raw_only": True,
            "tts_passes_per_scene": 1,
            "semantic_revisions": 0,
            "recovered_scene_count": recovered[0],
            "raw_speech_seconds": round(sum(float(s.get("raw_duration_seconds", 0)) for s in segments), 3),
            "boundary_pause_seconds": round(sum(float(s.get("boundary_pause_seconds", 0)) for s in segments), 3),
            "target_error_seconds": round(actual_final - target_seconds, 3),
            "target_error_ratio": round(abs(actual_final - target_seconds) / max(0.1, target_seconds), 6),
        },
        "output_url": f"/outputs/{job_id}/final.mp4",
        "final_path": str(final_mp4),
        "stream_url": f"/segments/{job_id}/stream.mp4",
        "stream_path": str(assembler.stream_path),
        "plan": plan,
    }

    (job_dir / "duration_diagnostics.json").write_text(
        json.dumps({
            **result["duration_integrity"],
            "duration_compiler": plan.get("duration_compiler", {}),
            "scene_audio": [{
                "scene_id": segment.get("scene_id"),
                "selected_beat_ids": segment.get("selected_beat_ids", []),
                "raw_speech_seconds": segment.get("raw_duration_seconds"),
                "boundary_pause_seconds": segment.get("boundary_pause_seconds", 0),
                "final_audio_seconds": segment.get("duration_seconds"),
            } for segment in segments],
        }, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    return result
