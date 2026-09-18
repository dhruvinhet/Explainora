"""
run_log.py — Per-run detailed logging.

For every job, a JSONL file at logs/runs/<job_id>.jsonl records each API call:
    stage, provider, model, prompt/completion/total/cached/reasoning tokens,
    provider timing, latency, status, retries, price and timestamp

summary.json holds totals (calls, prompt/completion tokens, latency) and a
rough USD cost estimate based on per-model pricing tables.

Call sites:
- llm_provider.pooled_create   -> log_llm_call (stage from caller kwarg)
- streaming_llm.stream_text    -> log_llm_call (stage="director_stream")
- tts_engine                   -> log_tts_call
- segment_pipeline             -> log stage events (optional high-level)

GET /api/logs/<job_id> serves the summary; GET /api/logs/<job_id>/calls
serves the raw JSONL lines.
"""

import hashlib
import json
import os
import re
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = Path(os.environ.get("RUN_LOGS_DIR", BASE_DIR / "logs" / "runs"))
LOGS_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Groq on-demand list pricing, USD per 1M tokens. Source:
# https://console.groq.com/docs/models (verified 2026-08-22).
# Prompt caching gives a 50% discount on cached input for supported GPT-OSS
# models: https://console.groq.com/docs/prompt-caching
# ---------------------------------------------------------------------------
PRICING_VERSION = "groq-2026-08-22"
PRICING_SOURCE = "https://console.groq.com/docs/models"
MODEL_PRICING: Dict[str, Dict[str, Any]] = {
    "openai/gpt-oss-120b": {
        "input_per_million": 0.15, "output_per_million": 0.60,
        "cached_input_multiplier": 0.5,
    },
    "qwen/qwen3.6-27b": {
        "input_per_million": 0.60, "output_per_million": 3.00,
        "cached_input_multiplier": 1.0,
    },
    "openai/gpt-oss-20b": {
        "input_per_million": 0.075, "output_per_million": 0.30,
        "cached_input_multiplier": 0.5,
    },
    "whisper-large-v3": {"audio_per_hour": 0.111},
    "whisper-large-v3-turbo": {"audio_per_hour": 0.04},
}

# Cerebras serverless pricing, USD per 1M tokens. The director uses the same
# GPT-OSS weights through a direct Cerebras endpoint, with provider-specific
# billing rather than the Groq price above.
CEREBRAS_PRICING_VERSION = "cerebras-2026-08-22"
CEREBRAS_PRICING_SOURCE = "https://inference-docs.cerebras.ai/api-reference/models/public-models"
CEREBRAS_MODEL_PRICING: Dict[str, Dict[str, Any]] = {
    "gpt-oss-120b": {
        "input_per_million": 0.35,
        "output_per_million": 0.75,
        "cached_input_multiplier": 1.0,
    },
}


