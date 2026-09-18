import copy
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, render_template, request, send_from_directory
from flask_socketio import SocketIO, disconnect, join_room
from werkzeug.utils import secure_filename

from observability import configure_logging, install_request_logging

# Windows services and redirected processes commonly expose a legacy CP1252
# console. LLM output is Unicode, so diagnostic prints must never be able to
# abort an otherwise successful generation request.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass

load_dotenv()
configure_logging()

ENABLE_STREAMING = (
    os.environ.get("USE_STREAMING", "1").strip().lower()
    in {"1", "true", "yes", "on"}
)

from director import create_video_plan, narration_word_count
from tts_engine import audio_duration_seconds, pad_wav_to_duration, resolve_backend, synthesize
from video_engine import (
    concat_scene_videos, render_scene_video, render_scene_video_sequence,
    svg_to_png, write_manifest,
)
from quality_report import write_canvas_evaluation
from explanation_quality import assess_visual_anchor_coverage
from continuity_engine import ContinuityCoordinator, continuity_planner_instruction
from graphics_style import apply_graphic_style
from freeform_repair import repair_post_continuity_plan
from visual_engine import (
    PLAN_REVIEW_MODE,
    PLANNER_MODEL,
    create_visual_plan,
    compile_static_beat_states,
    materialize_static_visual_state,
    normalize_visual_plan,
    render_visual_plan,
    beat_visual_timeline_instruction,
    evaluate_pre_render_quality_gate,
)
from job_runtime import JobRuntimeConfig, activate_runtime
from duration_calibrator import record_text_sample
from animated_scene import (
    ANIM_ENABLED,
    FALLBACK_DURATIONS,
    MAX_ANIMATIONS_PER_SCENE,
    parse_animations_from_plan,
    render_animated_segment,
    fast_svg_animation_available,
)
from visual_quality import CandidateRegistry, semantic_fallback_plan
from run_log import log_local_transform
from supabase_store import supabase_store


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", BASE_DIR / "outputs"))
WORK_DIR = Path(os.environ.get("WORK_DIR", BASE_DIR / "work"))
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)

MAX_WORKERS = max(
    1,
    int(os.environ.get("MAX_WORKERS", "4"))
)

MAX_VISUAL_WORKERS = max(
    1,
    int(os.environ.get("MAX_VISUAL_WORKERS", "2"))
)


def _salvage_single_render_element(
    plan: Dict[str, Any], *, sketch: bool,
) -> Optional[Tuple[Dict[str, Any], str, List[str], Dict[str, Any]]]:
    """Isolate one broken element while preserving the surrounding composition.

    A semantic label is substituted at the same location.  The search is
    bounded so a malformed decorative path cannot force replacement of an
    otherwise strong illustration.
    """
    elements = plan.get("elements") if isinstance(plan, dict) else None
    if not isinstance(elements, list):
        return None
    for index, offending in enumerate(elements[:24]):
        if not isinstance(offending, dict):
            continue
        candidate = copy.deepcopy(plan)
        candidate_elements = candidate.get("elements", [])
        removed = candidate_elements.pop(index)
        label = str(
            removed.get("text") or removed.get("query") or removed.get("id") or "item"
        ).replace("_", " ").strip()[:42]
        if label:
            candidate_elements.append({
                "type": "text", "id": f"salvaged_{removed.get('id', index)}",
                "text": label, "x": removed.get("x", 640),
                "y": removed.get("y", 360), "font_size": 18,
                "font_weight": "bold", "align": "center", "layer": 8,
                "semantic_refs": copy.deepcopy(removed.get("semantic_refs", [])),
                "label_for": removed.get("label_for"),
            })
        try:
            svg, icons = render_visual_plan(candidate, sketch=sketch)
        except Exception:
            continue
        candidate["_render_salvage"] = {
            "strategy": "single_element_semantic_substitution",
            "offending_element": copy.deepcopy(offending),
            "replacement_label": label,
        }
        return candidate, svg, icons, offending
    return None
try:
    from api_key_pool import recommended_provider_workers
    from llm_provider import get_llm_config

    MAX_VISUAL_WORKERS = recommended_provider_workers(
        get_llm_config("planner").provider, MAX_VISUAL_WORKERS
    )
except Exception:
    pass

DURATION_TOLERANCE_SECONDS = float(
    os.environ.get(
        "DURATION_TOLERANCE_SECONDS",
        "20",
    )
)

MAX_SCRIPT_REVISIONS = int(
    os.environ.get(
        "MAX_SCRIPT_REVISIONS",
        "3",
    )
)

app = Flask(__name__, template_folder="templates")
install_request_logging(app)

APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"
MAINTENANCE_MODE = os.environ.get("MAINTENANCE_MODE", "0").strip().lower() in {
    "1", "true", "yes", "on",
}
MAX_UPLOAD_BYTES = max(1, int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))))
MAX_PROMPT_CHARS = max(100, int(os.environ.get("MAX_PROMPT_CHARS", "4000")))
MAX_VIDEO_DURATION_SECONDS = max(20, int(os.environ.get("MAX_VIDEO_DURATION_SECONDS", "1800")))
GENERATION_RATE_LIMIT = max(1, int(os.environ.get("GENERATION_RATE_LIMIT", "10")))
GENERATION_RATE_WINDOW_SECONDS = max(1, int(os.environ.get("GENERATION_RATE_WINDOW_SECONDS", "3600")))
PASSWORD_RESET_RATE_LIMIT = max(1, int(os.environ.get("PASSWORD_RESET_RATE_LIMIT", "3")))
PASSWORD_RESET_RATE_WINDOW_SECONDS = max(
    60, int(os.environ.get("PASSWORD_RESET_RATE_WINDOW_SECONDS", "3600"))
)
JOB_EXECUTION_MODE = os.environ.get(
    "JOB_EXECUTION_MODE", "queue" if IS_PRODUCTION else "inline"
).strip().lower()
if JOB_EXECUTION_MODE not in {"inline", "queue"}:
    raise RuntimeError("JOB_EXECUTION_MODE must be 'inline' or 'queue'")
if IS_PRODUCTION and JOB_EXECUTION_MODE != "queue":
    raise RuntimeError("Production requires JOB_EXECUTION_MODE=queue")
if IS_PRODUCTION and not supabase_store.enabled:
    raise RuntimeError("Production requires SUPABASE_URL and SUPABASE_SECRET_KEY")

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + (256 * 1024)

_configured_origins = [
    value.strip() for value in os.environ.get(
        "ALLOWED_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
    ).split(",") if value.strip()
]
if IS_PRODUCTION and not _configured_origins:
    raise RuntimeError("ALLOWED_ORIGINS is required in production")

_media_signing_key = os.environ.get("MEDIA_SIGNING_KEY", "").encode("utf-8")
if IS_PRODUCTION and len(_media_signing_key) < 32:
    raise RuntimeError("MEDIA_SIGNING_KEY must contain at least 32 characters in production")
if not _media_signing_key:
    _media_signing_key = uuid.uuid4().hex.encode("ascii")


@app.before_request
def maintenance_gate():
    """Stop user traffic while preserving operational health endpoints."""
    if not MAINTENANCE_MODE or request.path in {"/healthz", "/readyz"}:
        return None

    wants_json = (
        request.path.startswith(("/api/", "/media/", "/outputs/", "/segments/"))
        or request.is_json
        or request.accept_mimetypes.best == "application/json"
    )
    if wants_json:
        response = jsonify({
            "error": "maintenance",
            "message": "PencilVid is temporarily unavailable while an update is applied.",
        })
    else:
        response = Response(render_template("maintenance.html"), mimetype="text/html")
    response.status_code = 503
    response.headers["Cache-Control"] = "no-store"
    response.headers["Retry-After"] = "300"
    return response

socketio = SocketIO(
    app,
    cors_allowed_origins=_configured_origins,
    async_mode="threading",
    logger=False,
    engineio_logger=False,
) if ENABLE_STREAMING else None

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_job_cancel_events: Dict[str, threading.Event] = {}
_job_cancel_lock = threading.Lock()


class JobCanceled(RuntimeError):
    """Raised cooperatively when a user cancels an active render."""


def _request_job_cancellation(job_id: str) -> None:
    with _job_cancel_lock:
        _job_cancel_events.setdefault(job_id, threading.Event()).set()


def _clear_job_cancellation(job_id: str) -> None:
    with _job_cancel_lock:
        _job_cancel_events.pop(job_id, None)


def _raise_if_job_canceled(job_id: str) -> None:
    with _job_cancel_lock:
        canceled = bool(
            job_id in _job_cancel_events and _job_cancel_events[job_id].is_set()
        )
    if canceled:
        raise JobCanceled("Video generation was canceled")


def _set_local_job_canceled(job_id: str) -> None:
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update({
            "status": "canceled",
            "stage": "canceled",
            "detail": "Canceled by user",
            "error": None,
        })

# Streaming-mode bookkeeping (segments queue + per-job plan cache).
_segment_state: Dict[str, Dict[str, Any]] = {}
_segment_state_lock = threading.Lock()

STREAM_PLAYER_ROUTE = "/stream" if ENABLE_STREAMING else None


def _auth_error_status(message: Optional[str]) -> int:
    return 503 if message and "temporarily unavailable" in message.lower() else 401


def _account_subject_hash(value: str) -> str:
    normalized = value.strip().lower().encode("utf-8")
    return hmac.new(_media_signing_key, normalized, hashlib.sha256).hexdigest()


def _audit_account_event(
    subject: str, event_type: str, outcome: str, metadata: Optional[Dict[str, Any]] = None,
) -> None:
    supabase_store.record_security_event(
        _account_subject_hash(subject),
        event_type,
        outcome,
        str(getattr(g, "request_id", "")),
        metadata,
    )


def _remove_local_account_jobs(job_ids: List[str]) -> None:
    """Remove only validated per-job directories beneath configured roots."""
    with _jobs_lock:
        for job_id in job_ids:
            _jobs.pop(job_id, None)
    with _segment_state_lock:
        for job_id in job_ids:
            _segment_state.pop(job_id, None)
    for job_id in job_ids:
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):
            continue
        for root in (WORK_DIR, OUTPUT_DIR):
            root_path = root.resolve()
            target = (root_path / job_id).resolve()
            if target.parent == root_path and target.is_dir():
                shutil.rmtree(target)


def _authorized_job(job_id: str) -> Tuple[Optional[str], Optional[Tuple[Any, int]]]:
    user_id, error = supabase_store.authenticate(request.headers.get("Authorization", ""))
    if error:
        return None, (jsonify({"error": error}), _auth_error_status(error))
    if not supabase_store.owns_job(user_id, job_id):
        return user_id, (jsonify({"error": "resource not found"}), 404)
    return user_id, None


def _signed_media_url(path: str, ttl_seconds: int = 900) -> str:
    expires = int(time.time()) + max(30, ttl_seconds)
    payload = f"{path}|{expires}".encode("utf-8")
    signature = hmac.new(_media_signing_key, payload, hashlib.sha256).hexdigest()
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}expires={expires}&signature={signature}"


def _valid_media_signature() -> bool:
    try:
        expires = int(request.args.get("expires", "0"))
    except ValueError:
        return False
    if expires < int(time.time()) or expires > int(time.time()) + 3600:
        return False
    payload = f"{request.path}|{expires}".encode("utf-8")
    expected = hmac.new(_media_signing_key, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, request.args.get("signature", ""))


