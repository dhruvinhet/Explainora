from types import SimpleNamespace

import api_key_pool
import llm_provider
import openai


class FakeRateLimitError(Exception):
    pass


class FakeCompletions:
    def __init__(self, result):
        self.result = result

    def create(self, **_kwargs):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeEntry:
    def __init__(self, key, result):
        self.key = key
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(result)))


class FakePool:
    def __init__(self, entries):
        self._entries = entries
        self.quarantined = []
        self.selected = []
        self.reconciled = []

    @property
    def size(self):
        return len(self._entries)

    def entries(self):
        return list(self._entries)

    def best_key(self, _estimated, now=None, *, exclude_keys=None, allow_quarantined_fallback=True):
        excluded = exclude_keys or set()
        for entry in self._entries:
            if entry.key not in excluded and entry.key not in self.quarantined:
                self.selected.append(entry.key)
                return entry
        return None

    def record(self, _entry, _estimated):
        return None

    def reconcile(self, entry, reserved, actual):
        self.reconciled.append((entry.key, reserved, actual))

    def quarantine(self, entry, severity="minute"):
        self.quarantined.append(entry.key)


def test_logical_call_exhausts_distinct_keys_until_one_succeeds(monkeypatch):
    success = SimpleNamespace(choices=[])
    entries = [FakeEntry(f"key-{index}", FakeRateLimitError("429 tokens per minute")) for index in range(5)]
    entries.append(FakeEntry("key-working", success))
    pool = FakePool(entries)
    monkeypatch.setattr(openai, "RateLimitError", FakeRateLimitError)
    monkeypatch.setattr(api_key_pool, "groq_pool", lambda: pool)
    config = SimpleNamespace(provider="groq", model="test-model", context_window=32768)

    result = llm_provider.pooled_create(
        None,
        config,
        {"model": "test-model", "messages": [{"role": "user", "content": "x"}], "max_tokens": 512},
        max_key_failovers=-1,
    )

    assert result is success
    assert pool.selected == ["key-0", "key-1", "key-2", "key-3", "key-4", "key-working"]
    assert len(set(pool.selected)) == len(pool.selected)
    assert [key for key, _reserved, actual in pool.reconciled if actual == 0] == [
        "key-0", "key-1", "key-2", "key-3", "key-4",
    ]


def test_atomic_checkout_never_overbooks_key_capacity(monkeypatch):
    monkeypatch.setattr(api_key_pool, "_parse_keys", lambda: ["key-a", "key-b"])
    pool = api_key_pool.APIKeyPool("test")
    pool._tpm_limit = 8000
    now = 1_800_000_010.0

    first = pool.checkout(5000, now=now)
    second = pool.checkout(5000, now=now)
    third = pool.checkout(5000, now=now)

    assert first is not None and second is not None
    assert first.key != second.key
    assert third is None
    assert pool.seconds_until_capacity(5000, now=now) > 0


def test_cerebras_pool_rotates_before_per_key_request_limit(monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY", "key-primary")
    monkeypatch.setenv("CEREBRAS_API_KEYS", "key-second,key-third")
    monkeypatch.setenv("CEREBRAS_RPM_LIMIT", "2")
    monkeypatch.setenv("CEREBRAS_RPH_LIMIT", "20")
    monkeypatch.setenv("CEREBRAS_RPD_LIMIT", "100")
    monkeypatch.setattr(api_key_pool, "OpenAI", lambda **kwargs: SimpleNamespace(**kwargs))
    pool = api_key_pool.APIKeyPool("cerebras")
    now = 1_800_000_010.0

    selected = [pool.checkout(100, now=now).key for _ in range(6)]

    assert selected == [
        "key-primary", "key-second", "key-third",
        "key-primary", "key-second", "key-third",
    ]
    assert pool.checkout(100, now=now) is None
    assert 0 < pool.seconds_until_capacity(100, now=now) <= 61


def test_cerebras_request_limit_uses_rolling_minute(monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY", "key-primary")
    monkeypatch.setenv("CEREBRAS_API_KEYS", "")
    monkeypatch.setenv("CEREBRAS_RPM_LIMIT", "2")
    monkeypatch.setenv("CEREBRAS_RPH_LIMIT", "150")
    monkeypatch.setenv("CEREBRAS_RPD_LIMIT", "2400")
    monkeypatch.setattr(api_key_pool, "OpenAI", lambda **kwargs: SimpleNamespace(**kwargs))
    pool = api_key_pool.APIKeyPool("cerebras")

    assert pool.checkout(100, now=59.0) is not None
    assert pool.checkout(100, now=59.5) is not None
    # Crossing a clock-minute boundary must not permit a rolling-window burst.
    assert pool.checkout(100, now=60.1) is None
    assert pool.seconds_until_capacity(100, now=60.1) > 59
    assert pool.checkout(100, now=119.3) is not None


def test_cerebras_429_immediately_fails_over_to_next_key(monkeypatch):
    success = SimpleNamespace(choices=[])
    pool = FakePool([
        FakeEntry("cerebras-a", FakeRateLimitError("429 requests per minute")),
        FakeEntry("cerebras-b", success),
    ])
    monkeypatch.setattr(openai, "RateLimitError", FakeRateLimitError)
    monkeypatch.setattr(api_key_pool, "provider_pool", lambda provider: pool)
    config = SimpleNamespace(provider="cerebras", model="gpt-oss-120b")

    result = llm_provider.pooled_create(
        None,
        config,
        {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "x"}], "max_tokens": 512},
        max_key_failovers=-1,
    )

    assert result is success
    assert pool.selected == ["cerebras-a", "cerebras-b"]
