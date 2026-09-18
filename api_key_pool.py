"""
api_key_pool.py — Multi-key Groq (or any OpenAI-compatible) API key pool.

Problem it solves:
A single Groq API key has a TPM (tokens-per-minute) cap. With SCENE_WORKERS=2
and later up to 8 parallel workers, one key either throttles (429) or forces
us to queue everything. A pool of N keys spreads scenes across keys so up to
N scenes can generate truly in parallel, and any key that hits its quota is
automatically replaced by a healthy one.

Design — "weighted least-load ledger":

1. Pool: GROQ_API_KEYS env holds a comma-separated list of keys
   (primary GROQ_API_KEY is added automatically). Each key gets its own
   OpenAI client (same base URL + model), plus a per-minute token ledger.

2. Load-balancing at checkout time:
   - Every LLM request estimates its token cost (input estimate + budgeted
     output tokens) BEFORE sending.
   - pick_key(estimated_tokens) returns the healthy key whose projected
     usage this minute is LOWEST (ledger sum + this request), preferring
     keys that have not been used this minute at all.
   - That means the first 8 parallel scenes each grab a different key, the
     9th scene reuses the key that will have the smallest projected load,
     and so on — any number of scenes works with any pool size.

3. Quota failover:
   - If a request on key K gets a 429 (or connection error), the ledger
     entry for K is inflated to +inf, quarantining it for the remainder of
     the minute, and the request is transparently retried on the next best
     key. The original caller gets a client for the new key.
   - The Ledger rolls each calendar minute, so quarantined keys come back
     automatically when their minute resets.
   - If ALL keys are quarantined, we fall back to the lowest-ledger key and
     let Groq's retry-after semantics play out (raise after one honest
     attempt rather than deadlocking).

4. Observability:
   - /api/keys (new route, read-only) shows live ledger state per key.
   - Every 429 is logged so you can see failover happen.

Environment:
    GROQ_API_KEYS=gsk_xxxx,gsk_yyyy,...   (extra keys; primary key auto-added)
    GROQ_TPM_LIMIT=<per-key limit>        (fallback 8000, model-based default)
    GROQ_TPM_SAFETY_TOKENS=300            (reserve kept out of the budget)
"""

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from openai import OpenAI

from llm_provider import (
    _DEFAULT_BASE_URLS,
    estimate_message_tokens,
    fit_completion_token_budget,
    get_llm_config,
)


def _parse_keys() -> List[str]:
    return _parse_provider_keys("groq")


def _parse_provider_keys(provider: str) -> List[str]:
    keys: List[str] = []
    seen: set = set()

    def add(k: str) -> None:
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)

    prefix = str(provider).strip().upper()
    # Primary key always first, followed by optional rotating keys.
    add(os.environ.get(f"{prefix}_API_KEY", ""))
    for k in os.environ.get(f"{prefix}_API_KEYS", "").split(","):
        add(k)
    return keys


def configured_key_count(provider: str) -> int:
    """Count distinct configured credentials without exposing their values."""
    return len(_parse_provider_keys(provider))


def recommended_provider_workers(provider: str, configured_workers: int) -> int:
    """Use independent provider organizations without unbounded fan-out."""
    configured = max(1, int(configured_workers))
    if os.environ.get("AUTO_SCALE_PROVIDER_WORKERS", "true").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return configured
    cap = max(1, int(os.environ.get("MAX_PROVIDER_WORKERS", "8")))
    return max(configured, min(cap, max(1, configured_key_count(provider))))