def _authorize_media(job_id: str) -> Optional[Tuple[Any, int]]:
    if _valid_media_signature():
        return None
    _, response = _authorized_job(job_id)
    return response


def _require_admin() -> Tuple[Optional[str], Optional[Tuple[Any, int]]]:
    user_id, error = supabase_store.authenticate(request.headers.get("Authorization", ""))
    if error:
        return None, (jsonify({"error": error}), _auth_error_status(error))
    if not supabase_store.enabled:
        if os.environ.get("ALLOW_LOCAL_DEBUG_ENDPOINTS", "0") != "1":
            return None, (jsonify({"error": "not found"}), 404)
        return user_id, None
    admins = {
        item.strip() for item in os.environ.get("ADMIN_USER_IDS", "").split(",")
        if item.strip()
    }
    if not user_id or user_id not in admins:
        return user_id, (jsonify({"error": "not found"}), 404)
    return user_id, None


def _secure_local_media_urls(job: Dict[str, Any]) -> Dict[str, Any]:
    secured = copy.deepcopy(job)
    for key in ("output_url", "manifest_url", "evaluation_url", "stream_url"):
        value = secured.get(key)
        if isinstance(value, str) and value.startswith("/"):
            secured[key] = _signed_media_url(value)
    for scene in secured.get("scenes", []):
        for key in ("image_url", "imageUrl", "video_url", "videoUrl"):
            value = scene.get(key)
            if isinstance(value, str) and value.startswith("/"):
                scene[key] = _signed_media_url(value)
    return secured


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": f"Upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"}), 413


@app.route("/api/account/password-reset-request", methods=["POST"])
def account_password_reset_request():
    """Audit and rate-limit a recovery request without revealing account existence."""
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    if not email or len(email) > 320 or "@" not in email:
        return jsonify({"error": "Enter a valid email address"}), 400
    subject_hash = _account_subject_hash(email)
    if supabase_store.enabled and (
        JOB_EXECUTION_MODE == "queue"
        or not job
        or job.get("status") in {"completed", "failed", "canceled"}
    ):
        try:
            rate = supabase_store.consume_rate_limit(
                f"password-reset:{subject_hash}",
                PASSWORD_RESET_RATE_LIMIT,
                PASSWORD_RESET_RATE_WINDOW_SECONDS,
            )
            outcome = "requested" if rate.get("allowed") else "blocked"
            _audit_account_event(email, "password_reset", outcome)
        except Exception:
            _audit_account_event(email, "password_reset", "failed")
    return jsonify({"accepted": True}), 202


@app.route("/api/account/password-reset-complete", methods=["POST"])
def account_password_reset_complete():
    user_id, error = supabase_store.authenticate(request.headers.get("Authorization", ""))
    if error:
        return jsonify({"error": error}), _auth_error_status(error)
    if user_id:
        _audit_account_event(user_id, "password_reset", "succeeded")
    return jsonify({"recorded": True})


@app.route("/api/account/password", methods=["POST"])
def account_password_change():
    user, error = supabase_store.authenticated_user(request.headers.get("Authorization", ""))
    if error or not user:
        return jsonify({"error": error or "Authentication required"}), _auth_error_status(error)
    user_id = str(user.get("id") or "")
    email = str(user.get("email") or "")
    data = request.get_json(silent=True) or {}
    current_password = str(data.get("current_password") or "")
    new_password = str(data.get("new_password") or "")
    confirmation = str(data.get("new_password_confirmation") or "")
    if len(current_password) < 1:
        return jsonify({"error": "Enter your current password"}), 400
    if len(new_password) < 8:
        return jsonify({"error": "New password must contain at least 8 characters"}), 400
    if new_password != confirmation:
        return jsonify({"error": "New passwords do not match"}), 400
    if new_password == current_password:
        return jsonify({"error": "Choose a password different from your current password"}), 400
    verified, reauth_error = supabase_store.reauthenticate_password(user_id, email, current_password)
    if not verified:
        _audit_account_event(user_id, "password_change", "rejected", {"reason": "reauthentication"})
        return jsonify({"error": reauth_error or "Current password is incorrect"}), _auth_error_status(reauth_error)
    try:
        supabase_store.update_account_password(user_id, new_password)
    except Exception:
        _audit_account_event(user_id, "password_change", "failed")
        return jsonify({"error": "Password could not be changed right now"}), 503
    _audit_account_event(user_id, "password_change", "succeeded")
    return jsonify({"changed": True, "reauthentication_required": True})


@app.route("/api/account", methods=["DELETE"])
def account_delete():
    user, error = supabase_store.authenticated_user(request.headers.get("Authorization", ""))
    if error or not user:
        return jsonify({"error": error or "Authentication required"}), _auth_error_status(error)
    user_id = str(user.get("id") or "")
    email = str(user.get("email") or "")
    data = request.get_json(silent=True) or {}
    password = str(data.get("current_password") or "")
    if data.get("confirmation") != "DELETE MY ACCOUNT":
        return jsonify({"error": "Type DELETE MY ACCOUNT to confirm"}), 400
    if not password:
        return jsonify({"error": "Enter your current password"}), 400
    _audit_account_event(user_id, "account_deletion", "requested")
    verified, reauth_error = supabase_store.reauthenticate_password(user_id, email, password)
    if not verified:
        _audit_account_event(user_id, "account_deletion", "rejected", {"reason": "reauthentication"})
        return jsonify({"error": reauth_error or "Current password is incorrect"}), _auth_error_status(reauth_error)
    try:
        job_ids = supabase_store.delete_account(user_id)
    except RuntimeError as exc:
        if str(exc) == "active_jobs":
            _audit_account_event(user_id, "account_deletion", "blocked", {"reason": "active_jobs"})
            return jsonify({"error": "Wait for active video generations to finish before deleting your account"}), 409
        _audit_account_event(user_id, "account_deletion", "failed")
        return jsonify({"error": "Account deletion could not be completed safely"}), 503
    except Exception:
        _audit_account_event(user_id, "account_deletion", "failed")
        return jsonify({"error": "Account deletion could not be completed safely"}), 503
    _remove_local_account_jobs(job_ids)
    _audit_account_event(user_id, "account_deletion", "succeeded", {"jobs_removed": len(job_ids)})
    return jsonify({"deleted": True})


@app.route("/healthz")
def health_check():
    """Liveness only: this process can accept and return HTTP."""
    return jsonify({
        "status": "ok",
        "service": "pencilvid-web",
        "version": os.environ.get("APP_VERSION", "development"),
    })


@app.route("/readyz")
def readiness_check():
    """Readiness: required database, storage, and render tools are usable."""
    if MAINTENANCE_MODE:
        return jsonify({
            "status": "maintenance",
            "service": "pencilvid-web",
            "version": os.environ.get("APP_VERSION", "development"),
            "checks": {"maintenance_mode": {"ok": False, "detail": "enabled"}},
        }), 503

    from runtime_health import check_readiness

    result = check_readiness(
        store=supabase_store,
        frontend_dist=FRONTEND_DIST,
        work_dir=WORK_DIR,
        output_dir=OUTPUT_DIR,
        production=IS_PRODUCTION,
    )
    result.update({
        "service": "pencilvid-web",
        "version": os.environ.get("APP_VERSION", "development"),
    })
    return jsonify(result), 200 if result["status"] == "ready" else 503


def _set_job(job_id: str, **changes: Any) -> None:
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(changes)
    supabase_store.update_job(job_id, changes)


def _get_job(job_id: str) -> Dict[str, Any]:
    with _jobs_lock:
        return dict(_jobs.get(job_id, {}))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _job_scene_from_plan(scene: Dict[str, Any]) -> Dict[str, Any]:
    scene_id = max(1, int(scene.get("id", 1)))
    return {
        "index": scene_id - 1,
        "title": str(
            scene.get("teaching_beat")
            or scene.get("title")
            or f"Scene {scene_id}"
        ),
        "narration": str(scene.get("narration", "")),
        "status": "pending",
        "image_url": None,
        "video_url": None,
    }


def _set_job_plan(job_id: str, plan: Dict[str, Any]) -> None:
    scenes = [
        _job_scene_from_plan(scene)
        for scene in plan.get("scenes", [])
        if isinstance(scene, dict)
    ]
    _set_job(
        job_id,
        title=str(plan.get("title") or ""),
        description=str(
            plan.get("scope_summary")
            or plan.get("learning_goal")
            or ""
        ),
        coverage=list(plan.get("must_cover", [])),
        scenes=scenes,
    )
    supabase_store.replace_scenes(_get_job(job_id).get("user_id"), job_id, scenes)


def _update_job_scene(job_id: str, scene_index: int, **changes: Any) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        scenes = [dict(scene) for scene in job.get("scenes", [])]
        for scene in scenes:
            if int(scene.get("index", -1)) == scene_index:
                scene.update(changes)
                break
        job["scenes"] = scenes
    supabase_store.update_scene(job_id, scene_index, changes)


