import os
import math
from dataclasses import dataclass
from typing import Dict, Optional

from openai import OpenAI


SUPPORTED_PROVIDERS = {"nvidia", "groq", "openrouter", "cerebras"}

_DEFAULT_BASE_URLS = {
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "cerebras": "https://api.cerebras.ai/v1",
}

_DEFAULT_MODELS = {
    "nvidia": {
        "director": "meta/llama-3.1-8b-instruct",
        "planner": "meta/llama-3.1-8b-instruct",
    },
    "groq": {
        "director": "openai/gpt-oss-120b",
        "planner": "qwen/qwen3.6-27b",
    },
    "openrouter": {
        "director": "openai/gpt-oss-120b",
        "planner": "qwen/qwen3.6-27b",
    },
    "cerebras": {
        "director": "gpt-oss-120b",
        "planner": "gpt-oss-120b",
    },
}

_GROQ_FREE_TPM_LIMITS = {
    "allam-2-7b": 6000,
    "groq/compound": 70000,
    "groq/compound-mini": 70000,
    "llama-3.1-8b-instant": 6000,
    "llama-3.3-70b-versatile": 12000,
    "meta-llama/llama-prompt-guard-2-22m": 15000,
    "meta-llama/llama-prompt-guard-2-86m": 15000,
    "openai/gpt-oss-120b": 8000,
    "openai/gpt-oss-20b": 8000,
    "openai/gpt-oss-safeguard-20b": 8000,
    "qwen/qwen3.6-27b": 8000,
}

# Groq also enforces an output-only tokens-per-minute ceiling (OTPM). It is
# independent from TPM, so fitting input + output under 8K is insufficient for
# models such as Qwen 3.6 whose on-demand output allowance is 1K.
_GROQ_FREE_OTPM_LIMITS = {
    "qwen/qwen3.6-27b": 1000,
}


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: Optional[str]
    base_url: str
    default_headers: Dict[str, str]


def _provider_for(role: str) -> str:
    explicit = os.environ.get(f"{role.upper()}_PROVIDER", "").strip()
    if not explicit:
        explicit = os.environ.get("LLM_PROVIDER", "").strip()
    if explicit:
        provider = explicit.lower()
    else:
        # Production historically defaulted to NVIDIA even when only a Groq
        # or Cerebras key was configured. Select the first actually usable
        # provider instead of binding the process to a missing credential.
        provider = next(
            (
                candidate
                for candidate in ("groq", "cerebras", "openrouter", "nvidia")
                if os.environ.get(f"{candidate.upper()}_API_KEY", "").strip()
            ),
            "groq",
        )

    if provider not in SUPPORTED_PROVIDERS:
        choices = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ValueError(
            f"Unsupported {role} LLM provider {provider!r}. Choose one of: {choices}."
        )
    return provider


def get_llm_config(role: str) -> LLMConfig:
    role = role.strip().lower()
    if role not in {"director", "planner"}:
        raise ValueError("LLM role must be 'director' or 'planner'.")

    provider = _provider_for(role)
    prefix = provider.upper()

    api_key = os.environ.get(f"{prefix}_API_KEY")
    base_url = os.environ.get(
        f"{prefix}_BASE_URL",
        _DEFAULT_BASE_URLS[provider],
    ).rstrip("/")

    provider_model = os.environ.get(
        f"{prefix}_{role.upper()}_MODEL"
    )
    legacy_model = os.environ.get(
        "DIRECTOR_MODEL" if role == "director" else "PLANNER_MODEL"
    )
    if not legacy_model and role == "planner":
        legacy_model = os.environ.get("SCENE_MODEL")
    if not legacy_model:
        legacy_model = os.environ.get("NVIDIA_MODEL")

    model = (
        provider_model
        or legacy_model
        or _DEFAULT_MODELS[provider][role]
    )

    headers: Dict[str, str] = {}
    if provider == "openrouter":
        referer = os.environ.get("OPENROUTER_HTTP_REFERER")
        title = os.environ.get("OPENROUTER_APP_TITLE", "Explainora")
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-OpenRouter-Title"] = title

    return LLMConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        default_headers=headers,
    )


def create_llm_client(config: LLMConfig) -> Optional[OpenAI]:
    if not config.api_key:
        return None

    kwargs = {
        "api_key": config.api_key,
        "base_url": config.base_url,
        "max_retries": 0,
    }
    if config.provider == "cerebras":
        kwargs["timeout"] = max(
            5.0,
            float(os.environ.get("CEREBRAS_TIMEOUT_SECONDS", "30")),
        )
    if config.default_headers:
        kwargs["default_headers"] = config.default_headers
    return OpenAI(**kwargs)


