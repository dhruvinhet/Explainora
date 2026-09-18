"""
streaming_llm.py — Streaming LLM completions with incremental JSON parsing.

Provides:
- stream_text(system, payload, max_tokens)  -> generator of text chunks
- stream_scenes(system, payload, max_tokens) -> (generator of (scene_dict, accumulated_text), final_text)

Uses the OpenAI-compatible client from llm_provider (NVIDIA / Groq /
OpenRouter / Cerebras). These providers support stream=True on chat completions.

Why this matters for speed:
- The director no longer blocks until the entire script is done; the first
  scene arrives in ~1-3s and downstream stages (TTS, visuals) can begin.
- Progress events can be pushed to the browser via WebSocket as scenes land.

Fallback: if the provider rejects stream=True, we degrade to a single
non-streaming call (same behavior as before).
"""

import json
import re
from typing import Any, Dict, Generator, List, Optional, Tuple

from llm_provider import (
    LLMConfig,
    create_llm_client,
    estimate_message_tokens,
    fit_completion_token_budget,
    missing_key_message,
)


def _deepseek_thinking_off(model: str) -> bool:
    return "deepseek" in model.lower()


def _build_kwargs(
    config: LLMConfig,
    messages: List[Dict[str, str]],
    max_tokens: Optional[int],
    stream: bool,
) -> Dict[str, Any]:
    if max_tokens is None:
        max_tokens = int(
            __import__("os").environ.get("LLM_STREAM_MAX_TOKENS", "20000")
        )
    safe = max(512, min(int(max_tokens), 32768))

    kwargs: Dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": fit_completion_token_budget(config, messages, safe),
        "stream": stream,
    }
    if stream:
        # OpenAI-compatible providers return authoritative usage on the final
        # chunk only when explicitly requested.
        kwargs["stream_options"] = {"include_usage": True}

    model_lower = config.model.lower()

    if config.provider == "nvidia" and "deepseek" in model_lower:
        kwargs["extra_body"] = {
            "reasoning_effort": "none",
            "chat_template_kwargs": {"thinking": False, "reasoning_effort": "none"},
        }
    elif config.provider in {"groq", "openrouter", "cerebras"}:
        kwargs["response_format"] = {"type": "json_object"}
        if model_lower.startswith("openai/gpt-oss-") or model_lower.startswith("gpt-oss-"):
            if config.provider == "groq":
                kwargs["extra_body"] = {
                    "reasoning_effort": "low",
                    "include_reasoning": False,
                }
            elif config.provider == "openrouter":
                kwargs["extra_body"] = {
                    "reasoning": {"effort": "low", "exclude": True},
                }
            else:
                kwargs["extra_body"] = {"reasoning_effort": "low"}
        elif model_lower.startswith("qwen/"):
            kwargs["extra_body"] = {
                "reasoning_effort": "none",
                "include_reasoning": False,
            }
    elif "nemotron" in model_lower:
        thinking = __import__("os").environ.get("NEMOTRON_THINKING", "false").strip().lower() in {
            "1", "true", "yes", "on",
        }
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": thinking}}

    return kwargs


