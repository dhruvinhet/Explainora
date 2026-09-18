"""Voice-specific, raw-TTS narration duration calibration.

No LLM is used here. Stable text features are corrected by observations from
the exact backend/model/voice/speed profile. Padded or tempo-modified audio is
never allowed into calibration.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()
ENABLED = os.environ.get("ENABLE_WPS_CALIBRATION", "1").strip().lower() in {"1", "true", "yes", "on"}
SLOPE_OVERRIDE = os.environ.get("DIRECTOR_WPS_SLOPE")
INTERCEPT_OVERRIDE = os.environ.get("DIRECTOR_WPS_INTERCEPT")
MIN_SAMPLES = max(1, int(os.environ.get("MIN_CALIBRATION_SAMPLES", "3")))
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", BASE_DIR / "outputs"))
CALIBRATION_FILE = Path(os.environ.get(
    "DURATION_CALIBRATION_FILE", BASE_DIR / "work" / "duration_calibration.jsonl"
))
DEFAULT_SLOPE = 60.0 / float(os.environ.get("DIRECTOR_WPM", "145"))
DEFAULT_INTERCEPT = float(os.environ.get("DIRECTOR_WPS_INTERCEPT_DEFAULT", "0.15"))
PIPER_COLD_START_WPM = max(
    100.0, min(260.0, float(os.environ.get("PIPER_COLD_START_WPM", "178")))
)
PIPER_COLD_START_FEATURE_SCALE = max(
    0.5, min(1.2, float(os.environ.get("PIPER_COLD_START_FEATURE_SCALE", "0.67")))
)
MIN_SLOPE, MAX_SLOPE = 1.0 / 4.5, 1.0 / 1.5

_samples: Optional[List[Tuple[int, float]]] = None  # legacy-compatible view
_detailed_samples: Optional[List[Dict[str, Any]]] = None
_samples_lock = threading.Lock()
_rejected_samples = 0


def voice_profile() -> Dict[str, Any]:
    backend = os.environ.get("TTS_BACKEND", "auto").strip().lower()
    model = Path(os.environ.get("PIPER_MODEL", "")).name if backend in {"auto", "piper"} else ""
    voice = os.environ.get("TTS_VOICE", "en-us").strip().lower()
    speed = int(os.environ.get("TTS_SPEED_WPM", "145"))
    length_scale = round(float(os.environ.get("PIPER_LENGTH_SCALE", "1.08")), 4)
    language = os.environ.get("TTS_LANGUAGE", voice.split("-")[0] or "unknown").strip().lower()
    raw = f"{backend}|{model}|{voice}|{speed}|{length_scale}|{language}"
    return {
        "id": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        "backend": backend, "model": model, "voice": voice,
        "speed_wpm": speed, "length_scale": length_scale, "language": language,
    }


def text_features(text: str) -> Dict[str, int]:
    text = str(text or "").strip()
    words = re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE)
    alpha_words = [re.sub(r"[^A-Za-z]", "", word) for word in words]
    syllables = sum(max(1, len(re.findall(r"[aeiouy]+", word.lower()))) for word in alpha_words if word)
    return {
        "words": len(words), "characters": sum(len(word) for word in words),
        "syllable_proxy": syllables, "sentences": len(re.findall(r"[.!?]+", text)),
        "commas": len(re.findall(r"[,;:]", text)),
        "numbers": len(re.findall(r"\b\d+(?:[.,]\d+)?\b", text)),
        "abbreviations": len(re.findall(r"\b(?:[A-Z]{2,}|(?:[A-Za-z]\.){2,})\b", text)),
        "long_words": sum(1 for word in alpha_words if len(word) >= 9),
    }


def _base_feature_seconds(features: Dict[str, int]) -> float:
    words = features.get("words", 0)
    if words <= 0:
        return 0.0
    base = DEFAULT_INTERCEPT + DEFAULT_SLOPE * words
    base += 0.012 * features.get("characters", 0)
    base += 0.018 * features.get("syllable_proxy", 0)
    base += 0.11 * features.get("sentences", 0) + 0.07 * features.get("commas", 0)
    base += 0.10 * features.get("numbers", 0) + 0.08 * features.get("abbreviations", 0)
    base += 0.025 * features.get("long_words", 0)
    return max(0.1, base)


def _load_detailed_samples() -> List[Dict[str, Any]]:
    if not CALIBRATION_FILE.is_file():
        return []
    try:
        lines = CALIBRATION_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict) and row.get("raw_tts") is True and row.get("features"):
            rows.append(row)
    return rows[-1000:]


def detailed_samples() -> List[Dict[str, Any]]:
    global _detailed_samples
    if _detailed_samples is None:
        _detailed_samples = _load_detailed_samples()
    return _detailed_samples


def _profile_rows(profile: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    profile_id = (profile or voice_profile())["id"]
    return [row for row in detailed_samples() if row.get("profile_id") == profile_id]


def _profile_scale(profile: Optional[Dict[str, Any]] = None) -> Tuple[float, int]:
    selected = profile or voice_profile()
    ratios = []
    for row in _profile_rows(selected):
        base = _base_feature_seconds(row.get("features") or {})
        actual = float(row.get("measured_seconds") or 0)
        if base > 0 and actual > 0:
            ratios.append(actual / base)
    if len(ratios) < MIN_SAMPLES:
        if selected.get("backend") in {"auto", "piper"} and selected.get("model"):
            return PIPER_COLD_START_FEATURE_SCALE, len(ratios)
        return 1.0, len(ratios)
    return max(0.65, min(1.45, statistics.median(ratios[-100:]))), len(ratios)


def estimate_text_seconds(text: str, profile: Optional[Dict[str, Any]] = None) -> float:
    scale, _ = _profile_scale(profile)
    return round(_base_feature_seconds(text_features(text)) * scale, 4)


def _load_samples() -> List[Tuple[int, float]]:
    return [(int(row["features"].get("words", 0)), float(row["measured_seconds"]))
            for row in detailed_samples() if int(row.get("features", {}).get("words", 0)) > 0]


def samples() -> List[Tuple[int, float]]:
    global _samples
    if _samples is None:
        _samples = _load_samples()
    return _samples


def _least_squares(pairs: List[Tuple[int, float]]) -> Optional[Tuple[float, float]]:
    n = len(pairs)
    if n < 2:
        return None
    sx, sy = sum(w for w, _ in pairs), sum(s for _, s in pairs)
    sxx, sxy = sum(w * w for w, _ in pairs), sum(w * s for w, s in pairs)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return None
    a = (n * sxy - sx * sy) / denom
    return a, (sy - a * sx) / n


def model() -> Tuple[float, float, int]:
    pairs = [(int(row["features"].get("words", 0)), float(row["measured_seconds"]))
             for row in _profile_rows() if row.get("features", {}).get("words")]
    if SLOPE_OVERRIDE:
        try:
            slope = float(SLOPE_OVERRIDE)
        except ValueError:
            slope = DEFAULT_SLOPE
        return slope, float(INTERCEPT_OVERRIDE) if INTERCEPT_OVERRIDE else DEFAULT_INTERCEPT, len(pairs)
    if not ENABLED:
        return DEFAULT_SLOPE, DEFAULT_INTERCEPT, 0
    if len(pairs) < MIN_SAMPLES:
        profile = voice_profile()
        if profile.get("backend") in {"auto", "piper"} and profile.get("model"):
            return 60.0 / PIPER_COLD_START_WPM, DEFAULT_INTERCEPT, len(pairs)
        return DEFAULT_SLOPE, DEFAULT_INTERCEPT, len(pairs)
    # Scene samples occupy a narrow word-count range, which makes an OLS
    # intercept/slope pair unstable (one slow short sentence can make the
    # inverse budget miss by dozens of words). Median seconds-per-word is the
    # robust statistic we actually need for first-pass word budgeting.
    seconds_per_word = statistics.median(
        seconds / words for words, seconds in pairs if words > 0 and seconds > 0
    )
    return max(MIN_SLOPE, min(MAX_SLOPE, seconds_per_word)), 0.0, len(pairs)


def estimate_seconds(words: int) -> float:
    a, b, _ = model()
    return max(b, a * int(words) + b) if words else b


def estimate_words(target_seconds: float) -> int:
    a, b, _ = model()
    return max(1, round((max(0.1, float(target_seconds)) - b) / a)) if a > 0 else 0


def duration_budget(target_seconds: float, scene_count: int) -> Dict[str, Any]:
    target = max(1.0, float(target_seconds))
    _scale, count = _profile_scale()
    reserve = float(os.environ.get(
        "NARRATION_PAUSE_RESERVE_RATIO" if count >= max(10, MIN_SAMPLES)
        else "NARRATION_COLD_START_PAUSE_RATIO", "0.06" if count >= max(10, MIN_SAMPLES) else "0.10"
    ))
    reserve = max(0.02, min(0.15, reserve))
    spoken = target * (1.0 - reserve)
    return {
        "target_seconds": round(target, 4), "spoken_target_seconds": round(spoken, 4),
        "boundary_pause_reserve_seconds": round(target - spoken, 4),
        "boundary_pause_reserve_ratio": round(reserve, 4), "target_words": estimate_words(spoken),
        "scene_count": max(1, int(scene_count)), "voice_profile": voice_profile(),
        "profile_sample_count": count,
    }


def record_text_sample(text: str, measured_seconds: float, *, profile: Optional[Dict[str, Any]] = None,
                       raw_tts: bool, tempo_factor: float = 1.0) -> bool:
    global _samples, _detailed_samples, _rejected_samples
    features = text_features(text)
    try:
        measured, tempo = float(measured_seconds), float(tempo_factor)
    except (TypeError, ValueError):
        measured, tempo = 0.0, 0.0
    wps = features["words"] / measured if measured > 0 else 0.0
    accepted = ENABLED and raw_tts and features["words"] > 0 and measured > 0 \
        and abs(tempo - 1.0) < 1e-9 and 1.0 <= wps <= 5.5
    with _samples_lock:
        if not accepted:
            _rejected_samples += 1
            return False
        selected = profile or voice_profile()
        row = {"version": 2, "profile_id": selected["id"], "profile": selected,
               "text_hash": hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:20],
               "features": features, "measured_seconds": round(measured, 4),
               "raw_tts": True, "tempo_factor": 1.0}
        CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CALIBRATION_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        if _detailed_samples is None:
            _detailed_samples = _load_detailed_samples()
        else:
            _detailed_samples.append(row)
        if _samples is None:
            _samples = _load_samples()
        else:
            _samples.append((features["words"], measured))
    return True


def record_sample(words: int, measured_seconds: float, *, raw_tts: bool, tempo_factor: float = 1.0) -> bool:
    """Compatibility API; production paths record text-feature observations."""
    global _samples, _rejected_samples
    try:
        words, measured, tempo = int(words), float(measured_seconds), float(tempo_factor)
    except (TypeError, ValueError):
        words, measured, tempo = 0, 0.0, 0.0
    wps = words / measured if measured > 0 else 0.0
    accepted = ENABLED and raw_tts and words > 0 and measured > 0 \
        and abs(tempo - 1.0) < 1e-9 and 1.0 <= wps <= 5.5
    with _samples_lock:
        if not accepted:
            _rejected_samples += 1
            return False
        if _samples is None:
            _samples = _load_samples()
        _samples.append((words, measured))
    return True


def calibration_summary() -> Dict[str, Any]:
    a, b, n = model()
    scale, feature_n = _profile_scale()
    return {
        "enabled": ENABLED, "samples": n, "feature_samples": feature_n,
        "voice_profile": voice_profile(), "feature_scale": round(scale, 6),
        "slope_seconds_per_word": round(a, 6), "intercept_seconds": round(b, 4),
        "words_per_second": round(1.0 / a, 4) if a > 0 else None,
        "estimated_wpm": round(60.0 / a, 2) if a > 0 else None,
        "sample_policy": "raw_tts_only", "predictor": "voice_specific_text_features_v2",
        "rejected_samples_this_process": _rejected_samples,
    }