def estimate_message_tokens(messages) -> int:
    """Conservative tokenizer-free estimate for request budgeting."""
    characters = 0
    for message in messages:
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, str):
            characters += len(content)
        else:
            characters += len(str(content))
    return 64 + (16 * len(messages)) + math.ceil(characters / 3.0)


def fit_completion_token_budget(
    config: LLMConfig,
    messages,
    requested_tokens: int,
) -> int:
    """Keep a Groq request's input + completion allowance below its TPM cap."""
    requested_tokens = max(1, int(requested_tokens))
    if config.provider != "groq":
        return requested_tokens

    configured_limit = os.environ.get("GROQ_TPM_LIMIT")
    tpm_limit = (
        int(configured_limit)
        if configured_limit
        else _GROQ_FREE_TPM_LIMITS.get(config.model.lower(), 8000)
    )
    safety_tokens = max(
        0,
        int(os.environ.get("GROQ_TPM_SAFETY_TOKENS", "300")),
    )
    estimated_input = estimate_message_tokens(messages)
    available = tpm_limit - safety_tokens - estimated_input

    if available < 512:
        raise RuntimeError(
            "Prompt is too large for the configured Groq TPM limit: "
            f"estimated input={estimated_input}, limit={tpm_limit}, "
            f"safety reserve={safety_tokens}. Shorten the prompt or set "
            "GROQ_TPM_LIMIT to your upgraded tier's value."
        )

    configured_output_limit = os.environ.get("GROQ_OTPM_LIMIT")
    output_limit = (
        int(configured_output_limit)
        if configured_output_limit
        else _GROQ_FREE_OTPM_LIMITS.get(config.model.lower())
    )
    if output_limit is None:
        return min(requested_tokens, available)
    output_reserve = max(
        0,
        int(os.environ.get("GROQ_OTPM_SAFETY_TOKENS", "100")),
    )
    usable_output = output_limit - output_reserve
    if usable_output < 256:
        raise RuntimeError(
            "Configured Groq output-token limit is too small after its safety "
            "reserve. Increase GROQ_OTPM_LIMIT or reduce "
            "GROQ_OTPM_SAFETY_TOKENS."
        )
    return min(requested_tokens, available, usable_output)


def missing_key_message(config: LLMConfig) -> str:
    return (
        f"{config.provider.upper()}_API_KEY environment variable is missing "
        f"for the {config.provider} {config.model} model."
    )


def llm_configuration_readiness() -> Dict[str, object]:
    """Report whether both generation roles have usable credentials."""
    try:
        configs = [get_llm_config("director"), get_llm_config("planner")]
    except (TypeError, ValueError) as exc:
        return {"ok": False, "detail": str(exc)}
    missing = [config.provider for config in configs if not config.api_key]
    if missing:
        providers = ", ".join(dict.fromkeys(missing))
        return {"ok": False, "detail": f"missing API key for {providers}"}
    roles = ", ".join(
        f"{role}:{config.provider}/{config.model}"
        for role, config in zip(("director", "planner"), configs)
    )
    return {"ok": True, "detail": roles}


def _is_transient_transport_error(error: Exception) -> bool:
    """Identify network failures that are safe to retry on another key."""
    error_name = error.__class__.__name__.lower()
    if any(marker in error_name for marker in (
        "apiconnection", "apitimeout", "connecterror", "timeouterror",
    )):
        return True
    return str(error).strip().lower() in {
        "connection error.", "connection error", "connection aborted.",
        "connection reset by peer", "read timed out",
    }