@dataclass
class _KeyEntry:
    key: str
    client: OpenAI
    ledger: Dict[int, float] = field(default_factory=dict)  # minute -> tokens
    quarantined: Dict[int, bool] = field(default_factory=dict)
    day_quarantined: Dict[int, bool] = field(default_factory=dict)  # utc-day -> flag
    hour_quarantined: Dict[int, bool] = field(default_factory=dict)
    request_minutes: Dict[int, int] = field(default_factory=dict)
    request_hours: Dict[int, int] = field(default_factory=dict)
    request_days: Dict[int, int] = field(default_factory=dict)
    token_days: Dict[int, float] = field(default_factory=dict)
    request_times: List[float] = field(default_factory=list)

    def usage_this_minute(self, now: float) -> float:
        minute = int(now // 60)
        return float(self.ledger.get(minute, 0))

    def is_quarantined(self, now: float) -> bool:
        if self.day_quarantined.get(int(now // 86400), False):
            return True
        if self.hour_quarantined.get(int(now // 3600), False):
            return True
        return bool(self.quarantined.get(int(now // 60), False))

    def can_make_request(self, rpm: int, rph: int, rpd: int, now: float) -> bool:
        # Rolling windows avoid a boundary burst such as five calls at :59
        # followed by five more at :00, which a provider may count as ten in
        # one rolling minute.
        return (
            sum(timestamp > now - 60 for timestamp in self.request_times) < rpm
            and sum(timestamp > now - 3600 for timestamp in self.request_times) < rph
            and sum(timestamp > now - 86400 for timestamp in self.request_times) < rpd
        )

    def seconds_until_request_capacity(
        self, rpm: int, rph: int, rpd: int, now: float
    ) -> float:
        waits: List[float] = []
        for window, limit in ((60.0, rpm), (3600.0, rph), (86400.0, rpd)):
            timestamps = sorted(
                timestamp for timestamp in self.request_times
                if timestamp > now - window
            )
            if len(timestamps) >= limit:
                waits.append(timestamps[-limit] + window - now + 0.25)
        return max([0.0, *waits])

    def can_fit(self, tokens: float, limit: float, now: float) -> bool:
        return self.usage_this_minute(now) + tokens <= limit

    def can_fit_day(self, tokens: float, limit: float, now: float) -> bool:
        return self.token_days.get(int(now // 86400), 0.0) + tokens <= limit

    def record(self, tokens: float, now: float) -> None:
        minute = int(now // 60)
        self.ledger[minute] = self.ledger.get(minute, 0) + float(tokens)
        self.quarantined.pop(minute, None)
        self.request_minutes[minute] = self.request_minutes.get(minute, 0) + 1
        hour = int(now // 3600)
        day = int(now // 86400)
        self.request_hours[hour] = self.request_hours.get(hour, 0) + 1
        self.request_days[day] = self.request_days.get(day, 0) + 1
        self.token_days[day] = self.token_days.get(day, 0.0) + float(tokens)
        self.request_times.append(now)

    def reconcile_tokens(self, reserved: float, actual: float, now: float) -> None:
        """Replace a conservative reservation with provider-reported usage."""
        delta = float(actual) - float(reserved)
        minute, day = int(now // 60), int(now // 86400)
        self.ledger[minute] = max(0.0, self.ledger.get(minute, 0.0) + delta)
        self.token_days[day] = max(0.0, self.token_days.get(day, 0.0) + delta)

    def quarantine(self, now: float, severity: str = "minute") -> None:
        """severity='minute' quarantines for the rest of the minute; severity='day' quarantines for the rest of the UTC day (tokens-per-day exhaustion)."""
        if severity == "day":
            # Tokens-per-day exhaustion: stay quarantined until the UTC day
            # rolls over, so this key is not retried today.
            self.day_quarantined[int(now // 86400)] = True
        elif severity == "hour":
            self.hour_quarantined[int(now // 3600)] = True
        else:
            self.quarantined[int(now // 60)] = True

    def cleanup(self, now: float) -> None:
        cutoff = int(now // 60) - 2
        for m in [m for m in self.ledger if m < cutoff]:
            self.ledger.pop(m, None)
            self.quarantined.pop(m, None)
            self.request_minutes.pop(m, None)
        hour = int(now // 3600)
        for h in [h for h in self.request_hours if h != hour]:
            self.request_hours.pop(h, None)
            self.hour_quarantined.pop(h, None)
        # Expire day quarantines when the UTC day has rolled over.
        today = int(now // 86400)
        for d in [d for d in self.day_quarantined if d != today]:
            self.day_quarantined.pop(d, None)
        for d in [d for d in self.request_days if d != today]:
            self.request_days.pop(d, None)
        for d in [d for d in self.token_days if d != today]:
            self.token_days.pop(d, None)
        self.request_times = [
            timestamp for timestamp in self.request_times
            if timestamp > now - 86400
        ]


class APIKeyPool:
    """Thread-safe pool of API-key clients with per-minute TPM accounting."""

    def __init__(self, role: str = "groq") -> None:
        self._role = role
        self.provider = role if role in {"groq", "cerebras"} else "groq"
        self._lock = threading.Lock()
        self._entries: List[_KeyEntry] = []
        prefix = self.provider.upper()
        default_tpm = "0" if self.provider == "groq" else "1000000000"
        self._tpm_limit: float = float(os.environ.get(f"{prefix}_TPM_LIMIT", default_tpm)) or 0
        if not self._tpm_limit:
            self._tpm_limit = 8000.0  # per-key fallback (gpt-oss-120b default)
        unlimited = 2_147_483_647
        # Groq's published free limits for the configured GPT-OSS/Qwen models
        # are 30 RPM, 1K RPD, 8K TPM and 200K TPD. Environment values remain
        # authoritative for developer/enterprise tiers.
        self._rpm_limit = max(1, int(os.environ.get(f"{prefix}_RPM_LIMIT", "5" if self.provider == "cerebras" else "30")))
        self._rph_limit = max(1, int(os.environ.get(f"{prefix}_RPH_LIMIT", "150" if self.provider == "cerebras" else str(unlimited))))
        self._rpd_limit = max(1, int(os.environ.get(f"{prefix}_RPD_LIMIT", "2400" if self.provider == "cerebras" else "1000")))
        self._tpd_limit = max(1.0, float(os.environ.get(f"{prefix}_TPD_LIMIT", "1000000000" if self.provider == "cerebras" else "200000")))

    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self._entries)

    def _ensure_entries(self) -> None:
        """Lazily build one entry per key (idempotent)."""
        keys = _parse_provider_keys(self.provider) if self.provider == "cerebras" else _parse_keys()
        if len(self._entries) >= len(keys):
            return
        base = os.environ.get(f"{self.provider.upper()}_BASE_URL", _DEFAULT_BASE_URLS[self.provider]).rstrip("/")
        for k in keys:
            if not any(e.key == k for e in self._entries):
                self._entries.append(
                    _KeyEntry(
                        key=k,
                        # The pool owns retries and can move a stalled request
                        # to another key instead of retrying the same key.
                        client=OpenAI(api_key=k, base_url=base, max_retries=0),
                    )
                )

    def entries(self) -> List[_KeyEntry]:
        with self._lock:
            self._ensure_entries()
            return list(self._entries)

    def best_key(
        self,
        estimated_tokens: float,
        now: Optional[float] = None,
        *,
        exclude_keys: Optional[set] = None,
        allow_quarantined_fallback: bool = True,
    ) -> Optional[_KeyEntry]:
        """Pick the healthy key with the lowest projected load for this minute.

        Returns None when the pool is empty. Quarantined keys are skipped.
        When every key is quarantined, returns the lowest-ledger key anyway
        (caller must handle a possible 429)."""
        with self._lock:
            self._ensure_entries()
            now = now if now is not None else time.time()
            excluded = exclude_keys or set()
            candidates = [
                e for e in self._entries
                if e.key not in excluded and not e.is_quarantined(now)
            ]
            if not candidates and allow_quarantined_fallback:
                candidates = [e for e in self._entries if e.key not in excluded]
            if not candidates:
                return None
            return min(
                candidates,
                key=lambda e: e.usage_this_minute(now) + estimated_tokens
                if e.can_fit(estimated_tokens, self._tpm_limit, now)
                and e.can_fit_day(estimated_tokens, self._tpd_limit, now)
                else e.usage_this_minute(now) + 1e12,
            )

    def checkout(
        self,
        estimated_tokens: float,
        now: Optional[float] = None,
        *,
        exclude_keys: Optional[set] = None,
    ) -> Optional[_KeyEntry]:
        """Atomically reserve capacity on one healthy distinct key.

        Selection and ledger reservation used to be separate operations, so
        parallel scenes could both observe the same idle key. This method makes
        admission atomic and never knowingly exceeds the configured TPM limit.
        """
        with self._lock:
            self._ensure_entries()
            now = now if now is not None else time.time()
            self._gc(now)
            excluded = exclude_keys or set()
            candidates = [
                entry for entry in self._entries
                if entry.key not in excluded
                and not entry.is_quarantined(now)
                and entry.can_fit(estimated_tokens, self._tpm_limit, now)
                and entry.can_fit_day(estimated_tokens, self._tpd_limit, now)
                and entry.can_make_request(self._rpm_limit, self._rph_limit, self._rpd_limit, now)
            ]
            if not candidates:
                return None
            entry = min(candidates, key=lambda item: item.usage_this_minute(now))
            entry.record(estimated_tokens, now)
            return entry

    def seconds_until_capacity(
        self, estimated_tokens: float, now: Optional[float] = None,
    ) -> Optional[float]:
        """Return a bounded local-ledger wait, or None for day exhaustion."""
        with self._lock:
            self._ensure_entries()
            now = now if now is not None else time.time()
            self._gc(now)
            today = int(now // 86400)
            usable_today = [
                entry for entry in self._entries
                if not entry.day_quarantined.get(today, False)
                and entry.request_days.get(today, 0) < self._rpd_limit
                and entry.token_days.get(today, 0.0) + estimated_tokens <= self._tpd_limit
            ]
            if not usable_today:
                return None
            if any(
                not entry.is_quarantined(now)
                and entry.can_fit(estimated_tokens, self._tpm_limit, now)
                and entry.can_fit_day(estimated_tokens, self._tpd_limit, now)
                and entry.can_make_request(self._rpm_limit, self._rph_limit, self._rpd_limit, now)
                for entry in usable_today
            ):
                return 0.0
            request_waits = [
                entry.seconds_until_request_capacity(
                    self._rpm_limit, self._rph_limit, self._rpd_limit, now
                )
                for entry in usable_today
                if not entry.is_quarantined(now)
            ]
            positive_request_waits = [wait for wait in request_waits if wait > 0]
            if positive_request_waits:
                return min(positive_request_waits)
            current_hour = int(now // 3600)
            if all(
                entry.hour_quarantined.get(current_hour, False)
                or entry.request_hours.get(current_hour, 0) >= self._rph_limit
                for entry in usable_today
            ):
                return max(0.1, 3600.0 - (now % 3600.0) + 0.25)
            # The local ledger rolls on a minute boundary. Add a small guard so
            # concurrent waiters do not wake a few milliseconds too early.
            return max(0.1, 60.0 - (now % 60.0) + 0.25)

    def record(self, entry: _KeyEntry, tokens: float, now: Optional[float] = None) -> None:
        with self._lock:
            entry.record(tokens, now if now is not None else time.time())
            self._gc(now if now is not None else time.time())

    def reconcile(
        self, entry: _KeyEntry, reserved_tokens: float, actual_tokens: float,
        now: Optional[float] = None,
    ) -> None:
        with self._lock:
            current = now if now is not None else time.time()
            entry.reconcile_tokens(reserved_tokens, actual_tokens, current)
            self._gc(current)

    def quarantine(self, entry: _KeyEntry, now: Optional[float] = None, severity: str = "minute") -> None:
        """severity='minute' (default) quarantines for the minute; severity='day' quarantines for the day (tokens-per-day exhausted)."""
        with self._lock:
            entry.quarantine(now if now is not None else time.time(), severity)
            where = "rest of the UTC day" if severity == "day" else ("rest of the hour" if severity == "hour" else "rest of the minute")
            print(f"[pool] key {entry.key[:8]}… quarantined for the {where} (429, {severity})")

    def _gc(self, now: float) -> None:
        for e in self._entries:
            e.cleanup(now)

    # ------------------------------------------------------------------
    def create_client(self, messages, max_tokens: int, max_attempts: int = 3, config=None):
        """Checkout: pick a key by estimated cost, try, fail over on 429.

        Returns (client, chosen_entry, tokens_estimated).
        Raises the last API error if every key fails."""
        self._ensure_entries()
        config = config or get_llm_config("director")
        estimated_input = estimate_message_tokens(messages)
        budgeted = fit_completion_token_budget(config, messages, max_tokens)
        estimated = float(estimated_input + budgeted)

        last_error: Optional[Exception] = None
        tried: set = set()

        for _ in range(max_attempts):
            now = time.time()
            entry = self.checkout(estimated, now, exclude_keys=tried)
            if entry is None:
                raise RuntimeError(
                    "No API keys configured: set GROQ_API_KEY and/or GROQ_API_KEYS."
                )
            if entry.key in tried and len(tried) >= len(self._entries):
                if last_error is not None:
                    raise last_error
                raise RuntimeError(f"All {len(self._entries)} API keys exhausted; last error: {last_error}")
            tried.add(entry.key)
            try:
                return entry.client, entry, estimated
            except Exception as exc:  # noqa: BLE001 — caller supplies client use
                last_error = exc
        raise RuntimeError("Key selection failed.")  # pragma: no cover


# ---------------------------------------------------------------------------
# module-level singletons (one pool per role usage)
# ---------------------------------------------------------------------------

GROQ_POOL: Optional[APIKeyPool] = None
CEREBRAS_POOL: Optional[APIKeyPool] = None


def groq_pool() -> APIKeyPool:
    global GROQ_POOL
    if GROQ_POOL is None:
        GROQ_POOL = APIKeyPool("groq")
    return GROQ_POOL


def cerebras_pool() -> APIKeyPool:
    global CEREBRAS_POOL
    if CEREBRAS_POOL is None:
        CEREBRAS_POOL = APIKeyPool("cerebras")
    return CEREBRAS_POOL


def provider_pool(provider: str) -> Optional[APIKeyPool]:
    if provider == "groq":
        return groq_pool()
    if provider == "cerebras":
        return cerebras_pool()
    return None


def pool_status() -> List[Dict]:
    """Human-readable per-key state for debugging / the /api/keys route."""
    now = time.time()
    out: List[Dict] = []
    for pool in (groq_pool(), cerebras_pool()):
        for e in pool.entries():
            out.append(
                {
                    "provider": pool.provider,
                    "key_prefix": e.key[:8] + "…",
                    "usage_this_minute_tokens": round(e.usage_this_minute(now), 1),
                    "requests_last_minute": sum(
                        timestamp > now - 60 for timestamp in e.request_times
                    ),
                    "requests_last_hour": sum(
                        timestamp > now - 3600 for timestamp in e.request_times
                    ),
                    "requests_last_day": sum(
                        timestamp > now - 86400 for timestamp in e.request_times
                    ),
                    "usage_this_day_tokens": round(e.token_days.get(int(now // 86400), 0.0), 1),
                    "quarantined": e.is_quarantined(now),
                    "tpm_limit": pool._tpm_limit,
                    "rpm_limit": pool._rpm_limit,
                    "rph_limit": pool._rph_limit,
                    "rpd_limit": pool._rpd_limit,
                    "tpd_limit": pool._tpd_limit,
                }
            )
    return out


__all__ = [
    "APIKeyPool", "cerebras_pool", "configured_key_count", "groq_pool",
    "pool_status", "provider_pool", "recommended_provider_workers",
]