def _extract_outer_json(text: str) -> Optional[str]:
    """Find the outermost complete JSON object in (possibly partial) text."""
    s = text.strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _checkout_client(config: LLMConfig, messages, max_tokens: int):
    """Pick a pooled API key for this call; fail over on 429 within limits."""
    from api_key_pool import provider_pool

    if config.provider not in {"groq", "cerebras"}:
        client = create_llm_client(config)
        if client is None:
            raise RuntimeError(missing_key_message(config))
        return client, None, 0

    pool = provider_pool(config.provider)
    if pool.size < 1:
        client = create_llm_client(config)
        if client is None:
            raise RuntimeError(missing_key_message(config))
        return client, None, 0

    attempts = 0
    max_attempts = min(pool.size, 3) or 1
    last_error: Optional[Exception] = None
    tried: set = set()
    while attempts < max_attempts:
        client, entry, estimated = pool.create_client(
            messages, max_tokens, config=config
        )
        if entry is None:
            break
        attempts += 1
        if entry.key in tried and attempts >= max_attempts:
            if last_error is not None:
                raise last_error
            raise RuntimeError(f"All {pool.size} API keys exhausted; last error: {last_error}")
        tried.add(entry.key)
        kwargs = _build_kwargs(config, messages, max_tokens, stream=True)
        try:
            return client, entry, estimated
        except Exception as exc:  # noqa: BLE001 — caller uses client; record below
            last_error = exc
            status = getattr(exc, "status_code", None)
            is_429 = (isinstance(status, int) and status == 429) or \
                     (hasattr(exc, "__class__") and "RateLimit" in exc.__class__.__name__)
            if is_429:
                pool.quarantine(entry)
            else:
                raise
    raise last_error or RuntimeError("Key selection failed.")


def stream_text(
    system: str,
    payload: Any,
    config: LLMConfig,
    max_tokens: Optional[int] = None,
    stage: str = "director_stream",
    job_id: Optional[str] = None,
) -> Generator[str, None, str]:
    """Yield incremental text chunks; return (via generator close value) full text.

    Uses the pooled API keys (Groq): the call is charged to the least-loaded
    key and, on a 429, automatically fails over to the next healthy key.
    """
    payload_text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": payload_text},
    ]

    client, entry, estimated = _checkout_client(config, messages, max_tokens)
    if client is None:
        raise RuntimeError(missing_key_message(config))

    kwargs = _build_kwargs(config, messages, max_tokens, stream=True)

    import time as _time

    _started = _time.perf_counter()
    _usage: Dict[str, Any] = {}
    full = []
    try:
        stream = client.chat.completions.create(**kwargs)
        for chunk in stream:
            if not chunk.choices:
                _capture_stream_usage(chunk, _usage)
                continue
            _capture_stream_usage(chunk, _usage)
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                full.append(content)
                yield content
    except Exception as exc:
        _status = getattr(exc, "status_code", None)
        _is_429 = (isinstance(_status, int) and _status == 429) or \
                  (hasattr(exc, "__class__") and "RateLimit" in exc.__class__.__name__)
        if entry is not None and _is_429:
            from api_key_pool import provider_pool
            pool = provider_pool(config.provider)
            pool.quarantine(entry)
            last_rate_error = exc
            for _ in range(max(1, pool.size - 1)):
                print(f"Streaming LLM call 429 on key {entry.key[:8]}…; retrying on another key.")
                client2, entry2, _ = _checkout_client(config, messages, max_tokens)
                kwargs = _build_kwargs(config, messages, max_tokens, stream=True)
                try:
                    stream = client2.chat.completions.create(**kwargs)
                    for chunk in stream:
                        if not chunk.choices:
                            _capture_stream_usage(chunk, _usage)
                            continue
                        _capture_stream_usage(chunk, _usage)
                        delta = chunk.choices[0].delta
                        content = getattr(delta, "content", None)
                        if content:
                            full.append(content)
                            yield content
                    if job_id:
                        _log_stream_call(
                            config, stage, job_id,
                            entry2, _usage, estimated,
                            "429_failover",
                            (_time.perf_counter() - _started) * 1000,
                            messages=messages,
                            response_text="".join(full),
                        )
                    return "".join(full)
                except Exception as retry_exc:
                    retry_status = getattr(retry_exc, "status_code", None)
                    retry_is_429 = (
                        (isinstance(retry_status, int) and retry_status == 429)
                        or "RateLimit" in retry_exc.__class__.__name__
                    )
                    if not retry_is_429 or entry2 is None:
                        raise
                    pool.quarantine(entry2)
                    entry = entry2
                    last_rate_error = retry_exc
            raise last_rate_error
        # Provider rejects streaming; fall back to one non-streaming call.
        print(f"Streaming LLM call failed ({exc}); falling back to non-streaming.")
        kwargs["stream"] = False
        kwargs.pop("stream_options", None)
        try:
            response = client.chat.completions.create(**kwargs)
            text = response.choices[0].message.content or ""
            from run_log import usage_details_from_object
            _usage.update(usage_details_from_object(getattr(response, "usage", None)))
            yield text
            full = [text]
        except Exception:  # noqa: BLE001 — fallback failed too
            raise

    if job_id:
        _log_stream_call(
            config, stage, job_id,
            entry, _usage, estimated,
            "ok",
            (_time.perf_counter() - _started) * 1000,
            messages=messages,
            response_text="".join(full),
        )
    return "".join(full)


