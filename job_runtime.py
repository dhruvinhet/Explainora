"""Immutable, thread-safe runtime settings for one video job.

Request-specific values must never be written to ``os.environ`` or module
globals: multiple video jobs can run in the same Python process.  A context
variable keeps deep renderer calls convenient while ``activate_runtime`` is
explicitly entered inside every executor worker (contexts are not inherited by
new threads automatically).
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterator, Optional


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def parse_optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return None


def _env_bool(name: str, default: bool) -> bool:
    parsed = parse_optional_bool(os.environ.get(name))
    return default if parsed is None else parsed


def _style_name(value: Any) -> str:
    style = str(value or "default").strip().lower() or "default"
    return style if style in {
        "default", "clean-diagrams", "ocean", "forest", "sunset", "mono"
    } else "default"


@dataclass(frozen=True)
class JobRuntimeConfig:
    job_id: str
    quality_mode: str
    planner_model: str
    style: str
    audience: str
    captions: Optional[bool]
    word_timestamps: bool
    diagrams: bool
    camera_enabled: bool
    camera_seed: int

    @classmethod
    def from_payload(
        cls,
        job_id: str,
        payload: Dict[str, Any],
        *,
        style: Optional[str] = None,
        default_planner_model: str,
    ) -> "JobRuntimeConfig":
        quality = str(payload.get("quality_mode", "balanced")).strip().lower()
        if quality not in {"fast", "balanced", "rich"}:
            quality = "balanced"
        planner_provider = str(
            os.environ.get("PLANNER_PROVIDER", os.environ.get("LLM_PROVIDER", "nvidia"))
        ).strip().upper()
        planner_model = (
            str(
                os.environ.get(
                    f"{planner_provider}_FAST_PLANNER_MODEL",
                    os.environ.get("FAST_PLANNER_MODEL", default_planner_model),
                )
            ).strip()
            if quality == "fast"
            else str(default_planner_model).strip()
        )
        captions = parse_optional_bool(payload.get("captions"))
        requested_words = parse_optional_bool(payload.get("word_timestamps"))
        word_timestamps = quality != "fast" if requested_words is None else requested_words
        requested_diagrams = parse_optional_bool(payload.get("diagrams"))
        diagrams = _env_bool("DIAGRAM_ENABLED", True) if requested_diagrams is None else requested_diagrams
        requested_camera = parse_optional_bool(payload.get("camera"))
        camera_enabled = _env_bool("CAMERA", True) if requested_camera is None else requested_camera
        raw_seed = payload.get("seed")
        if raw_seed in (None, ""):
            raw_seed = int(hashlib.sha256(str(job_id).encode("utf-8")).hexdigest()[:8], 16)
        try:
            camera_seed = int(str(raw_seed))
        except (TypeError, ValueError):
            camera_seed = 0
        return cls(
            job_id=str(job_id),
            quality_mode=quality,
            planner_model=planner_model,
            style=_style_name(style if style is not None else payload.get("style", os.environ.get("STYLE_SKIN", "default"))),
            audience=str(payload.get("audience", "general learner")).strip() or "general learner",
            captions=captions,
            word_timestamps=bool(word_timestamps),
            diagrams=bool(diagrams),
            camera_enabled=bool(camera_enabled),
            camera_seed=camera_seed,
        )

    def diagnostics(self) -> Dict[str, Any]:
        values = asdict(self)
        fingerprint_source = json.dumps(values, sort_keys=True, separators=(",", ":"))
        values["fingerprint"] = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
        values["isolation_scope"] = "immutable_job_context"
        values["process_environment_mutated"] = False
        values["audio_source"] = "raw_tts_plus_boundary_silence"
        values["tempo_adjustment_applied"] = False
        values["calibration_sample_policy"] = "raw_tts_only"
        return values


_CURRENT_RUNTIME: ContextVar[Optional[JobRuntimeConfig]] = ContextVar(
    "simi_job_runtime", default=None
)


def current_runtime() -> Optional[JobRuntimeConfig]:
    return _CURRENT_RUNTIME.get()


@contextmanager
def activate_runtime(config: JobRuntimeConfig) -> Iterator[JobRuntimeConfig]:
    token = _CURRENT_RUNTIME.set(config)
    try:
        yield config
    finally:
        _CURRENT_RUNTIME.reset(token)


__all__ = [
    "JobRuntimeConfig",
    "activate_runtime",
    "current_runtime",
    "parse_optional_bool",
]
