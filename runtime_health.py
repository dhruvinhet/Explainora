"""Dependency-aware health checks shared by HTTP and container probes."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix="ready-", delete=True):
            pass
        return True
    except OSError:
        return False


def check_readiness(
    *, store, frontend_dist: Path, work_dir: Path, output_dir: Path,
    production: bool, cache_seconds: float = 10.0,
) -> dict[str, Any]:
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache and now - _cache[0] < cache_seconds:
            return dict(_cache[1])

    from animated_scene import fast_svg_animation_available
    from tts_engine import resolve_backend

    checks: dict[str, dict[str, Any]] = {
        "frontend": {
            "ok": frontend_dist.is_dir() or not production,
            "detail": "built" if frontend_dist.is_dir() else "development fallback",
        },
        "work_directory": {"ok": _writable(work_dir), "detail": "writable"},
        "output_directory": {"ok": _writable(output_dir), "detail": "writable"},
        "ffmpeg": {"ok": bool(shutil.which(os.environ.get("FFMPEG_BIN", "ffmpeg"))), "detail": "available"},
        "ffprobe": {"ok": bool(shutil.which(os.environ.get("FFPROBE_BIN", "ffprobe"))), "detail": "available"},
    }
    try:
        checks["tts"] = {"ok": True, "detail": resolve_backend()}
    except Exception:
        checks["tts"] = {"ok": False, "detail": "unavailable"}
    try:
        renderer_ready = bool(fast_svg_animation_available())
        checks["svg_renderer"] = {
            "ok": renderer_ready,
            "detail": "available" if renderer_ready else "unavailable",
        }
    except Exception:
        checks["svg_renderer"] = {"ok": False, "detail": "unavailable"}
    if production:
        from llm_provider import llm_configuration_readiness

        checks["llm"] = llm_configuration_readiness()

    persistence = store.readiness()
    if store.enabled:
        checks.update(persistence)
        if production:
            checks["worker"] = store.worker_readiness(
                int(os.environ.get("WORKER_HEALTH_MAX_AGE_SECONDS", "90"))
            )
    else:
        checks["database"] = {
            "ok": not production,
            "detail": "not required in development" if not production else "not configured",
        }
        checks["storage"] = dict(checks["database"])

    result = {
        "status": "ready" if all(item["ok"] for item in checks.values()) else "not_ready",
        "checks": checks,
    }
    with _cache_lock:
        _cache = (now, result)
    return dict(result)