def _scene_image_path(job_id: str, scene_index: int) -> Optional[Path]:
    scene_number = scene_index + 1
    candidates = (
        WORK_DIR / job_id / "segments" / f"scene_{scene_number:03d}" / "scene.png",
        WORK_DIR / job_id / "scenes" / f"scene_{scene_number:03d}.png",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _scene_image_url(job_id: str, scene_index: int) -> Optional[str]:
    if _scene_image_path(job_id, scene_index) is None:
        return None
    return f"/media/jobs/{job_id}/scenes/{scene_index}.png"


def _write_preflight_failure_evaluation(
    job_id: str, output_dir: Path, payload: Dict[str, Any], exc: Exception,
) -> Optional[Path]:
    diagnostics = getattr(exc, "diagnostics", None)
    if not isinstance(diagnostics, dict):
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "evaluation.json"
    path.write_text(json.dumps({
        "report_version": "2.1",
        "job_id": job_id,
        "status": "rejected_before_tts_visuals_and_encoding",
        "request": {
            "prompt": str(payload.get("prompt", "")),
            "target_duration_seconds": float(payload.get("duration_seconds", 0)),
            "audience": str(payload.get("audience", "general learner")),
        },
        "duration_preflight": diagnostics,
        "quality_gate": {
            "pass": False,
            "reason": diagnostics.get("reason"),
            "tts_calls_prevented": True,
            "visual_calls_prevented": True,
            "encoding_prevented": True,
            "silence_padding_prevented": True,
        },
        "error": str(exc),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _parallel_map(items, fn: Callable, workers: int = MAX_WORKERS):
    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        future_to_index = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for future in as_completed(future_to_index):
            i = future_to_index[future]
            results[i] = future.result()
    return results


def _duration_within_tolerance(actual_seconds: float, target_seconds: float) -> bool:
    return abs(actual_seconds - target_seconds) <= DURATION_TOLERANCE_SECONDS


def _generate_tts_pass(
    plan: Dict[str, Any], pass_dir: Path, job_id: Optional[str] = None,
) -> Tuple[List[Path], List[float]]:
    pass_dir.mkdir(parents=True, exist_ok=True)
    scenes = plan["scenes"]

    def worker(scene):
        if job_id:
            _raise_if_job_canceled(job_id)
        scene_id = int(scene["id"])
        wav = pass_dir / f"scene_{scene_id:03d}.wav"
        synthesize(scene["narration"], wav, job_id=job_id)
        if job_id:
            _raise_if_job_canceled(job_id)
        return wav, audio_duration_seconds(wav)

    pairs = _parallel_map(scenes, worker, workers=min(MAX_WORKERS, 4))
    audio_files = [p[0] for p in pairs]
    durations = [float(p[1]) for p in pairs]
    return audio_files, durations


def _fit_narration_duration(
    job_id: str,
    plan: Dict[str, Any],
    target_seconds: float,
    job_dir: Path,
):
    _set_job(job_id, stage="tts_duration_check", progress=18,
             detail="Generating one natural narration pass")
    raw_files, raw_durations = _generate_tts_pass(plan, job_dir / "audio_raw", job_id=job_id)
    from narration_compiler import allocate_final_scene_durations

    planned_targets = [float(scene.get("target_scene_seconds", 0)) for scene in plan["scenes"]]
    allocated_targets = allocate_final_scene_durations(raw_durations, target_seconds, planned_targets)
    final_dir = job_dir / "audio_final"
    final_files, final_durations, pauses = [], [], []
    for scene, raw_file, measured, allocated in zip(plan["scenes"], raw_files, raw_durations, allocated_targets):
        final_file = final_dir / raw_file.name
        padding = pad_wav_to_duration(raw_file, final_file, allocated)
        scene["allocated_scene_seconds"] = round(allocated, 4)
        scene["raw_audio_duration_seconds"] = round(float(measured), 4)
        scene["boundary_pause_seconds"] = padding["boundary_pause_seconds"]
        scene["audio_duration_seconds"] = padding["final_duration_seconds"]
        scene["duration_overshoot_seconds"] = padding["overshoot_seconds"]
        final_files.append(final_file)
        final_durations.append(float(padding["final_duration_seconds"]))
        pauses.append(float(padding["boundary_pause_seconds"]))
    raw_total, final_total = sum(raw_durations), sum(final_durations)
    history = [{
        "attempt": 1, "tts_passes": 1, "semantic_revisions": 0,
        "words": narration_word_count(plan), "raw_speech_seconds": round(raw_total, 4),
        "boundary_pause_seconds": round(sum(pauses), 4), "duration_seconds": round(final_total, 4),
        "target_seconds": round(float(target_seconds), 4),
        "target_error_seconds": round(final_total - target_seconds, 4),
        "audio_source": "raw_tts_plus_boundary_silence", "tempo_adjustment_applied": False,
    }]
    return plan, final_files, final_durations, history

def _generate_visual_assets(
    job_id: str,
    plan: Dict[str, Any],
    job_dir: Path,
    sketch: bool,
    runtime: Optional[JobRuntimeConfig] = None,
):
    scenes_dir = job_dir / "scenes"
    scenes_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    scenes = plan["scenes"]

    completed = 0
    completed_lock = threading.Lock()
    continuity = ContinuityCoordinator()

    def worker(scene):
        nonlocal completed

        if runtime is not None:
            with activate_runtime(runtime):
                return build_scene(scene)
        return build_scene(scene)

    def build_scene(scene):
        nonlocal completed

        _raise_if_job_canceled(job_id)
        scene_id = int(scene["id"])
        _update_job_scene(job_id, scene_id - 1, status="active")

        visual_prompt = str(
            scene.get(
                "visual_prompt",
                scene.get(
                    "narration",
                    "Main idea",
                ),
            )
        ).strip()

        print(
            f"\n========== GENERATING SCENE "
            f"{scene_id} =========="
        )

        try:
            # create_visual_plan itself now has retries
            # and a deterministic fallback.
            continuity_instruction = continuity_planner_instruction(scene)
            beat_instruction = beat_visual_timeline_instruction(scene)
            visual_plan = create_visual_plan(
                visual_prompt,
                anim_prompt="\n\n".join(
                    part for part in (continuity_instruction, beat_instruction) if part
                ) or None,
                job_id=job_id,
                semantic_constraints=scene.get("semantic_contract"),
                scene_id=scene_id,
            )
            _raise_if_job_canceled(job_id)

        except Exception as error:
            # This theoretically should almost never be
            # reached now, but keep an app-level safety net.
            print(
                f"Scene {scene_id} visual generation "
                f"unexpectedly failed:",
                error,
            )

            visual_plan = semantic_fallback_plan(
                visual_prompt, scene.get("semantic_contract")
            )
            log_local_transform(
                job_id, "visual_generation_exception", scene_id=scene_id,
                candidate_id="semantic_fallback", after=visual_plan,
                details={"error": str(error)[:2000]}, status="repaired",
            )

        # LLM planning above still runs in parallel. Only this cheap local
        # reconciliation commits plans in scene order so recurring objects keep
        # their previous on-screen geometry.
        pre_continuity_plan = visual_plan
        visual_plan = continuity.submit(scene, visual_plan)
        log_local_transform(
            job_id, "continuity_reconciliation", scene_id=scene_id,
            candidate_id="post_continuity", before=pre_continuity_plan,
            after=visual_plan,
            details={"decision": copy.deepcopy(visual_plan.get("_continuity", {}))},
            status="ok",
        )
        pre_static_state = visual_plan
        visual_plan = materialize_static_visual_state(visual_plan)
        log_local_transform(
            job_id, "static_visual_state", scene_id=scene_id,
            candidate_id="declared_final_state", before=pre_static_state,
            after=visual_plan,
            details=copy.deepcopy(visual_plan.get("_static_state", {})),
            status="ok",
        )
        visual_plan = repair_post_continuity_plan(
            visual_plan, spoken_context=str(scene.get("narration", ""))
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
            quality_shadow_mode = os.environ.get(
                "VISUAL_QUALITY_SHADOW_MODE", "false"
            ).strip().lower() in {"1", "true", "yes", "on"}
            active_gate_enabled = os.environ.get(
                "VISUAL_ACTIVE_QUALITY_GATE", "true"
            ).strip().lower() in {"1", "true", "yes", "on"}
            original = visual_plan
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
            fallback = repair_post_continuity_plan(
                fallback, spoken_context=str(scene.get("narration", ""))
            )
            fallback = normalize_visual_plan(fallback)
            fallback = apply_graphic_style(fallback)
            fallback["_explanation"] = assess_visual_anchor_coverage(fallback, scene)
            fallback["_quality_gate"] = evaluate_pre_render_quality_gate(fallback, scene)
            registry = CandidateRegistry(
                scene.get("semantic_contract")
                if isinstance(scene.get("semantic_contract"), dict) else {}
            )
            registry.add("post_continuity", "post_continuity", original)
            registry.add(
                "rich_without_continuity", "continuity_rollback", independent
            )
            registry.add("semantic_fallback", "active_gate_repair", fallback)
            selected = registry.best()
            if selected and active_gate_enabled and not quality_shadow_mode:
                visual_plan = selected["plan"]
                visual_plan["_candidate_selection"] = {
                    "selected": selected["candidate_id"],
                    "quality": selected["quality"], "candidate_count": 3,
                    "reason": "active_post_continuity_gate",
                }
            log_local_transform(
                job_id, "active_post_continuity_gate", scene_id=scene_id,
                candidate_id=str(
                    visual_plan.get("_candidate_selection", {}).get(
                        "selected", "post_continuity"
                    )
                ), before=original, after=visual_plan,
                details={
                    "issues_before": original.get("_quality_gate", {}).get("issues", []),
                    "issues_after": visual_plan.get("_quality_gate", {}).get("issues", []),
                    "scores": [item["quality"] for item in registry.candidates],
                    "shadow_mode": quality_shadow_mode,
                    "active_gate_enabled": active_gate_enabled,
                    "would_select": selected["candidate_id"] if selected else None,
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

        try:
            svg, icons = render_visual_plan(
                visual_plan,
                sketch=sketch,
            )

        except Exception as render_error:
            print(
                f"Scene {scene_id} SVG rendering failed:",
                render_error,
            )

            failed_plan = visual_plan
            salvaged = _salvage_single_render_element(
                failed_plan, sketch=sketch,
            )
            if salvaged is not None:
                visual_plan, svg, icons, offending = salvaged
                log_local_transform(
                    job_id, "svg_render_element_isolation", scene_id=scene_id,
                    candidate_id="single_element_salvage", before=failed_plan,
                    after=visual_plan,
                    details={
                        "error": str(render_error)[:2000],
                        "offending_element": offending,
                    }, status="repaired",
                )
            else:
                # No single element explained the failure. Compile a safe
                # semantic composition as the final renderable candidate.
                visual_plan = semantic_fallback_plan(
                    visual_prompt, scene.get("semantic_contract")
                )
                visual_plan = repair_post_continuity_plan(
                    visual_plan, spoken_context=str(scene.get("narration", ""))
                )
                visual_plan = normalize_visual_plan(visual_plan)
                visual_plan = apply_graphic_style(visual_plan)
                visual_plan["_explanation"] = assess_visual_anchor_coverage(
                    visual_plan, scene
                )
                visual_plan["_quality_gate"] = evaluate_pre_render_quality_gate(
                    visual_plan, scene
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

                svg, icons = render_visual_plan(
                    visual_plan,
                    sketch=False,
                )
                log_local_transform(
                    job_id, "svg_render_exception", scene_id=scene_id,
                    candidate_id="semantic_fallback", before=failed_plan,
                    after=visual_plan,
                    details={"error": str(render_error)[:2000]}, status="repaired",
                )

        icon_provenance = copy.deepcopy(
            visual_plan.get("_graphic_style", {}).get("icon_resolutions", [])
            if isinstance(visual_plan.get("_graphic_style"), dict) else []
        )
        if icon_provenance or icons:
            log_local_transform(
                job_id, "icon_resolution", scene_id=scene_id,
                candidate_id=str(
                    visual_plan.get("_candidate_selection", {}).get("selected", "final")
                ), after={"resolved_icons": icons, "provenance": icon_provenance},
                details={"resolved_count": len(icons)}, status="ok",
            )

        svg_path = (
            scenes_dir
            / f"scene_{scene_id:03d}.svg"
        )

        visual_plan_path = (
            scenes_dir
            / f"scene_{scene_id:03d}.visual_plan.json"
        )

        png_path = (
            scenes_dir
            / f"scene_{scene_id:03d}.png"
        )

        svg_path.write_text(
            svg,
            encoding="utf-8",
        )

        visual_plan_path.write_text(
            json.dumps(visual_plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        svg_to_png(
            svg,
            png_path,
        )

        _update_job_scene(
            job_id,
            scene_id - 1,
            status="done",
            image_url=f"/media/jobs/{job_id}/scenes/{scene_id - 1}.png",
        )

        with completed_lock:
            completed += 1

            _set_job(
                job_id,
                progress=55
                + round(
                    23
                    * completed
                    / len(scenes)
                ),
                detail=(
                    f"Rendered visual "
                    f"{completed}/{len(scenes)}"
                ),
            )

        return {
            "scene_id": scene_id,
            "visual_plan": visual_plan,
            "icons": icons,
            "visual_plan_path": visual_plan_path,
            "svg_path": svg_path,
            "png_path": png_path,
            "beat_source_plan": pre_static_state,
        }

    return _parallel_map(
        scenes,
        worker,
        workers=MAX_VISUAL_WORKERS,
    )


def _render_scene_segments(job_id: str, visual_assets, audio_files, durations, job_dir: Path, plan: Dict[str, Any], captions: Optional[bool] = None, runtime: Optional[JobRuntimeConfig] = None):
    video_dir = job_dir / "video_scenes"
    video_dir.mkdir(parents=True, exist_ok=True)
    scene_by_id = {int(s.get("id")): s for s in plan.get("scenes", [])}
    items = list(zip(visual_assets, audio_files, durations))
    completed = 0
    completed_lock = threading.Lock()
    def worker(item):
        nonlocal completed
        if runtime is not None:
            with activate_runtime(runtime):
                return render_item(item)
        return render_item(item)

    def render_item(item):
        nonlocal completed
        _raise_if_job_canceled(job_id)
        visual, audio_path, duration = item
        scene_id = int(visual["scene_id"])
        output = video_dir / f"scene_{scene_id:03d}.mp4"
        scene = scene_by_id.get(scene_id, {})
        narration = str(scene.get("narration", "")).strip()
        visual_plan = visual.get("visual_plan", {})
        beats = [
            beat for beat in scene.get("narration_beats", [])
            if isinstance(beat, dict) and str(beat.get("narration", "")).strip()
        ]
        animations = []
        if ANIM_ENABLED:
            try:
                animations = parse_animations_from_plan(visual_plan, float(duration))
            except Exception:  # noqa: BLE001 - deterministic path below
                animations = []
        if ANIM_ENABLED and len(animations) < max(1, len(beats)):
            animations = []
            candidates = [
                (index, element)
                for index, element in enumerate(visual_plan.get("elements", []))
                if isinstance(element, dict) and not bool(element.get("persistent"))
            ][:min(MAX_ANIMATIONS_PER_SCENE, max(1, len(beats)) * 2)]
            beat_count = max(1, len(beats))
            for order, (element_index, element) in enumerate(candidates):
                phase = min(beat_count - 1, int(order / max(1, len(candidates)) * beat_count))
                phase_start = phase / beat_count * float(duration)
                kind = str(element.get("type", "")).lower()
                animation_type = "draw" if kind in {"line", "arrow", "path", "polyline"} else "fade"
                animations.append({
                    "t": round(min(max(0.1, phase_start + order * 0.04), max(0.1, float(duration) - 0.8)), 3),
                    "e": element_index,
                    "a": animation_type,
                    "d": FALLBACK_DURATIONS.get(animation_type, 0.5),
                    "_beat_id": str(beats[phase].get("beat_id", f"beat_{phase + 1}"))
                    if beats else f"beat_{phase + 1}",
                })
        for order, animation in enumerate(animations):
            if "_beat_id" not in animation:
                phase = min(max(0, len(beats) - 1), int(order / max(1, len(animations)) * max(1, len(beats))))
                animation["_beat_id"] = str(beats[phase].get("beat_id", f"beat_{phase + 1}")) if beats else f"beat_{phase + 1}"
        covered = sorted({str(animation.get("_beat_id", "")) for animation in animations if animation.get("_beat_id")})
        visual_plan["_beat_sync"] = {
            "version": 1,
            "narration_beat_count": len(beats),
            "animation_count": len(animations),
            "covered_beat_ids": covered,
            "coverage_pass": not beats or len(covered) >= len(beats),
            "strategy": "batch_deterministic_beat_windows",
        }
        animation_renderer_ready = bool(
            ANIM_ENABLED and animations and fast_svg_animation_available()
        )
        visual_plan["_beat_sync"]["rendered"] = animation_renderer_ready
        if ANIM_ENABLED and animations and not animation_renderer_ready:
            visual_plan["_beat_sync"]["fallback_reason"] = (
                "fast_svg_rasterizer_unavailable_static_render_used"
            )
        visual["visual_plan_path"].write_text(
            json.dumps(visual_plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        static_state_paths = []
        if not animation_renderer_ready and len(beats) > 1:
            try:
                source_plan = visual.get("beat_source_plan")
                states = compile_static_beat_states(
                    source_plan if isinstance(source_plan, dict) else visual_plan,
                    beats,
                )
                beat_dir = video_dir / f"scene_{scene_id:03d}_beats"
                if states:
                    beat_dir.mkdir(parents=True, exist_ok=True)
                for index, state in enumerate(states, start=1):
                    state = repair_post_continuity_plan(
                        state,
                        spoken_context=" ".join(filter(None, (
                            narration, str(scene.get("visual_prompt", "")),
                        ))),
                    )
                    state = normalize_visual_plan(state)
                    state = apply_graphic_style(state)
                    state_svg, _ = render_visual_plan(state, sketch=False)
                    state_svg_path = beat_dir / f"beat_{index:02d}.svg"
                    state_png_path = beat_dir / f"beat_{index:02d}.png"
                    state_svg_path.write_text(state_svg, encoding="utf-8")
                    svg_to_png(state_svg, state_png_path)
                    static_state_paths.append(state_png_path)
                if len(static_state_paths) > 1:
                    weights = [
                        max(1, len(str(beat.get("narration", "")).split()))
                        for beat in beats
                    ]
                    weight_total = sum(weights)
                    state_durations = [float(duration) * weight / weight_total for weight in weights]
                    render_scene_video_sequence(
                        static_state_paths, state_durations, audio_path, output,
                        caption_text=narration, captions=captions,
                    )
                    visual_plan["_beat_sync"].update({
                        "rendered": True,
                        "strategy": "cumulative_static_beat_states",
                        "rendered_static_state_count": len(static_state_paths),
                        "fallback_reason": None,
                    })
            except Exception as static_state_error:  # noqa: BLE001
                print(
                    f"Scene {scene_id} static beat-state render failed "
                    f"({static_state_error}); using final static render."
                )
                static_state_paths = []
                visual_plan["_beat_sync"]["static_state_error"] = str(static_state_error)[:1000]
        if animation_renderer_ready:
            try:
                render_animated_segment(
                    visual["svg_path"].read_text(encoding="utf-8"),
                    visual_plan.get("elements", []),
                    animations,
                    audio_path,
                    output,
                    narration=narration,
                    caption_text=narration,
                    camera_spec=visual_plan.get("camera"),
                    job_seed=scene_id,
                )
            except Exception as animation_error:  # noqa: BLE001
                print(f"Scene {scene_id} batch animation failed ({animation_error}); using static render.")
                render_scene_video(visual["png_path"], audio_path, output, caption_text=narration, captions=captions)
        elif len(static_state_paths) <= 1:
            render_scene_video(visual["png_path"], audio_path, output, caption_text=narration, captions=captions)
        visual["visual_plan_path"].write_text(
            json.dumps(visual_plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with completed_lock:
            completed += 1
            _set_job(
                job_id,
                progress=80 + round(13 * completed / len(items)),
                detail=f"Encoded scene {completed}/{len(items)}",
            )
        return output

    return _parallel_map(items, worker, workers=min(MAX_VISUAL_WORKERS, 3))


def _run_video_job(job_id: str, payload: Dict[str, Any]) -> None:
    job_dir = WORK_DIR / job_id
    output_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        _raise_if_job_canceled(job_id)
        from run_log import get_summary, log_stage_ts
        log_stage_ts(job_id, "started", "batch video generation")
        prompt = str(payload["prompt"]).strip()
        target_seconds = float(payload["duration_seconds"])
        audience = str(payload.get("audience", "general learner")).strip() or "general learner"
        sketch = bool(payload.get("sketch", False))
        raw_captions = payload.get("captions")
        if raw_captions is None:
            captions = None
        elif isinstance(raw_captions, bool):
            captions = raw_captions
        else:
            captions = str(raw_captions).strip().lower() in {"1", "true", "yes", "on"} \
                if str(raw_captions).strip().lower() in {"1", "true", "yes", "on", "0", "false", "no", "off"} \
                else None
        runtime = JobRuntimeConfig.from_payload(
            job_id,
            payload,
            style=str(payload.get("style", "")).strip().lower() or None,
            default_planner_model=PLANNER_MODEL,
        )
        (job_dir / "runtime_diagnostics.json").write_text(
            json.dumps(runtime.diagnostics(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _set_job(job_id, status="running", stage="planning", progress=5, detail="Locking topic scope and writing the complete explanation")
        plan = create_video_plan(prompt, target_seconds, audience, job_id=job_id)
        _raise_if_job_canceled(job_id)
        _set_job_plan(job_id, plan)

        _set_job(job_id, stage="tts", progress=16, detail="Generating local narration and measuring real speech duration")
        plan, audio_files, durations, duration_history = _fit_narration_duration(
            job_id, plan, target_seconds, job_dir
        )
        _raise_if_job_canceled(job_id)
        for scene in plan.get("scenes", []):
            record_text_sample(
                str(scene.get("narration", "")),
                float(scene.get("raw_audio_duration_seconds", 0)),
                raw_tts=True,
                tempo_factor=1.0,
            )

        _set_job(job_id, stage="visuals", progress=52, detail="Generating SVG scenes in parallel")
        visual_assets = _generate_visual_assets(job_id, plan, job_dir, sketch, runtime=runtime)
        _raise_if_job_canceled(job_id)

        _set_job(job_id, stage="encoding", progress=79, detail="Combining each scene with its narration")
        scene_videos = _render_scene_segments(
            job_id, visual_assets, audio_files, durations, job_dir,
            plan=plan, captions=captions, runtime=runtime,
        )
        _raise_if_job_canceled(job_id)

        _set_job(job_id, stage="finalizing", progress=95, detail="Concatenating scene videos")
        final_mp4 = output_dir / "final.mp4"
        concat_scene_videos(scene_videos, final_mp4)
        _raise_if_job_canceled(job_id)
        final_duration = audio_duration_seconds(final_mp4)
        log_stage_ts(job_id, "completed", "batch video generation completed")
        llm_usage = get_summary(job_id)

        manifest = {
            "job_id": job_id,
            "request": {
                "prompt": prompt,
                "target_duration_seconds": target_seconds,
                "audience": audience,
                "sketch": sketch,
                "style": str(payload.get("style", "")).strip().lower(),
            },
            "tts_backend": resolve_backend(),
            "duration_history": duration_history,
            "final_duration_seconds": final_duration,
            "llm_usage": llm_usage,
            "runtime_config": runtime.diagnostics(),
            "duration_integrity": {
                "audio_source": "raw_tts_plus_boundary_silence",
                "tempo_adjustment_applied": False,
                "calibration_samples_raw_only": True,
                "tts_passes": 1,
                "semantic_revisions": 0,
                "boundary_pause_seconds": round(sum(float(s.get("boundary_pause_seconds", 0)) for s in plan["scenes"]), 3),
                "target_error_seconds": round(final_duration - target_seconds, 3),
                "target_error_ratio": round(abs(final_duration - target_seconds) / max(0.1, target_seconds), 6),
            },
            "plan": plan,
            "scenes": [
                {
                    "id": scene["id"],
                    "teaching_beat": scene["teaching_beat"],
                    "narration": scene["narration"],
                    "visual_prompt": scene["visual_prompt"],
                    "audio_duration_seconds": durations[i],
                    "raw_audio_duration_seconds": scene.get("raw_audio_duration_seconds"),
                    "boundary_pause_seconds": scene.get("boundary_pause_seconds", 0),
                    "selected_beat_ids": scene.get("selected_beat_ids", []),
                    "icons": visual_assets[i]["icons"],
                }
                for i, scene in enumerate(plan["scenes"])
            ],
        }
        (job_dir / "duration_diagnostics.json").write_text(
            json.dumps({
                **duration_history[-1],
                "duration_compiler": plan.get("duration_compiler", {}),
                "scene_audio": [{
                    "scene_id": scene.get("id"),
                    "selected_beat_ids": scene.get("selected_beat_ids", []),
                    "raw_speech_seconds": scene.get("raw_audio_duration_seconds"),
                    "boundary_pause_seconds": scene.get("boundary_pause_seconds", 0),
                    "final_audio_seconds": scene.get("audio_duration_seconds"),
                } for scene in plan.get("scenes", [])],
            }, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        evaluation_path = write_canvas_evaluation(
            job_id=job_id,
            mode="batch",
            job_dir=job_dir,
            output_dir=output_dir,
            final_video=final_mp4,
            request_data=manifest["request"],
            plan_data=plan,
        )
        manifest["evaluation_url"] = f"/outputs/{job_id}/{evaluation_path.name}"
        manifest_path = output_dir / "manifest.json"
        write_manifest(manifest_path, manifest)
        _raise_if_job_canceled(job_id)
        supabase_store.complete_video(payload.get("user_id"), job_id, manifest)
        _raise_if_job_canceled(job_id)

        _set_job(
            job_id,
            status="completed",
            stage="completed",
            progress=100,
            detail="Video ready",
            final_duration_seconds=final_duration,
            output_url=f"/outputs/{job_id}/final.mp4",
            manifest_url=f"/outputs/{job_id}/manifest.json",
            evaluation_url=f"/outputs/{job_id}/evaluation.json",
            llm_usage=llm_usage,
            llm_cost_usd=llm_usage.get("estimated_usd_cost"),
            llm_cost_inr=llm_usage.get("estimated_inr_cost"),
            plan_summary={
                "title": plan.get("title"),
                "scope_summary": plan.get("scope_summary"),
                "must_cover": plan.get("must_cover", []),
                "out_of_scope": plan.get("out_of_scope", []),
                "scene_count": len(plan.get("scenes", [])),
                "narration_words": narration_word_count(plan),
            },
        )

    except JobCanceled:
        _set_local_job_canceled(job_id)
    except Exception as exc:
        evaluation = _write_preflight_failure_evaluation(job_id, output_dir, payload, exc)
        try:
            from run_log import log_stage_ts
            log_stage_ts(job_id, "failed", str(exc)[:300])
        except Exception:
            pass
        traceback.print_exc()
        _set_job(
            job_id,
            status="failed",
            stage="failed",
            detail=str(exc),
            error=str(exc),
            evaluation_url=f"/outputs/{job_id}/evaluation.json" if evaluation else None,
        )


def _pick_streaming_mode(data: Dict[str, Any]) -> bool:
    """Choose streaming mode: explicit field, or default when enabled."""
    mode = str(data.get("mode", "")).strip().lower()
    if mode == "streaming" or mode == "stream":
        return True
    if mode == "batch":
        return False
    return bool(ENABLE_STREAMING)


DOC_UPLOAD_DIR = Path(os.environ.get("DOC_UPLOAD_DIR", str(WORK_DIR / "uploads")))
DOC_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
# Full extracted text is stored for excerpt selection, but only a small
# TPM-safe anchor is sent to the director (developer plan TPM limit ~8k).
DOC_MAX_CHARS = 200_000
DOC_DIRECTOR_ANCHOR_CHARS = 4_000


def _extract_document_text(path: Path) -> str:
    """Extract plain text from an uploaded document.

    Supports .txt/.md directly, .pdf via pdftotext, .docx via python-docx
    (best effort), .pptx via python-pptx (best effort). Returns at most
    DOC_MAX_CHARS. Constant token cost: the director receives only a
    truncated excerpt anchor, not the whole document.
    """
    name = str(path.name).lower()
    try:
        if name.endswith((".txt", ".md", ".markdown")):
            return path.read_text(encoding="utf-8", errors="replace")
        if name.endswith(".pdf"):
            try:
                res = subprocess.run(
                    ["pdftotext", "-layout", str(path), "-"],
                    capture_output=True, timeout=180,
                )
            except FileNotFoundError:
                # pdftotext not installed at all — try a pure-Python fallback
                import pypdf as _pypdf
                text = ""
                try:
                    reader = _pypdf.PdfReader(str(path))
                    text = "\n\n".join(
                        page.extract_text() or "" for page in reader.pages
                    )
                except Exception:
                    pass
                if text.strip():
                    return text
                raise
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.decode("utf-8", errors="replace")
            if res.returncode != 0:
                # pdftotext failed (e.g. encrypted/scan-like PDF): fall back
                # to pure-Python extraction instead of giving up entirely.
                import pypdf as _pypdf
                reader = _pypdf.PdfReader(str(path))
                text = "\n\n".join(
                    page.extract_text() or "" for page in reader.pages
                )
                if text.strip():
                    return text
        if name.endswith((".docx",)):
            import docx

            doc = docx.Document(str(path))
            return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if name.endswith((".pptx",)):
            from pptx import Presentation

            prs = Presentation(str(path))
            parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        t = "\n".join(
                            r.text for r in shape.text_frame.paragraphs
                            if r.text.strip()
                        )
                        if t.strip():
                            parts.append(t)
            return "\n\n".join(parts)
    except Exception as exc:  # pylint: disable=broad-except
        # Log at ERROR level with the filename so the failure is visible in
        # the server log and per-run logs instead of a silent 0-char result.
        import logging
        logging.getLogger(__name__).error(
            "Document extraction failed for %s: %s; continuing without source "
            "text.", path.name, exc,
        )
    return ""


def _load_video_manifest(job_id: str) -> Optional[Dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return None
    manifest_path = OUTPUT_DIR / job_id / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _manifest_created_at(job_id: str) -> str:
    manifest_path = OUTPUT_DIR / job_id / "manifest.json"
    timestamp = manifest_path.stat().st_mtime if manifest_path.is_file() else 0
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _manifest_scene_payloads(
    job_id: str, manifest: Dict[str, Any], *, status: str = "done",
) -> List[Dict[str, Any]]:
    plan = manifest.get("plan") if isinstance(manifest.get("plan"), dict) else {}
    plan_scenes = plan.get("scenes") if isinstance(plan, dict) else []
    manifest_scenes = manifest.get("scenes")
    segments = manifest.get("segments")
    source_scenes = (
        plan_scenes if isinstance(plan_scenes, list) and plan_scenes
        else manifest_scenes if isinstance(manifest_scenes, list) and manifest_scenes
        else segments if isinstance(segments, list) else []
    )
    scenes = []
    for fallback_index, scene in enumerate(source_scenes):
        if not isinstance(scene, dict):
            continue
        raw_id = scene.get("id", scene.get("scene_id", fallback_index + 1))
        try:
            index = max(0, int(raw_id) - 1)
        except (TypeError, ValueError):
            index = fallback_index
        title = str(
            scene.get("teaching_beat")
            or scene.get("title")
            or scene.get("visual_prompt")
            or f"Scene {index + 1}"
        )
        scenes.append({
            "index": index,
            "title": title,
            "narration": str(scene.get("narration", "")),
            "status": status,
            "imageUrl": _scene_image_url(job_id, index),
        })
    return sorted(scenes, key=lambda scene: scene["index"])


def _manifest_video_detail(job_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    request_data = manifest.get("request") if isinstance(manifest.get("request"), dict) else {}
    plan = manifest.get("plan") if isinstance(manifest.get("plan"), dict) else {}
    scenes = _manifest_scene_payloads(job_id, manifest)
    coverage = plan.get("must_cover") if isinstance(plan, dict) else []
    coverage = [str(item) for item in coverage] if isinstance(coverage, list) else []
    scene_count = len(scenes)
    coverage_count = len(coverage)
    coverage_map = [
        min(
            max(0, scene_count - 1),
            round(index * max(0, scene_count - 1) / max(1, coverage_count - 1)),
        )
        for index in range(coverage_count)
    ]
    prompt = str(request_data.get("prompt", ""))
    title = str(plan.get("title") or prompt or "Untitled explanation")
    duration = manifest.get(
        "final_duration_seconds",
        request_data.get("target_duration_seconds", 0),
    )
    try:
        duration_seconds = max(0, round(float(duration)))
    except (TypeError, ValueError):
        duration_seconds = 0
    return {
        "id": job_id,
        "title": title,
        "description": str(
            plan.get("scope_summary")
            or plan.get("learning_goal")
            or prompt
        ),
        "audience": str(request_data.get("audience", "beginner")),
        "durationSec": duration_seconds,
        "style": str(request_data.get("style") or "clean-diagrams"),
        "scenes": scenes,
        "coverage": coverage,
        "coverageSceneMap": coverage_map,
        "videoUrl": f"/outputs/{job_id}/final.mp4",
        "posterUrl": scenes[0].get("imageUrl") if scenes else None,
    }


def _database_video_detail(row: Dict[str, Any], scenes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    job_id = str(row["id"])
    video_url = supabase_store.signed_storage_url("video-assets", row.get("video_storage_path"))
    if not video_url:
        return None
    manifest = row.get("manifest") if isinstance(row.get("manifest"), dict) else {}
    coverage = row.get("coverage") if isinstance(row.get("coverage"), list) else []
    scene_payloads = []
    for scene in scenes:
        image_url = supabase_store.signed_storage_url("video-assets", scene.get("image_storage_path"))
        scene_payloads.append({
            "index": int(scene.get("scene_index", len(scene_payloads))),
            "title": str(scene.get("title") or f"Scene {len(scene_payloads) + 1}"),
            "narration": str(scene.get("narration") or ""),
            "status": str(scene.get("status") or "done"),
            "imageUrl": image_url,
        })
    scene_count = len(scene_payloads)
    coverage_map = [
        min(max(0, scene_count - 1), round(index * max(0, scene_count - 1) / max(1, len(coverage) - 1)))
        for index in range(len(coverage))
    ]
    return {
        "id": job_id,
        "title": str(row.get("title") or "Untitled explanation"),
        "description": str(row.get("description") or ""),
        "audience": str(row.get("audience") or "beginner"),
        "durationSec": max(0, round(float(row.get("duration_seconds") or 0))),
        "style": str(row.get("style") or "default"),
        "scenes": scene_payloads,
        "coverage": [str(item) for item in coverage],
        "coverageSceneMap": coverage_map,
        "videoUrl": video_url,
        "posterUrl": supabase_store.signed_storage_url("video-assets", row.get("poster_storage_path")),
        "manifest": manifest,
    }


@app.route("/media/jobs/<job_id>/scenes/<int:scene_index>.png")
def generated_scene_image(job_id: str, scene_index: int):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id) or scene_index < 0:
        return jsonify({"error": "invalid scene path"}), 404
    denied = _authorize_media(job_id)
    if denied:
        return denied
    image_path = _scene_image_path(job_id, scene_index)
    if image_path is None:
        return jsonify({"error": "scene image not found"}), 404
    return send_from_directory(image_path.parent, image_path.name)


@app.route("/api/videos", methods=["GET"])
def list_videos():
    user_id, auth_error = supabase_store.authenticate(request.headers.get("Authorization", ""))
    if auth_error:
        return jsonify({"error": auth_error}), _auth_error_status(auth_error)

    owned_job_ids = supabase_store.owned_job_ids(user_id)

    def is_owned(job_id: str) -> bool:
        return owned_job_ids is None or job_id in owned_job_ids

    summaries: List[Dict[str, Any]] = []
    seen = set()
    if supabase_store.enabled:
        try:
            for row in supabase_store.list_video_records(user_id) or []:
                summaries.append({
                    "id": str(row["id"]),
                    "title": str(row.get("title") or "Untitled explanation"),
                    "durationSec": max(0, round(float(row.get("duration_seconds") or 0))),
                    "audience": str(row.get("audience") or "beginner"),
                    "createdAt": str(row.get("created_at") or _utc_now_iso()),
                    "posterUrl": supabase_store.signed_storage_url("video-assets", row.get("poster_storage_path")),
                    "status": "ready",
                })
                seen.add(str(row["id"]))
            for job in supabase_store.active_job_records(user_id):
                if str(job["id"]) in seen:
                    continue
                summaries.insert(0, {
                    "id": str(job["id"]),
                    "title": str(job.get("prompt") or "Creating explanation"),
                    "durationSec": max(0, round(float(job.get("requested_duration_seconds") or 0))),
                    "audience": str(job.get("audience") or "beginner"),
                    "createdAt": str(job.get("created_at") or _utc_now_iso()),
                    "posterUrl": None,
                    "status": "generating",
                    "progressPercent": int(job.get("progress") or 0),
                })
            return jsonify(summaries)
        except Exception as exc:
            app.logger.error("Unable to list durable videos: %s", type(exc).__name__)
            return jsonify({"error": "Video library is temporarily unavailable"}), 503

    if OUTPUT_DIR.is_dir():
        manifests = sorted(
            OUTPUT_DIR.glob("*/manifest.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for manifest_path in manifests:
            job_id = manifest_path.parent.name
            if not is_owned(job_id):
                continue
            manifest = _load_video_manifest(job_id)
            if manifest is None or not (manifest_path.parent / "final.mp4").is_file():
                continue
            detail = _manifest_video_detail(job_id, manifest)
            summaries.append({
                "id": job_id,
                "title": detail["title"],
                "durationSec": detail["durationSec"],
                "audience": detail["audience"],
                "createdAt": _manifest_created_at(job_id),
                "posterUrl": detail["posterUrl"],
                "status": "ready",
            })
            seen.add(job_id)

    with _jobs_lock:
        current_jobs = [(job_id, dict(job)) for job_id, job in _jobs.items()]
    for job_id, job in current_jobs:
        if job_id in seen or job.get("status") not in {"queued", "running"}:
            continue
        if not is_owned(job_id):
            continue
        summaries.insert(0, {
            "id": job_id,
            "title": str(job.get("title") or job.get("prompt") or "Creating explanation"),
            "durationSec": round(float(job.get("target_duration_seconds", 0))),
            "audience": str(job.get("audience", "beginner")),
            "createdAt": str(job.get("created_at") or _utc_now_iso()),
            "posterUrl": next(
                (
                    scene.get("image_url")
                    for scene in job.get("scenes", [])
                    if scene.get("image_url")
                ),
                None,
            ),
            "status": "generating",
            "progressPercent": int(job.get("progress", 0)),
        })
    return jsonify(summaries)


@app.route("/api/videos/<job_id>", methods=["GET"])
def video_detail(job_id: str):
    user_id, auth_error = supabase_store.authenticate(request.headers.get("Authorization", ""))
    if auth_error:
        return jsonify({"error": auth_error}), _auth_error_status(auth_error)
    if not supabase_store.owns_job(user_id, job_id):
        return jsonify({"error": "video not found"}), 404
    if supabase_store.enabled:
        try:
            row = supabase_store.video_record(user_id, job_id)
            if not row:
                return jsonify({"error": "video not found"}), 404
            detail = _database_video_detail(row, supabase_store.scene_records(user_id, job_id))
            if not detail:
                return jsonify({"error": "Video storage is temporarily unavailable"}), 503
            return jsonify(detail)
        except Exception as exc:
            app.logger.error("Unable to load durable video %s: %s", job_id, type(exc).__name__)
            return jsonify({"error": "Video is temporarily unavailable"}), 503
    manifest = _load_video_manifest(job_id)
    if manifest is None:
        return jsonify({"error": "video not found"}), 404
    return jsonify(_manifest_video_detail(job_id, manifest))


@app.route("/api/billing/checkout", methods=["POST"])
def create_billing_checkout():
    """Server boundary for a future verified payment-provider checkout.

    Paid plans are deliberately unavailable until a provider adapter and
    signed webhook are configured. The browser can no longer mutate plans.
    """
    _user_id, auth_error = supabase_store.authenticate(request.headers.get("Authorization", ""))
    if auth_error:
        return jsonify({"error": auth_error}), _auth_error_status(auth_error)
    data = request.get_json(silent=True) or {}
    if data.get("plan") not in {"pro", "pro_ultra"}:
        return jsonify({"error": "Unknown paid plan"}), 400
    return jsonify({
        "error": "Secure billing is not configured yet. No plan change was made.",
        "code": "billing_not_configured",
    }), 503


@app.route("/api/videos", methods=["POST"])
def create_video():
    user_id, auth_error = supabase_store.authenticate(request.headers.get("Authorization", ""))
    if auth_error:
        return jsonify({"error": auth_error}), _auth_error_status(auth_error)
    from llm_provider import llm_configuration_readiness

    llm_readiness = llm_configuration_readiness()
    if not llm_readiness["ok"]:
        return jsonify({
            "error": "Video generation is temporarily unavailable because its AI provider is not configured",
            "code": "llm_provider_not_configured",
        }), 503
    try:
        rate = supabase_store.consume_rate_limit(
            f"generation:{user_id or request.remote_addr or 'local'}",
            GENERATION_RATE_LIMIT,
            GENERATION_RATE_WINDOW_SECONDS,
        )
    except Exception as exc:
        app.logger.error("Generation rate limit failed: %s", type(exc).__name__)
        return jsonify({"error": "Request protection is temporarily unavailable"}), 503
    if not rate.get("allowed", False):
        retry_after = int(rate.get("retry_after_seconds") or GENERATION_RATE_WINDOW_SECONDS)
        response = jsonify({"error": "Too many generation requests", "retry_after_seconds": retry_after})
        response.headers["Retry-After"] = str(retry_after)
        return response, 429
    quota = supabase_store.quota_status(user_id)
    if quota and not quota.get("allowed"):
        status = 503 if quota.get("error") else 429
        return jsonify({"error": quota.get("error") or "Video limit reached", "quota": quota}), status
    # Accept both JSON bodies (existing clients) and multipart form uploads
    # (document attachments). Multipart fields arrive in request.form.
    is_multipart = request.content_type is not None and "multipart" in request.content_type
    if is_multipart:
        data = dict(request.form)
        doc_file = request.files.get("document")
    else:
        data = request.get_json(silent=True) or {}
        doc_file = None
    prompt = str(data.get("prompt", "")).strip()
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400
    if len(prompt) > MAX_PROMPT_CHARS:
        return jsonify({"error": f"prompt must be at most {MAX_PROMPT_CHARS} characters"}), 400

    try:
        duration_seconds = float(data.get("duration_seconds", 120))
    except (TypeError, ValueError):
        return jsonify({"error": "duration_seconds must be a number"}), 400

    if duration_seconds < 20 or duration_seconds > MAX_VIDEO_DURATION_SECONDS:
        return jsonify({
            "error": f"duration_seconds must be between 20 and {MAX_VIDEO_DURATION_SECONDS}"
        }), 400

    # ---- optional document input ---------------------------------------------
    source_text = ""
    full_text = ""
    source_document_id = None
    if doc_file is not None and getattr(doc_file, "filename", None):
        suffix = Path(str(doc_file.filename)).suffix.lower()
        if suffix in {".txt", ".md", ".markdown", ".pdf", ".docx", ".pptx"}:
            safe_name = secure_filename(str(doc_file.filename)) or f"source{suffix}"
            doc_path = DOC_UPLOAD_DIR / f"{uuid.uuid4().hex[:12]}{suffix}"
            doc_file.save(str(doc_path))
            if doc_path.stat().st_size > MAX_UPLOAD_BYTES:
                doc_path.unlink(missing_ok=True)
                return jsonify({"error": f"document must be no larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"}), 413
            # Full extraction (up to DOC_MAX_CHARS) feeds the per-scene
            # visual-planner excerpts; only a small TPM-safe anchor goes to
            # the director so the planning call never breaches the TPM limit.
            full_text = _extract_document_text(doc_path)[:DOC_MAX_CHARS]
            source_text = full_text[:DOC_DIRECTOR_ANCHOR_CHARS]
            source_document_id = supabase_store.create_source_document(
                user_id,
                safe_name,
                str(doc_path),
                getattr(doc_file, "mimetype", None),
                doc_path.stat().st_size,
                len(full_text),
            )
            if supabase_store.enabled and not source_document_id:
                doc_path.unlink(missing_ok=True)
                return jsonify({"error": "Document storage is temporarily unavailable"}), 503
            print(f"Document uploaded: {doc_file.filename} ({len(full_text)} chars; "
                  f"{len(source_text)} char anchor to director)")
        else:
            return jsonify({"error": f"unsupported document type: {suffix}"}), 400

    job_id = uuid.uuid4().hex[:12]
    style = str(data.get("style", "")).strip().lower()

    payload = {
        "prompt": prompt,
        "duration_seconds": duration_seconds,
        "audience": data.get("audience", "general learner"),
        "sketch": bool(data.get("sketch", False)),
        "mode": "streaming" if _pick_streaming_mode(data) else "batch",
        "source_text": source_text or "",
        "full_source_text": full_text if full_text else "",
        "style": style,
        "captions": data.get("captions"),
        # Feature E: token-economy quality dial (fast / balanced / rich).
        # Fast mode switches the scene planner to the smaller 20b model and
        # cuts the per-scene element budget; the director always stays on
        # the strong 120b model.
        "quality_mode": str(data.get("quality_mode") or "balanced").strip().lower(),
        "user_id": user_id,
        "source_document_id": source_document_id,
    }
    reservation = supabase_store.create_job(user_id, job_id, payload)
    if not reservation.get("allowed", False):
        supabase_store.delete_source_document(user_id, source_document_id)
        status = 503 if reservation.get("error") else 429
        reason = reservation.get("reason")
        message = reservation.get("error") or {
            "duration_limit": f"Your plan supports videos up to {reservation.get('max_duration_seconds')} seconds",
            "concurrency_limit": "Finish an active video before starting another",
            "quota_limit": "Video limit reached",
        }.get(reason, "Video limit reached")
        return jsonify({
            "error": message,
            "quota": reservation,
        }), status

    _set_job(
        job_id,
        status="queued",
        stage="queued",
        progress=0,
        detail="Queued",
        prompt=prompt,
        audience=str(data.get("audience", "general learner")).strip().lower(),
        style=style,
        created_at=_utc_now_iso(),
        scenes=[],
        target_duration_seconds=duration_seconds,
        user_id=user_id,
    )

    if JOB_EXECUTION_MODE == "queue":
        return jsonify({
            "job_id": job_id,
            "status_url": f"/api/jobs/{job_id}",
            "mode": payload["mode"],
            "queued": True,
        }), 202

    if payload["mode"] == "streaming" and ENABLE_STREAMING:
        from segment_pipeline import run_streaming_pipeline

        thread = threading.Thread(
            target=_run_streaming_video_job, args=(job_id, payload), daemon=True
        )
        thread.start()
        return jsonify(
            {
                "job_id": job_id,
                "status_url": f"/api/jobs/{job_id}",
                "mode": "streaming",
                "stream_url": f"/stream/{job_id}",
            }
        ), 202

    thread = threading.Thread(target=_run_video_job, args=(job_id, payload), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id, "status_url": f"/api/jobs/{job_id}", "mode": "batch"}), 202


def _run_streaming_video_job(job_id: str, payload: Dict[str, Any]) -> None:
    """Wrap the overlapped segment pipeline so the legacy job store and the
    browser SocketIO receive progress, and the final MP4 lands in outputs/."""
    import shutil

    from duration_calibrator import calibration_summary

    job_dir = WORK_DIR / job_id
    output_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Per-job segment event replay buffer so late-connecting browsers
    # can see segments that already became ready.
    with _segment_state_lock:
        _segment_state[job_id] = {"segments": [], "plan": None}

    def on_plan(plan: Dict[str, Any]) -> None:
        with _segment_state_lock:
            state = _segment_state.get(job_id)
            if state is not None:
                state["plan"] = copy.deepcopy(plan)
        _set_job_plan(job_id, plan)
        if plan.get("scenes"):
            _update_job_scene(job_id, 0, status="active")
        _set_job(
            job_id,
            status="running",
            stage="tts",
            progress=14,
            detail="Preparing narration while the first scenes are built",
        )

    def on_segment(seg_info: Dict[str, Any]) -> None:
        with _segment_state_lock:
            state = _segment_state.get(job_id)
            if state is not None:
                state["segments"].append(copy.deepcopy(seg_info))
                completed = len(state["segments"])
                plan = state.get("plan") or {}
                total = max(1, len(plan.get("scenes", [])))
            else:
                completed, total = 1, 1
        scene_index = max(0, int(seg_info.get("scene_id", 1)) - 1)
        _update_job_scene(
            job_id,
            scene_index,
            status="done",
            image_url=seg_info.get("png_url"),
            video_url=seg_info.get("mp4_url"),
            duration_seconds=seg_info.get("duration_seconds"),
        )
        next_index = scene_index + 1
        if next_index < total:
            _update_job_scene(job_id, next_index, status="active")
        _set_job(
            job_id,
            status="running",
            stage="streaming",
            progress=min(92, 20 + round(70 * completed / total)),
            detail=f"{completed}/{total} scenes ready to watch",
        )

    try:
        _raise_if_job_canceled(job_id)
        from run_log import get_summary, log_stage_ts
        log_stage_ts(job_id, "started", "streaming video generation")
        target_seconds = float(payload["duration_seconds"])
        source_text = str(payload.get("source_text", "")).strip()
        full_source_text = str(payload.get("full_source_text", "")).strip() or source_text
        style = str(payload.get("style", "")).strip().lower() or None

        _set_job(job_id, status="running", stage="planning", progress=4, detail="Locking scope and writing the explanation")
        from segment_pipeline import run_streaming_pipeline

        result = run_streaming_pipeline(
            job_id, payload, socketio=socketio, segment_cb=on_segment,
            plan_cb=on_plan,
            source_text=source_text, full_source_text=full_source_text, style=style,
        )
        _raise_if_job_canceled(job_id)

        # Copy the assembled final.mp4 into the outputs dir for the legacy
        # player and download links (manifest included for batch mode parity).
        final_src = Path(result.get("final_path") or (job_dir / "final.mp4"))
        if final_src.exists():
            shutil.copy2(final_src, output_dir / "final.mp4")

        # Live stream.mp4 also lands in outputs so the video player and the
        # download link always point at a stable location.
        stream_src = job_dir / "stream.mp4"
        if stream_src.exists():
            shutil.copy2(stream_src, output_dir / "stream.mp4")

        log_stage_ts(job_id, "completed", "streaming video generation completed")
        llm_usage = get_summary(job_id)

        manifest = {
            "job_id": job_id,
            "mode": "streaming",
            "request": {
                "prompt": payload["prompt"],
                "target_duration_seconds": target_seconds,
                "audience": payload.get("audience", "general learner"),
                "sketch": payload.get("sketch", False),
                "source_document": bool(payload.get("source_text", "").strip()),
                "style": style,
            },
            "tts_backend": resolve_backend(),
            "final_duration_seconds": result["final_duration_seconds"],
            "llm_usage": llm_usage,
            "calibration": result.get("calibration"),
            "runtime_config": result.get("runtime_config"),
            "duration_integrity": result.get("duration_integrity"),
            "scene_count": result.get("scene_count"),
            "stream_url": result.get("stream_url", f"/segments/{job_id}/stream.mp4"),
            "stream_path": str(job_dir / "stream.mp4"),
            "segments": [
                {
                    "scene_id": s["scene_id"],
                    "duration_seconds": s["duration_seconds"],
                    "narration": s["narration"],
                    "visual_prompt": s["visual_prompt"],
                    "predicted_seconds": s.get("predicted_seconds"),
                    "raw_duration_seconds": s.get("raw_duration_seconds"),
                    "boundary_pause_seconds": s.get("boundary_pause_seconds", 0),
                    "selected_beat_ids": s.get("selected_beat_ids", []),
                }
                for s in result.get("segments", [])
            ],
            "plan": result.get("plan"),
        }
        evaluation_path = write_canvas_evaluation(
            job_id=job_id,
            mode="streaming",
            job_dir=job_dir,
            output_dir=output_dir,
            final_video=output_dir / "final.mp4",
            request_data=manifest["request"],
            plan_data=manifest.get("plan"),
        )
        manifest["evaluation_url"] = f"/outputs/{job_id}/{evaluation_path.name}"
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _raise_if_job_canceled(job_id)
        supabase_store.complete_video(payload.get("user_id"), job_id, manifest)
        _raise_if_job_canceled(job_id)

        _set_job(
            job_id,
            status="completed",
            stage="completed",
            progress=100,
            detail="Video ready",
            mode="streaming",
            final_duration_seconds=result["final_duration_seconds"],
            output_url=f"/outputs/{job_id}/final.mp4",
            manifest_url=f"/outputs/{job_id}/manifest.json",
            evaluation_url=f"/outputs/{job_id}/evaluation.json",
            llm_usage=llm_usage,
            llm_cost_usd=llm_usage.get("estimated_usd_cost"),
            llm_cost_inr=llm_usage.get("estimated_inr_cost"),
            plan_summary=manifest["plan"]
            and {
                "title": manifest["plan"].get("title"),
                "scope_summary": manifest["plan"].get("scope_summary"),
                "must_cover": manifest["plan"].get("must_cover", []),
                "out_of_scope": manifest["plan"].get("out_of_scope", []),
                "scene_count": len(manifest["plan"].get("scenes", [])),
                "narration_words": narration_word_count(manifest["plan"]),
            },
        )
        if socketio is not None:
            socketio.emit(
                "job_completed",
                {
                    "job_id": job_id,
                    "final_duration_seconds": result["final_duration_seconds"],
                    "target_duration_seconds": target_seconds,
                    "scene_count": result.get("scene_count", 0),
                    "segment_count": result.get("scene_count", 0),
                    "output_url": f"/outputs/{job_id}/final.mp4",
                    "evaluation_url": f"/outputs/{job_id}/evaluation.json",
                    "calibration": result.get("calibration"),
                    "llm_usage": llm_usage,
                    "llm_cost_usd": llm_usage.get("estimated_usd_cost"),
                    "llm_cost_inr": llm_usage.get("estimated_inr_cost"),
                },
                to=f"job:{job_id}",
            )

    except JobCanceled:
        _set_local_job_canceled(job_id)
    except Exception as exc:
        evaluation = _write_preflight_failure_evaluation(job_id, output_dir, payload, exc)
        try:
            from run_log import log_stage_ts
            log_stage_ts(job_id, "failed", str(exc)[:300])
        except Exception:
            pass
        traceback.print_exc()
        _set_job(
            job_id,
            status="failed",
            stage="failed",
            detail=str(exc),
            error=str(exc),
            evaluation_url=f"/outputs/{job_id}/evaluation.json" if evaluation else None,
        )
        try:
            if socketio is not None:
                socketio.emit(
                    "job_failed",
                    {
                        "job_id": job_id,
                        "error": str(exc),
                        "evaluation_url": f"/outputs/{job_id}/evaluation.json" if evaluation else None,
                    },
                    to=f"job:{job_id}",
                )
        except Exception:
            pass


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def cancel_job(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return jsonify({"error": "job not found"}), 404
    user_id, denied = _authorized_job(job_id)
    if denied:
        return denied

    if not supabase_store.enabled:
        local_job = _get_job(job_id)
        if not local_job:
            return jsonify({"error": "job not found"}), 404
        if local_job.get("status") not in {"queued", "running"}:
            if local_job.get("status") == "canceled":
                return jsonify({"job_id": job_id, "status": "canceled"})
            return jsonify({"error": "This job can no longer be canceled"}), 409

    try:
        result = supabase_store.cancel_job(user_id, job_id)
    except Exception as exc:
        app.logger.error("Unable to cancel job %s: %s", job_id, type(exc).__name__)
        return jsonify({"error": "Cancellation is temporarily unavailable"}), 503

    outcome = str(result.get("outcome") or "")
    if outcome == "not_found":
        return jsonify({"error": "job not found"}), 404
    if outcome == "not_cancelable":
        return jsonify({
            "error": "This job can no longer be canceled",
            "status": result.get("status"),
        }), 409
    if outcome not in {"canceled", "already_canceled"}:
        return jsonify({"error": "Cancellation is temporarily unavailable"}), 503

    _request_job_cancellation(job_id)
    _set_local_job_canceled(job_id)
    return jsonify({"job_id": job_id, "status": "canceled"})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def job_status(job_id: str):
    user_id, auth_error = supabase_store.authenticate(request.headers.get("Authorization", ""))
    if auth_error:
        return jsonify({"error": auth_error}), _auth_error_status(auth_error)
    if not supabase_store.owns_job(user_id, job_id):
        return jsonify({"error": "job not found"}), 404
    job = _get_job(job_id)
    if job and job.get("status") == "completed" and supabase_store.enabled:
        try:
            video = supabase_store.video_record(user_id, job_id)
            if video:
                signed_video = supabase_store.signed_storage_url(
                    "video-assets", video.get("video_storage_path")
                )
                if not signed_video:
                    return jsonify({"error": "Video storage is temporarily unavailable"}), 503
                job["output_url"] = signed_video
                job["final_duration_seconds"] = video.get("duration_seconds")
                job.pop("manifest_url", None)
                job.pop("evaluation_url", None)
        except Exception as exc:
            app.logger.error("Unable to sign completed job %s: %s", job_id, type(exc).__name__)
            return jsonify({"error": "Video storage is temporarily unavailable"}), 503
    # In queue mode the web and render worker are separate processes. The
    # web process's local copy remains at `queued`; Supabase is authoritative
    # for every durable job, including failures and completion progress.
    if supabase_store.enabled:
        try:
            record = supabase_store.job_record(user_id, job_id)
        except Exception as exc:
            app.logger.error("Unable to load durable job %s: %s", job_id, type(exc).__name__)
            return jsonify({"error": "Job status is temporarily unavailable"}), 503
        if record:
            scene_rows = supabase_store.scene_records(user_id, job_id)
            job = {
                "job_id": job_id,
                "status": record.get("status"),
                "stage": record.get("stage"),
                "progress": record.get("progress"),
                "detail": record.get("detail"),
                "error": record.get("error_message"),
                "prompt": record.get("prompt"),
                "audience": record.get("audience"),
                "style": record.get("style"),
                "target_duration_seconds": record.get("requested_duration_seconds"),
                "created_at": record.get("created_at"),
                "scenes": [
                    {
                        "index": int(scene.get("scene_index", index)),
                        "title": str(scene.get("title") or f"Scene {index + 1}"),
                        "narration": str(scene.get("narration") or ""),
                        "status": str(scene.get("status") or "pending"),
                        "image_url": scene.get("image_url"),
                        "video_url": (
                            (scene.get("scene_manifest") or {}).get("live", {}).get("video_url")
                            if isinstance(scene.get("scene_manifest"), dict) else None
                        ),
                        "duration_seconds": scene.get("duration_seconds"),
                    }
                    for index, scene in enumerate(scene_rows)
                ],
            }
            if record.get("status") == "completed":
                video = supabase_store.video_record(user_id, job_id)
                if video:
                    signed_video = supabase_store.signed_storage_url("video-assets", video.get("video_storage_path"))
                    job.update({"output_url": signed_video, "final_duration_seconds": video.get("duration_seconds")})
    if not job:
        # In-memory job state is intentionally lightweight and disappears when
        # the development server reloads. Completed artifacts are authoritative;
        # reconstruct the response so a refreshed UI can still play/download a
        # video instead of exposing only static JSON files.
        if re.fullmatch(r"[0-9a-f]{12}", job_id):
            output_dir = (OUTPUT_DIR / job_id).resolve()
            final_path = output_dir / "final.mp4"
            manifest_path = output_dir / "manifest.json"
            if final_path.is_file() and manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    request_data = manifest.get("request", {})
                    plan_data = manifest.get("plan", {})
                    job = {
                        "job_id": job_id,
                        "status": "completed", "stage": "completed",
                        "mode": manifest.get("mode") or request_data.get("mode") or "batch",
                        "progress": 100, "detail": "Video ready",
                        "target_duration_seconds": request_data.get("target_duration_seconds"),
                        "final_duration_seconds": manifest.get("final_duration_seconds"),
                        "output_url": f"/outputs/{job_id}/final.mp4",
                        "manifest_url": f"/outputs/{job_id}/manifest.json",
                        "evaluation_url": f"/outputs/{job_id}/evaluation.json",
                        "llm_usage": manifest.get("llm_usage", {}),
                        "plan_summary": {
                            "title": plan_data.get("title"),
                            "scope_summary": plan_data.get("scope_summary"),
                            "must_cover": plan_data.get("must_cover", []),
                            "out_of_scope": plan_data.get("out_of_scope", []),
                            "scene_count": len(plan_data.get("scenes", [])),
                            "narration_words": narration_word_count(plan_data),
                        },
                        "prompt": str(request_data.get("prompt", "")),
                        "audience": str(request_data.get("audience", "beginner")),
                        "style": str(request_data.get("style") or "clean-diagrams"),
                        "created_at": _manifest_created_at(job_id),
                        "scenes": [
                            {
                                "index": scene["index"],
                                "title": scene["title"],
                                "narration": scene["narration"],
                                "status": scene["status"],
                                "image_url": scene.get("imageUrl"),
                            }
                            for scene in _manifest_scene_payloads(job_id, manifest)
                        ],
                    }
                except (OSError, ValueError, TypeError):
                    job = None
        if not job:
            return jsonify({"error": "job not found"}), 404
    created_at = str(job.get("created_at", ""))
    if created_at:
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            job["elapsed_sec"] = max(
                0, int((datetime.now(timezone.utc) - created).total_seconds())
            )
        except ValueError:
            job["elapsed_sec"] = 0
    else:
        job["elapsed_sec"] = 0
    return jsonify(_secure_local_media_urls(job))


@app.route("/api/logs/<job_id>")
def job_logs_summary(job_id: str):
    """Per-run logging: totals, per-call tokens, latency, cost estimate."""
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return jsonify({"error": "invalid job id"}), 404
    _, denied = _authorized_job(job_id)
    if denied:
        return denied
    try:
        from run_log import get_summary

        return jsonify(get_summary(job_id))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/logs/<job_id>/calls")
def job_logs_calls(job_id: str):
    """Per-run logging: raw call-by-call records."""
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return jsonify({"error": "invalid job id"}), 404
    _, denied = _authorized_job(job_id)
    if denied:
        return denied
    try:
        from run_log import get_calls

        return jsonify({"job_id": job_id, "calls": get_calls(job_id)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/logs/<job_id>/download")
def job_logs_download(job_id: str):
    """Download every recorded LLM input/output plus usage metadata."""
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return jsonify({"error": "invalid job id"}), 404
    _, denied = _authorized_job(job_id)
    if denied:
        return denied
    try:
        from run_log import get_download_bundle

        body = json.dumps(get_download_bundle(job_id), ensure_ascii=False, indent=2)
        return Response(
            body,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{job_id}-pipeline-trace.json"',
                "Cache-Control": "no-store",
            },
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/keys")
def key_pool_status():
    """Read-only live state of the multi-key API pool (debugging aid)."""
    _, denied = _require_admin()
    if denied:
        return denied
    try:
        from api_key_pool import pool_status
        status = pool_status()
        ok = bool(status)
        return jsonify({"keys": status, "pool_ok": ok})
    except Exception as exc:
        status = [{"error": str(exc)}]
        ok = False
        return jsonify({"keys": status, "pool_ok": ok})


@app.route("/segments/<job_id>/<path:filename>")
def segment_file(job_id: str, filename: str):
    """Serve per-scene streaming assets, the live stream.mp4, and offsets."""
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return jsonify({"error": "invalid job id"}), 404
    denied = _authorize_media(job_id)
    if denied:
        return denied
    clean = filename.replace("\\", "/")
    if ".." in clean or clean.startswith("."):
        return jsonify({"error": "invalid path"}), 404
    parts = [p for p in clean.split("/") if p and p != "."]
    base_dir = (WORK_DIR / job_id / "segments").resolve()

    if not parts:
        return jsonify({"error": "invalid path"}), 404

    if len(parts) == 1:
        # Top-level live assets: stream.mp4 (grows scene by scene),
        # scene_offsets.json (where each scene starts in the timeline),
        # and manifest.json (live per-scene playlist for the chunked player).
        asset = parts[0]
        if asset == "manifest":
            manifest_path = (base_dir.parent / "manifest.json").resolve()
            base = base_dir.parent
            if manifest_path.is_file():
                resp = send_from_directory(base, "manifest.json", mimetype="application/json")
            else:
                # Legacy job without a manifest: empty live playlist.
                resp = jsonify({"version": 0, "total_seconds": 0.0, "fps_uniform": True, "scenes": []})
            resp.headers["Cache-Control"] = "no-cache"
            return resp
        if asset not in {"stream.mp4", "scene_offsets.json"}:
            return jsonify({"error": "invalid segment path"}), 404
        asset_path = (base_dir.parent / asset).resolve()
        base = base_dir.parent
        if not str(asset_path).startswith(str(base)) or not asset_path.is_file():
            return jsonify({"error": "segment asset not found"}), 404
        resp = send_from_directory(base, asset, as_attachment=False)
        if asset == "stream.mp4":
            resp.headers["Accept-Ranges"] = "bytes"
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    scene_dir, asset = parts[0], parts[1]
    if not re.fullmatch(r"scene_\d{3}", scene_dir):
        return jsonify({"error": "invalid segment path"}), 404
    directory = (base_dir / scene_dir).resolve()
    if not str(directory).startswith(str(base_dir)) or not directory.is_dir():
        return jsonify({"error": "segment not found"}), 404
    asset_path = (directory / asset).resolve()
    if not str(asset_path).startswith(str(directory)) or not asset_path.is_file():
        return jsonify({"error": "segment asset not found"}), 404
    return send_from_directory(directory, asset, as_attachment=False)


if ENABLE_STREAMING:

    @app.route("/stream/<job_id>")
    def stream_player(job_id: str):
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):
            return jsonify({"error": "invalid job id"}), 404
        denied = _authorize_media(job_id)
        if denied:
            return denied
        job = _get_job(job_id)
        return render_template(
            "stream_player.html",
            job_id=job_id,
            mode=job.get("mode", "streaming"),
            target_seconds=job.get("target_duration_seconds"),
        )

    @socketio.on("connect")
    def on_socketio_connect(auth=None):
        token = str((auth or {}).get("token") or "").strip()
        user_id, error = supabase_store.authenticate(f"Bearer {token}" if token else "")
        if error or (supabase_store.enabled and not user_id):
            return False
        return True

    @socketio.on("rejoin")
    def on_rejoin(data):
        """Late-connecting browser asks for replay of already-ready segments."""
        job_id = str(data.get("job_id", ""))
        token = str(data.get("token") or "").strip()
        user_id, error = supabase_store.authenticate(f"Bearer {token}" if token else "")
        if error or not supabase_store.owns_job(user_id, job_id):
            disconnect()
            return
        room = f"job:{job_id}"
        join_room(room)
        with _segment_state_lock:
            state = _segment_state.get(job_id)
        if state is None:
            return
        for seg in state["segments"]:
            socketio.emit("segment_ready", {"job_id": job_id, "segment": seg}, to=room)
        if state.get("plan"):
            socketio.emit("plan_ready", {"job_id": job_id, "plan_summary": state["plan"]}, to=room)


@app.route("/outputs/<job_id>/<path:filename>")
def output_file(job_id: str, filename: str):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return jsonify({"error": "invalid job id"}), 404
    denied = _authorize_media(job_id)
    if denied:
        return denied
    directory = (OUTPUT_DIR / job_id).resolve()
    if not directory.is_dir():
        return jsonify({"error": "job output not found"}), 404
    return send_from_directory(directory, filename, as_attachment=False)


# Backward-compatible single-scene endpoint from your original app.
@app.route("/generate", methods=["POST"])
def generate_single_svg():
    if IS_PRODUCTION or os.environ.get("ENABLE_LEGACY_GENERATE", "0") != "1":
        return jsonify({"error": "not found"}), 404
    user_id, auth_error = supabase_store.authenticate(request.headers.get("Authorization", ""))
    if auth_error:
        return jsonify({"error": auth_error}), _auth_error_status(auth_error)
    try:
        data = request.get_json(silent=True) or {}
        text = str(data.get("text", "")).strip()
        sketch = bool(data.get("sketch", False))
        if not text:
            return jsonify({"error": "Text is required."}), 400
        plan = create_visual_plan(text)
        plan = repair_post_continuity_plan(plan)
        plan = normalize_visual_plan(plan)
        plan = apply_graphic_style(plan)
        svg, used_icons = render_visual_plan(plan, sketch=sketch)
        return jsonify(
            {
                "plan": plan,
                "scene": plan,
                "icons": used_icons,
                "svg": svg,
                "review_mode": PLAN_REVIEW_MODE,
            }
        )
    except Exception as error:
        traceback.print_exc()
        return jsonify({"error": str(error)}), 500


# Keep this route after every API, generated-asset, streaming, and legacy
# endpoint. Flask's specific routes win; this fallback exists solely so React
# Router deep links resolve to the same application shell on a single port.
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_react_app(path: str):
    protected_prefixes = (
        "api", "media", "outputs", "segments", "socket.io", "stream", "generate",
    )
    first_segment = path.split("/", 1)[0] if path else ""
    if first_segment in protected_prefixes:
        return jsonify({"error": "not found"}), 404

    if FRONTEND_DIST.is_dir():
        requested = (FRONTEND_DIST / path).resolve()
        try:
            requested.relative_to(FRONTEND_DIST.resolve())
        except ValueError:
            return jsonify({"error": "invalid path"}), 404
        if path and requested.is_file():
            return send_from_directory(FRONTEND_DIST, path)
        return send_from_directory(FRONTEND_DIST, "index.html")

    # Preserve the previous development UI until the first frontend build.
    return render_template("index.html")


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0").strip() == "1"
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=debug,
        threaded=True,
        use_reloader=debug,
    )