def pooled_create(client, config: "LLMConfig", kwargs: dict, max_key_failovers: int = -1):
    """chat.completions.create with automatic multi-key failover on 429.

    Used by the non-streaming callers (director per-scene repair/review,
    visual engine SVG generation). When a Groq 429 arrives, the failing key
    is quarantined for the rest of the minute and the call is retried on the
    next least-loaded key from the pool. With fewer than `max_key_failovers`
    healthy keys left, the last honest attempt is raised.

    For non-Groq providers (or when GROQ_API_KEYS is empty) this behaves as a
    plain pass-through.
    """
    from openai import RateLimitError

    pool = None
    entry = None
    estimated = 0
    if config.provider in {"groq", "cerebras"}:
        try:
            from api_key_pool import provider_pool

            pool = provider_pool(config.provider)
        except Exception:  # noqa: BLE001 — pool is an optimization
            pool = None

    pool_entries = pool.entries() if pool is not None else []
    capacity_wait_enabled = bool(pool_entries and max_key_failovers < 0)
    max_capacity_wait = max(
        0.0,
        float(os.environ.get(
            f"{config.provider.upper()}_KEY_WAIT_MAX_SECONDS", "70"
        )),
    )
    wait_deadline = None
    if capacity_wait_enabled:
        wait_deadline = __import__("time").monotonic() + max_capacity_wait

    def checkout_distinct(excluded):
        if pool is None:
            return None
        if hasattr(pool, "checkout"):
            return pool.checkout(estimated, exclude_keys=excluded)
        # Compatibility for custom/test pools.
        return pool.best_key(
            estimated, exclude_keys=excluded, allow_quarantined_fallback=False
        )

    if pool is not None and pool_entries:
        messages = kwargs.get("messages", [])
        estimated = float(
            estimate_message_tokens(messages)
            + max(1, int(kwargs.get("max_tokens", 2048)))
        )
        entry = checkout_distinct(set())
        if entry is None:
            client = None
        else:
            client = entry.client

    attempts = 0
    last_error: Optional[Exception] = None
    import time as _time

    stage = str(kwargs.pop("_stage", "llm"))
    job_id = kwargs.pop("_job_id", None)
    request_payload = {
        "messages": kwargs.get("messages", []),
        "parameters": {
            key: value for key, value in kwargs.items()
            if key not in {"messages", "extra_headers", "default_headers"}
        },
    }
    started = _time.perf_counter()
    if pool_entries and not capacity_wait_enabled:
        attempt_limit = len(pool_entries) if max_key_failovers < 0 else min(
            len(pool_entries), max(1, max_key_failovers + 1)
        )
    elif capacity_wait_enabled:
        attempt_limit = 1_000_000  # real bound is the monotonic wait deadline
    else:
        attempt_limit = 1
    tried_keys = set()
    last_retry_after = 0.0
    capacity_wait_seconds = 0.0

    def release_failed_reservation(failed_entry) -> None:
        """A failed transport/429 consumed no completion token allowance."""
        if (
            failed_entry is not None and pool is not None
            and hasattr(pool, "reconcile") and estimated > 0
        ):
            try:
                pool.reconcile(failed_entry, estimated, 0)
            except Exception:  # noqa: BLE001 — accounting cannot mask provider error
                pass

    while attempts < attempt_limit:
        if pool_entries and entry is None:
            remaining = max(0.0, (wait_deadline or 0.0) - _time.monotonic())
            if not capacity_wait_enabled or remaining <= 0:
                raise last_error or RuntimeError(
                    "All configured API keys are at capacity and the bounded wait expired."
                )
            local_wait = pool.seconds_until_capacity(estimated) if hasattr(pool, "seconds_until_capacity") else 0.5
            if local_wait is None:
                raise last_error or RuntimeError("All configured API keys are exhausted for the UTC day.")
            wait_for = min(remaining, max(0.25, float(local_wait), last_retry_after))
            print(
                f"[pool] all keys busy; waiting {wait_for:.2f}s for capacity "
                f"(bounded at {max_capacity_wait:.1f}s)"
            )
            _time.sleep(wait_for)
            capacity_wait_seconds += wait_for
            tried_keys.clear()
            last_retry_after = 0.0
            entry = checkout_distinct(tried_keys)
            if entry is not None:
                client = entry.client
            continue
        attempts += 1
        if entry is not None:
            tried_keys.add(entry.key)
        try:
            # Reserve the projected token load before the network call. This
            # keeps concurrent scenes (and a retry after timeout) from choosing
            # the same apparently-idle key while it is already busy.
            if entry is not None and not hasattr(pool, "checkout"):
                pool.record(entry, estimated)
            response = client.chat.completions.create(**kwargs)
            if job_id:
                try:
                    from run_log import log_llm_call
                    from run_log import usage_details_from_object

                    response_usage = getattr(response, "usage", None)
                    usage_details = usage_details_from_object(response_usage)
                    usage_details["queue_time_ms"] = (
                        float(usage_details.get("queue_time_ms", 0.0) or 0.0)
                        + capacity_wait_seconds * 1000.0
                    )
                    choice = response.choices[0] if getattr(response, "choices", None) else None
                    message = getattr(choice, "message", None)
                    response_payload = {
                        "content": getattr(message, "content", None),
                        "reasoning_content": getattr(message, "reasoning_content", None),
                        "finish_reason": getattr(choice, "finish_reason", None),
                    }
                    log_llm_call(
                        job_id=job_id,
                        stage=stage,
                        provider=config.provider,
                        model=config.model,
                        key_prefix=entry.key[:8] if entry is not None else "",
                        prompt_tokens=int(usage_details.pop("prompt_tokens", 0)),
                        completion_tokens=int(usage_details.pop("completion_tokens", 0)),
                        latency_ms=(_time.perf_counter() - started) * 1000,
                        retries=max(0, attempts - 1),
                        finish_reason=getattr(choice, "finish_reason", None),
                        response_id=getattr(response, "id", None),
                        service_tier=getattr(response, "service_tier", None),
                        usage_source="provider_reported" if response_usage is not None else "unavailable_from_provider",
                        request_payload=request_payload,
                        response_payload=response_payload,
                        **usage_details,
                    )
                    if entry is not None and pool is not None and hasattr(pool, "reconcile"):
                        pool.reconcile(entry, estimated, int(getattr(response_usage, "total_tokens", 0) or estimated))
                except Exception:  # noqa: BLE001 — logging never breaks the call
                    pass
            return response
        except RateLimitError as exc:
            last_error = exc
            release_failed_reservation(entry)
            match = __import__("re").search(
                r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", str(exc), __import__("re").I
            )
            if match:
                last_retry_after = max(last_retry_after, float(match.group(1)) + 0.25)
            if entry is not None and pool is not None:
                error_lower = str(exc).lower()
                sev = (
                    "day" if "per day" in error_lower or "daily" in error_lower
                    else "hour" if "per hour" in error_lower or "hourly" in error_lower
                    else "minute"
                )
                pool.quarantine(entry, severity=sev)
                print(
                    f"[pool] 429 on key {entry.key[:8]}… (severity={sev}); "
                    f"retrying on next distinct key ({attempts}/{attempt_limit})"
                )
            if attempts >= attempt_limit or pool is None or pool.size < 2:
                if job_id:
                    try:
                        from run_log import log_llm_call

                        log_llm_call(
                            job_id=job_id,
                            stage=stage,
                            provider=config.provider,
                            model=config.model,
                            key_prefix=entry.key[:8] if entry is not None else "",
                            prompt_tokens=0,
                            completion_tokens=0,
                            latency_ms=(_time.perf_counter() - started) * 1000,
                            retries=max(0, attempts - 1),
                            status="429_quarantined",
                            error=str(exc)[:300],
                            queue_time_ms=capacity_wait_seconds * 1000.0,
                            usage_source="unavailable_failed_call",
                            request_payload=request_payload,
                            response_payload={"error": str(exc)},
                        )
                    except Exception:  # noqa: BLE001
                        pass
                raise
            try:
                entry2 = checkout_distinct(tried_keys)
                if entry2 is None:
                    if capacity_wait_enabled:
                        entry = None
                        client = None
                        continue
                    raise last_error
                client, entry = entry2.client, entry2
            except Exception:  # noqa: BLE001 — all keys exhausted
                raise last_error
        except Exception as exc:  # noqa: BLE001 — classify transport failures below
            release_failed_reservation(entry)
            if job_id:
                try:
                    from run_log import log_llm_call
                    log_llm_call(
                        job_id=job_id, stage=stage, provider=config.provider,
                        model=config.model,
                        key_prefix=entry.key[:8] if entry is not None else "",
                        prompt_tokens=0, completion_tokens=0,
                        latency_ms=(_time.perf_counter() - started) * 1000,
                        retries=max(0, attempts - 1), status="error",
                        error=str(exc)[:1000], usage_source="unavailable_failed_call",
                        request_payload=request_payload,
                        response_payload={"error": str(exc)},
                    )
                except Exception:
                    pass
            if (
                _is_transient_transport_error(exc)
                and pool is not None
                and entry is not None
                and attempts < attempt_limit
            ):
                try:
                    entry2 = checkout_distinct(tried_keys)
                    if entry2 is not None:
                        print(
                            f"[pool] transient transport error on key {entry.key[:8]}...; "
                            f"retrying on next distinct key ({attempts}/{attempt_limit})"
                        )
                        client, entry = entry2.client, entry2
                        continue
                except Exception:  # noqa: BLE001 — preserve the original transport error
                    pass
            raise

    raise last_error or RuntimeError("pooled_create failed.")