def _capture_stream_usage(chunk: Any, usage: Dict[str, int]) -> None:
    """Groq returns usage on the final streaming chunk; capture it."""
    u = getattr(chunk, "usage", None)
    if u is not None:
        from run_log import usage_details_from_object

        usage.update(usage_details_from_object(u))


def _log_stream_call(
    config: LLMConfig,
    stage: str,
    job_id: str,
    entry: Any,
    usage: Dict[str, int],
    estimated: float,
    status: str,
    latency_ms: float,
    messages: Optional[List[Dict[str, str]]] = None,
    response_text: str = "",
) -> None:
    try:
        from run_log import log_llm_call

        log_llm_call(
            job_id=job_id,
            stage=stage,
            provider=config.provider,
            model=config.model,
            key_prefix=entry.key[:8] if entry is not None else "",
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=latency_ms,
            status=status,
            usage_source="provider_reported" if usage else "unavailable_from_provider",
            request_payload={
                "messages": messages or [],
                "parameters": {"stream": True},
            },
            response_payload={"content": response_text},
            **{
                key: value for key, value in usage.items()
                if key not in {"prompt_tokens", "completion_tokens"}
            },
        )
    except Exception:  # noqa: BLE001 — logging never breaks generation
        pass


def _scenes_iter(text: str) -> Generator[Dict[str, Any], None, None]:
    """Parse scene objects from accumulated JSON text.

    Handles a top-level object containing a "scenes": [...] array.
    Yields each scene dict as soon as its closing brace completes.
    """
    json_text = _extract_outer_json(text)
    if not json_text:
        return
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    scenes = data.get("scenes")
    if isinstance(scenes, list):
        for scene in scenes:
            if isinstance(scene, dict) and scene.get("narration") and scene.get("visual_prompt"):
                yield scene


def stream_scenes(
    system: str,
    payload: Any,
    config: LLMConfig,
    max_tokens: Optional[int] = None,
    yield_partial_scenes: bool = True,
    stage: str = "director_stream",
    job_id: Optional[str] = None,
) -> Tuple[Generator[Dict[str, Any], None, None], List[Dict[str, Any]]]:
    """Generator of scene dicts as they are completed by the stream.

    Usage:
        gen, store = stream_scenes(system, payload, config)
        for scene in gen:
            process(scene)          # scene grows as stream arrives (when yield_partial)
        final_scenes = store        # complete scene list after exhaustion

    With yield_partial=True, a partially-complete scene is yielded
    continuously so the TTS stage can start as early as possible (its
    narration keeps growing; we yield updated dicts until stable).
    """
    results: List[Dict[str, Any]] = []

    def gen():
        text = ""
        last_yielded_count = -1
        for chunk in stream_text(system, payload, config, max_tokens, stage=stage, job_id=job_id):
            text += chunk
            scenes = list(_scenes_iter(text))
            if not scenes:
                continue
            if yield_partial_scenes and len(scenes) > last_yielded_count:
                last_yielded_count = len(scenes) - 1
                yield scenes[last_yielded_count]
        final = list(_scenes_iter(text))
        for scene in final:
            if scene not in results:
                results.append(scene)
        # If partial yielding produced growing dicts, finalize:
        for i, scene in enumerate(final):
            if i < len(results):
                results[i] = scene
            else:
                results.append(scene)

    return gen(), results
