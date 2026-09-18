import llm_provider
from llm_provider import LLMConfig, get_llm_config
from job_runtime import JobRuntimeConfig
from streaming_llm import _build_kwargs


def test_cerebras_can_configure_both_llm_roles(monkeypatch):
    monkeypatch.delenv("DIRECTOR_PROVIDER", raising=False)
    monkeypatch.delenv("PLANNER_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "cerebras")
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-key")
    monkeypatch.setenv("CEREBRAS_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("CEREBRAS_DIRECTOR_MODEL", "director-model")
    monkeypatch.setenv("CEREBRAS_PLANNER_MODEL", "planner-model")

    director = get_llm_config("director")
    planner = get_llm_config("planner")

    assert director.provider == planner.provider == "cerebras"
    assert director.model == "director-model"
    assert planner.model == "planner-model"
    assert planner.api_key == "test-key"
    assert planner.base_url == "https://example.test/v1"


def test_provider_is_inferred_from_the_first_configured_key(monkeypatch):
    for name in (
        "DIRECTOR_PROVIDER", "PLANNER_PROVIDER", "LLM_PROVIDER",
        "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY",
        "NVIDIA_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CEREBRAS_API_KEY", "configured-cerebras-key")

    assert get_llm_config("director").provider == "cerebras"
    assert get_llm_config("planner").provider == "cerebras"
    readiness = llm_provider.llm_configuration_readiness()
    assert readiness["ok"] is True
    assert "director:cerebras/gpt-oss-120b" in readiness["detail"]


def test_missing_provider_key_fails_readiness_without_exposing_a_secret(monkeypatch):
    for name in (
        "DIRECTOR_PROVIDER", "PLANNER_PROVIDER", "LLM_PROVIDER",
        "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY",
        "NVIDIA_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    result = llm_provider.llm_configuration_readiness()
    assert result == {"ok": False, "detail": "missing API key for groq"}


def test_cerebras_can_be_selected_for_only_the_planner(monkeypatch):
    monkeypatch.delenv("DIRECTOR_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("PLANNER_PROVIDER", "cerebras")
    monkeypatch.setenv("CEREBRAS_PLANNER_MODEL", "gpt-oss-120b")

    assert get_llm_config("director").provider == "groq"
    planner = get_llm_config("planner")
    assert planner.provider == "cerebras"
    assert planner.model == "gpt-oss-120b"


def test_fast_mode_prefers_provider_specific_planner_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("PLANNER_PROVIDER", "cerebras")
    monkeypatch.setenv("FAST_PLANNER_MODEL", "groq-only-model")
    monkeypatch.setenv("CEREBRAS_FAST_PLANNER_MODEL", "cerebras-fast-model")

    runtime = JobRuntimeConfig.from_payload(
        "job-1",
        {"quality_mode": "fast"},
        default_planner_model="cerebras-normal-model",
    )

    assert runtime.planner_model == "cerebras-fast-model"


def test_cerebras_streaming_kwargs_use_json_mode_and_gpt_oss_reasoning():
    config = get_llm_config("planner")
    config = type(config)(
        provider="cerebras",
        model="gpt-oss-120b",
        api_key="test-key",
        base_url="https://api.cerebras.ai/v1",
        default_headers={},
    )

    kwargs = _build_kwargs(
        config,
        [{"role": "user", "content": "Return JSON"}],
        max_tokens=1000,
        stream=True,
    )

    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["extra_body"] == {"reasoning_effort": "low"}
    assert kwargs["model"] == "gpt-oss-120b"


def test_pooled_create_fails_over_transient_connection_error(monkeypatch):
    class Entry:
        def __init__(self, key, client):
            self.key = key
            self.client = client

    class Completions:
        def __init__(self, should_fail):
            self.should_fail = should_fail
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.should_fail:
                raise RuntimeError("Connection error.")
            return type("Response", (), {"choices": [object()]})()

    first = Completions(should_fail=True)
    second = Completions(should_fail=False)
    entries = [
        Entry("first-key", type("Client", (), {"chat": type("Chat", (), {"completions": first})()})()),
        Entry("second-key", type("Client", (), {"chat": type("Chat", (), {"completions": second})()})()),
    ]

    class Pool:
        size = 2

        def entries(self):
            return entries

        def checkout(self, estimated, exclude_keys=None):
            excluded = exclude_keys or set()
            return next((entry for entry in entries if entry.key not in excluded), None)

    monkeypatch.setattr("api_key_pool.provider_pool", lambda provider: Pool())
    response = llm_provider.pooled_create(
        None,
        LLMConfig("cerebras", "gpt-oss-120b", "key", "https://example.test/v1", {}),
        {"model": "gpt-oss-120b", "messages": [], "max_tokens": 512},
    )

    assert response.choices
    assert first.calls == 1
    assert second.calls == 1