def _pricing_for(
    model: str,
    table: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    # Longest first prevents whisper-large-v3-turbo matching whisper-large-v3.
    prices = table or MODEL_PRICING
    for prefix, price in sorted(prices.items(), key=lambda item: len(item[0]), reverse=True):
        if model.lower().startswith(prefix.lower()):
            return dict(price)
    return None


def usd_to_inr_rate() -> Dict[str, Any]:
    """Return the recorded FX reference used for reproducible run estimates."""
    try:
        rate = float(os.environ.get("USD_TO_INR_RATE", "95.6416"))
    except (TypeError, ValueError):
        rate = 95.6416
    return {
        "rate": rate,
        "as_of": os.environ.get("USD_TO_INR_RATE_AS_OF", "2026-08-20"),
        "source": os.environ.get(
            "USD_TO_INR_RATE_SOURCE",
            "https://www.fexant.com/bank/RBI",
        ),
    }


def usage_details_from_object(usage: Any) -> Dict[str, Any]:
    """Normalize OpenAI-compatible provider usage without assuming fields."""
    if usage is None:
        return {}
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    provider_cost = getattr(usage, "cost", None)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(getattr(usage, "total_tokens", 0) or (prompt + completion)),
        "cached_prompt_tokens": int(getattr(prompt_details, "cached_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(completion_details, "reasoning_tokens", 0) or 0),
        "accepted_prediction_tokens": int(getattr(completion_details, "accepted_prediction_tokens", 0) or 0),
        "rejected_prediction_tokens": int(getattr(completion_details, "rejected_prediction_tokens", 0) or 0),
        "queue_time_ms": round(float(getattr(usage, "queue_time", 0) or 0) * 1000, 3),
        "prompt_time_ms": round(float(getattr(usage, "prompt_time", 0) or 0) * 1000, 3),
        "completion_time_ms": round(float(getattr(usage, "completion_time", 0) or 0) * 1000, 3),
        "provider_total_time_ms": round(float(getattr(usage, "total_time", 0) or 0) * 1000, 3),
        "provider_cost_usd": (
            round(float(provider_cost), 12)
            if provider_cost is not None
            else None
        ),
    }


def calculate_call_cost(call: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate one call from its actual provider-reported usage."""
    provider = str(call.get("provider", "")).lower()
    model = str(call.get("model", ""))
    fx = usd_to_inr_rate()
    provider_cost = call.get("provider_cost_usd")
    if provider == "openrouter" and provider_cost is not None:
        usd = max(0.0, float(provider_cost))
        return {
            "pricing_status": "provider_reported_openrouter_cost",
            "pricing_version": "openrouter-provider-reported",
            "pricing_source": "https://openrouter.ai/docs/cookbook/administration/usage-accounting",
            "formula": "provider-reported OpenRouter request cost",
            "cost_usd": round(usd, 9),
            "usd_to_inr_rate": fx["rate"],
            "usd_to_inr_as_of": fx["as_of"],
            "usd_to_inr_source": fx["source"],
            "cost_inr": round(usd * fx["rate"], 6),
        }
    if provider == "groq":
        price = _pricing_for(model)
        pricing_status = "estimated_from_groq_list_price"
        pricing_version = PRICING_VERSION
        pricing_source = PRICING_SOURCE
    elif provider == "cerebras":
        price = _pricing_for(model, CEREBRAS_MODEL_PRICING)
        pricing_status = "estimated_from_cerebras_list_price"
        pricing_version = CEREBRAS_PRICING_VERSION
        pricing_source = CEREBRAS_PRICING_SOURCE
    else:
        price = None
        pricing_status = "unavailable_for_provider_or_model"
        pricing_version = PRICING_VERSION
        pricing_source = PRICING_SOURCE
    if price is None:
        return {
            "pricing_status": "unavailable_for_provider_or_model",
            "pricing_version": pricing_version,
            "pricing_source": pricing_source,
            "cost_usd": None,
            "cost_inr": None,
        }

    audio_seconds = max(0.0, float(call.get("audio_seconds", 0) or 0))
    if "audio_per_hour" in price:
        usd = audio_seconds / 3600.0 * float(price["audio_per_hour"])
        formula = "audio_seconds / 3600 × USD_per_audio_hour"
    else:
        input_tokens = max(0, int(call.get("prompt_tokens", 0) or 0))
        output_tokens = max(0, int(call.get("completion_tokens", 0) or 0))
        cached = min(input_tokens, max(0, int(call.get("cached_prompt_tokens", 0) or 0)))
        uncached = input_tokens - cached
        input_rate = float(price["input_per_million"])
        output_rate = float(price["output_per_million"])
        cache_multiplier = float(price.get("cached_input_multiplier", 1.0))
        usd = (
            uncached * input_rate
            + cached * input_rate * cache_multiplier
            + output_tokens * output_rate
        ) / 1_000_000.0
        formula = "((uncached_input × input_rate) + (cached_input × input_rate × cache_multiplier) + (output × output_rate)) / 1,000,000"
    return {
        "pricing_status": pricing_status,
        "pricing_version": pricing_version,
        "pricing_source": pricing_source,
        "rates": price,
        "formula": formula,
        "cost_usd": round(usd, 9),
        "usd_to_inr_rate": fx["rate"],
        "usd_to_inr_as_of": fx["as_of"],
        "usd_to_inr_source": fx["source"],
        "cost_inr": round(usd * fx["rate"], 6),
    }


# ---------------------------------------------------------------------------
# LLM call logging
# ---------------------------------------------------------------------------

_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "x-api-key", "secret",
    "access_token", "refresh_token", "password",
}


def _redact_secrets(value: Any) -> Any:
    """Recursively remove credentials without filtering ordinary model text."""
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _SENSITIVE_KEYS or normalized.endswith("_api_key"):
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = _redact_secrets(item)
        return cleaned
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str):
        cleaned = value
        for name, secret in os.environ.items():
            if not re.search(r"(?:API_KEY|TOKEN|SECRET|AUTHORIZATION)$", name, re.I):
                continue
            if secret and len(secret) >= 8:
                cleaned = cleaned.replace(secret, "[REDACTED]")
        return cleaned
    return value

def log_llm_call(
    job_id: str,
    stage: str,
    provider: str,
    model: str,
    key_prefix: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: float,
    status: str = "ok",
    retries: int = 0,
    error: Optional[str] = None,
    total_tokens: Optional[int] = None,
    cached_prompt_tokens: int = 0,
    reasoning_tokens: int = 0,
    accepted_prediction_tokens: int = 0,
    rejected_prediction_tokens: int = 0,
    queue_time_ms: float = 0.0,
    prompt_time_ms: float = 0.0,
    completion_time_ms: float = 0.0,
    provider_total_time_ms: float = 0.0,
    finish_reason: Optional[str] = None,
    response_id: Optional[str] = None,
    service_tier: Optional[str] = None,
    audio_seconds: float = 0.0,
    transcribed_words: int = 0,
    usage_source: str = "provider_reported",
    provider_cost_usd: Optional[float] = None,
    request_payload: Optional[Dict[str, Any]] = None,
    response_payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one LLM call record to the job's JSONL log."""
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    record = {
        "ts": time.time(),
        "kind": "llm",
        "stage": str(stage),
        "provider": str(provider),
        "model": str(model),
        "key_prefix": str(key_prefix),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": int(total_tokens or (prompt_tokens + completion_tokens)),
        "cached_prompt_tokens": int(cached_prompt_tokens or 0),
        "reasoning_tokens": int(reasoning_tokens or 0),
        "accepted_prediction_tokens": int(accepted_prediction_tokens or 0),
        "rejected_prediction_tokens": int(rejected_prediction_tokens or 0),
        "queue_time_ms": round(float(queue_time_ms or 0), 3),
        "prompt_time_ms": round(float(prompt_time_ms or 0), 3),
        "completion_time_ms": round(float(completion_time_ms or 0), 3),
        "provider_total_time_ms": round(float(provider_total_time_ms or 0), 3),
        "latency_ms": round(float(latency_ms or 0.0), 1),
        "status": str(status),
        "retries": int(retries or 0),
        "error": error,
        "finish_reason": finish_reason,
        "response_id": response_id,
        "service_tier": service_tier,
        "audio_seconds": round(float(audio_seconds or 0), 6),
        "transcribed_words": int(transcribed_words or 0),
        "usage_source": str(usage_source),
        "provider_cost_usd": (
            float(provider_cost_usd)
            if provider_cost_usd is not None
            else None
        ),
        # Deliberately store the complete model boundary, but never clients,
        # headers, or API keys.  This makes a downloaded run sufficient to
        # distinguish an LLM mistake from a downstream normalization mistake.
        "request": _redact_secrets(request_payload)
        if isinstance(request_payload, dict) else None,
        "response": _redact_secrets(response_payload)
        if isinstance(response_payload, dict) else None,
    }
    record.update(calculate_call_cost(record))
    _append(job_id, record)


def log_tts_call(
    job_id: str,
    engine: str,
    fallback_used: bool,
    latency_ms: float,
    status: str = "ok",
    error: Optional[str] = None,
) -> None:
    _append(job_id, {
        "ts": time.time(),
        "kind": "tts",
        "engine": str(engine),
        "fallback_used": bool(fallback_used),
        "latency_ms": round(float(latency_ms or 0.0), 1),
        "status": str(status),
        "error": error,
    })


def log_stage_ts(job_id: str, stage: str, detail: str = "") -> None:
    """Record a pipeline stage timestamp — used to derive wall time and per-
    stage progression for the live cost meter."""
    _append(job_id, {
        "ts": time.time(),
        "kind": "stage",
        "stage": str(stage),
        "detail": str(detail),
    })


def log_local_transform(
    job_id: str, stage: str, *, scene_id: Optional[int] = None,
    candidate_id: str = "", before: Any = None, after: Any = None,
    details: Optional[Dict[str, Any]] = None, status: str = "ok",
) -> None:
    """Record an auditable local boundary without exposing process state."""
    def digest(value: Any) -> Optional[str]:
        if value is None:
            return None
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    _append(job_id, {
        "ts": time.time(), "kind": "local_transform", "stage": str(stage),
        "scene_id": int(scene_id) if scene_id is not None else None,
        "candidate_id": str(candidate_id), "status": str(status),
        "before_hash": digest(before), "after_hash": digest(after),
        "before": _redact_secrets(before) if isinstance(before, (dict, list)) else None,
        "after": _redact_secrets(after) if isinstance(after, (dict, list)) else None,
        "details": _redact_secrets(details) if isinstance(details, dict) else {},
    })


def _append(job_id: str, record: Dict[str, Any]) -> None:
    with _lock:
        path = LOGS_DIR / f"{job_id}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


def get_summary(job_id: str) -> Dict[str, Any]:
    """Build totals + cost estimate from the job's JSONL log."""
    path = LOGS_DIR / f"{job_id}.jsonl"
    calls: List[Dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    calls.append(json.loads(line))
                except ValueError:
                    continue

    llm_calls = [c for c in calls if c.get("kind") == "llm"]
    tts_calls = [c for c in calls if c.get("kind") == "tts"]
    errors = [
        c for c in calls
        if c.get("status") and c.get("status") not in {
            "ok", "429_failover", "repaired", "selected", "shadow",
        }
    ]

    total_input = sum(int(c.get("prompt_tokens", 0)) for c in llm_calls)
    total_output = sum(int(c.get("completion_tokens", 0)) for c in llm_calls)
    total_cached = sum(int(c.get("cached_prompt_tokens", 0)) for c in llm_calls)
    total_reasoning = sum(int(c.get("reasoning_tokens", 0)) for c in llm_calls)
    total_latency_llm = sum(float(c.get("latency_ms", 0)) for c in llm_calls)
    total_latency_tts = sum(float(c.get("latency_ms", 0)) for c in tts_calls)

    cost = 0.0
    cost_inr = 0.0
    unpriced_calls = 0
    per_model: Dict[str, Dict[str, Any]] = {}
    for c in llm_calls:
        model = c.get("model", "?")
        per_model.setdefault(model, {
            "provider": c.get("provider"), "input": 0, "output": 0,
            "cached_input": 0, "reasoning": 0, "calls": 0,
            "cost_usd": 0.0, "cost_inr": 0.0,
        })
        c_in = int(c.get("prompt_tokens", 0))
        c_out = int(c.get("completion_tokens", 0))
        per_model[model]["input"] += c_in
        per_model[model]["output"] += c_out
        per_model[model]["cached_input"] += int(c.get("cached_prompt_tokens", 0) or 0)
        per_model[model]["reasoning"] += int(c.get("reasoning_tokens", 0) or 0)
        per_model[model]["calls"] += 1
        priced = calculate_call_cost(c)
        c_usd = priced.get("cost_usd")
        c_inr = priced.get("cost_inr")
        if c_usd is None:
            unpriced_calls += 1
        else:
            cost += float(c_usd)
            cost_inr += float(c_inr or 0)
            per_model[model]["cost_usd"] += float(c_usd)
            per_model[model]["cost_inr"] += float(c_inr or 0)

    for totals in per_model.values():
        totals["cost_usd"] = round(totals["cost_usd"], 9)
        totals["cost_inr"] = round(totals["cost_inr"], 6)

    key_usage: Dict[str, int] = {}
    for c in llm_calls:
        kp = c.get("key_prefix") or "?"
        key_usage[kp] = key_usage.get(kp, 0) + 1

    # ---- live cost-meter fields ------------------------------------------------
    # tokens so far (recomputable cheaply; log is append-only)
    tokens = total_input + total_output
    # wall time: from the earliest stage/llm/tts record until now (job running)
    # or until the 'completed' stage marker (job finished).
    started = None
    wall_seconds = None
    completed_ts = None
    for c in calls:
        t = c.get("ts")
        if not isinstance(t, (int, float)) or not t:
            continue
        if started is None:
            started = float(t)
    for c in calls:
        if c.get("kind") == "stage" and str(c.get("stage", "")).strip().lower() in {"completed", "job_completed", "done"}:
            completed_ts = float(c.get("ts", 0) or 0)
    if started:
        # Only report wall time for recent runs (within the last 6 hours) —
        # the log is append-only, so old jobs would otherwise show a huge
        # "wall time" measured from their original start timestamp.
        if started > time.time() - 6 * 3600:
            wall_seconds = round((completed_ts if completed_ts else time.time()) - started, 1)

    call_breakdown: List[Dict[str, Any]] = []
    for index, raw in enumerate(llm_calls, start=1):
        detailed = dict(raw)
        detailed.update(calculate_call_cost(raw))
        detailed.pop("key_prefix", None)
        detailed["call_number"] = index
        call_breakdown.append(detailed)

    return {
        "job_id": job_id,
        "log_exists": path.exists(),
        "total_calls": len(calls),
        "llm_calls": len(llm_calls),
        "tts_calls": len(tts_calls),
        "total_prompt_tokens": total_input,
        "total_completion_tokens": total_output,
        "total_cached_prompt_tokens": total_cached,
        "total_reasoning_tokens": total_reasoning,
        "total_tokens": total_input + total_output,
        "total_llm_latency_seconds": round(total_latency_llm / 1000.0, 2),
        "total_tts_latency_seconds": round(total_latency_tts / 1000.0, 2),
        "estimated_usd_cost": round(cost, 9),
        "estimated_inr_cost": round(cost_inr, 6),
        "unpriced_calls": unpriced_calls,
        "pricing": {
            "status": "complete" if unpriced_calls == 0 else "partial_unknown_provider_or_model",
            "version": PRICING_VERSION,
            "source": PRICING_SOURCE,
            "formula": "sum of each call's model-specific input/output or audio price",
            "usd_to_inr": usd_to_inr_rate(),
            "taxes_included": False,
            "note": "List-price estimate; free-tier credits, contracted pricing and taxes are not included.",
        },
        "errors": len(errors),
        "per_model": per_model,
        "call_breakdown": call_breakdown,
        "per_key": key_usage,
        # live cost-meter payload (consumed by the streaming player's cost chip)
        "live_cost": {
            "tokens": int(tokens),
            "cost_usd": round(cost, 6),
            "cost_inr": round(cost_inr, 4),
            "input_tokens": total_input,
            "output_tokens": total_output,
            "wall_seconds": wall_seconds,
        },
    }


def get_calls(job_id: str) -> List[Dict[str, Any]]:
    path = LOGS_DIR / f"{job_id}.jsonl"
    calls = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    record = json.loads(line)
                    if record.get("kind") == "llm":
                        # Recompute when pricing/FX configuration is updated;
                        # old raw logs remain useful and auditable.
                        record.update(calculate_call_cost(record))
                    calls.append(record)
                except ValueError:
                    continue
    return calls


def get_download_bundle(job_id: str) -> Dict[str, Any]:
    """Return one portable diagnostics document for UI download."""
    return {
        "schema_version": "pipeline-run-bundle-v2",
        "job_id": job_id,
        "summary": get_summary(job_id),
        "calls": get_calls(job_id),
    }


__all__ = [
    "MODEL_PRICING", "PRICING_SOURCE", "PRICING_VERSION",
    "calculate_call_cost", "get_calls", "get_download_bundle", "get_summary", "log_llm_call",
    "log_local_transform", "log_stage_ts", "log_tts_call", "usage_details_from_object",
    "usd_to_inr_rate",
]
