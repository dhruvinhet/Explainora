"""Durable Supabase-backed video render worker.

Run this as a process separate from the Flask web service:

    python worker.py

The database claim function uses ``FOR UPDATE SKIP LOCKED``, so multiple
workers can poll safely. Web deployments should set JOB_EXECUTION_MODE=queue.
"""

from __future__ import annotations

import os
import logging
import signal
import socket
import threading
import time
import uuid
from pathlib import Path

from app import (
    DOC_DIRECTOR_ANCHOR_CHARS,
    DOC_MAX_CHARS,
    DOC_UPLOAD_DIR,
    ENABLE_STREAMING,
    _extract_document_text,
    _clear_job_cancellation,
    _request_job_cancellation,
    _run_streaming_video_job,
    _run_video_job,
    _set_job,
)
from supabase_store import supabase_store


POLL_SECONDS = max(0.5, float(os.environ.get("WORKER_POLL_SECONDS", "2")))
HEARTBEAT_SECONDS = max(10, int(os.environ.get("WORKER_HEARTBEAT_SECONDS", "30")))
STALE_AFTER_SECONDS = max(60, int(os.environ.get("WORKER_STALE_AFTER_SECONDS", "300")))
WORKER_ID = os.environ.get(
    "WORKER_ID", f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
)
_stop = threading.Event()
LOGGER = logging.getLogger(__name__)
_worker_status_lock = threading.Lock()
_last_worker_status: tuple[float, str, str | None] | None = None
_last_status_error_log_at = 0.0
WORKER_HEALTH_FILE = Path(
    os.environ.get(
        "WORKER_HEALTH_FILE",
        "/tmp/pencilvid-worker-heartbeat" if os.name != "nt" else "work/worker-heartbeat",
    )
)


def _request_stop(_signum=None, _frame=None) -> None:
    _stop.set()


def _touch_health(status: str, job_id: str | None = None) -> None:
    global _last_worker_status, _last_status_error_log_at
    WORKER_HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = time.monotonic()
    with _worker_status_lock:
        if (
            _last_worker_status
            and _last_worker_status[1:] == (status, job_id)
            and now - _last_worker_status[0] < HEARTBEAT_SECONDS
        ):
            WORKER_HEALTH_FILE.write_text(str(time.time()), encoding="ascii")
            return
        try:
            supabase_store.set_worker_status(WORKER_ID, status, job_id)
            _last_worker_status = (now, status, job_id)
            WORKER_HEALTH_FILE.write_text(str(time.time()), encoding="ascii")
        except Exception as exc:
            if now - _last_status_error_log_at >= HEARTBEAT_SECONDS:
                LOGGER.warning(
                    "worker_status_update_failed",
                    extra={"error_type": type(exc).__name__},
                )
                _last_status_error_log_at = now


def _heartbeat(job_id: str, job_done: threading.Event) -> None:
    while not job_done.wait(HEARTBEAT_SECONDS) and not _stop.is_set():
        try:
            _touch_health("busy", job_id)
            if not supabase_store.heartbeat_job(job_id, WORKER_ID):
                return
        except Exception:
            # A transient heartbeat failure should not destroy an otherwise
            # healthy render. Stale recovery remains the final safety net.
            continue


def _watch_cancellation(job_id: str, job_done: threading.Event) -> None:
    """Bridge the durable canceled state into the active render process."""
    while not job_done.wait(1.0) and not _stop.is_set():
        try:
            if supabase_store.is_job_canceled(job_id):
                _request_job_cancellation(job_id)
                return
        except Exception:
            # A transient read failure must not fail the render. The next poll
            # retries, while the database has already released the user slot.
            continue


def _hydrate_payload(job: dict) -> tuple[dict, Path | None]:
    request_payload = job.get("request_payload")
    payload = dict(request_payload) if isinstance(request_payload, dict) else {}
    payload.update({
        "prompt": str(job.get("prompt") or ""),
        "duration_seconds": float(job.get("requested_duration_seconds") or 0),
        "audience": str(job.get("audience") or "general learner"),
        "style": str(job.get("style") or "default"),
        "mode": str(job.get("mode") or "batch"),
        "quality_mode": str(job.get("quality_mode") or "balanced"),
        "user_id": str(job.get("user_id") or ""),
        "source_document_id": job.get("source_document_id"),
    })
    downloaded_path: Path | None = None
    if job.get("source_document_id"):
        source = supabase_store.source_document_blob(str(job["source_document_id"]))
        if not source:
            raise RuntimeError("The source document could not be restored from durable storage")
        original_name, body = source
        suffix = Path(original_name).suffix.lower() or ".txt"
        downloaded_path = DOC_UPLOAD_DIR / f"worker-{uuid.uuid4().hex[:12]}{suffix}"
        downloaded_path.write_bytes(body)
        full_text = _extract_document_text(downloaded_path)[:DOC_MAX_CHARS]
        payload["full_source_text"] = full_text
        payload["source_text"] = full_text[:DOC_DIRECTOR_ANCHOR_CHARS]
    else:
        payload["full_source_text"] = ""
        payload["source_text"] = ""
    return payload, downloaded_path


def run_once() -> bool:
    job = supabase_store.claim_job(WORKER_ID)
    if not job:
        return False
    job_id = str(job["id"])
    _clear_job_cancellation(job_id)
    _touch_health("busy", job_id)
    downloaded_path: Path | None = None
    job_done = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(job_id, job_done), daemon=True)
    cancellation = threading.Thread(
        target=_watch_cancellation, args=(job_id, job_done), daemon=True
    )
    heartbeat.start()
    cancellation.start()
    try:
        payload, downloaded_path = _hydrate_payload(job)
        _set_job(
            job_id,
            status="running",
            stage="starting",
            progress=max(1, int(job.get("progress") or 0)),
            detail="Render worker started",
            prompt=payload["prompt"],
            audience=payload["audience"],
            style=payload["style"],
            created_at=str(job.get("created_at") or ""),
            scenes=[],
            target_duration_seconds=payload["duration_seconds"],
            user_id=payload["user_id"],
        )
        if payload["mode"] == "streaming" and ENABLE_STREAMING:
            _run_streaming_video_job(job_id, payload)
        else:
            _run_video_job(job_id, payload)
    except Exception as exc:
        _set_job(
            job_id,
            status="failed",
            stage="failed",
            detail="Video generation failed",
            error=str(exc),
        )
    finally:
        job_done.set()
        heartbeat.join(timeout=1)
        cancellation.join(timeout=1)
        _clear_job_cancellation(job_id)
        if downloaded_path:
            downloaded_path.unlink(missing_ok=True)
        _touch_health("idle")
    return True


def main() -> None:
    if not supabase_store.enabled:
        raise SystemExit("The durable worker requires Supabase server credentials")
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    _touch_health("idle")
    try:
        supabase_store.recover_stale_jobs(STALE_AFTER_SECONDS)
    except Exception:
        pass
    while not _stop.is_set():
        try:
            _touch_health("idle")
            worked = run_once()
        except Exception:
            worked = False
        if not worked:
            _stop.wait(POLL_SECONDS)
    try:
        _touch_health("stopped")
    except Exception:
        pass


if __name__ == "__main__":
    main()
