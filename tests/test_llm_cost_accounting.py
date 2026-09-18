import json

import run_log


def test_qwen_uses_distinct_input_and_output_rates(monkeypatch):
    monkeypatch.setenv("USD_TO_INR_RATE", "95")
    priced = run_log.calculate_call_cost({
        "provider": "groq",
        "model": "qwen/qwen3.6-27b",
        "prompt_tokens": 1_000,
        "completion_tokens": 200,
    })
    assert priced["cost_usd"] == 0.0012
    assert priced["cost_inr"] == 0.114


def test_gpt_oss_cached_input_discount_does_not_double_charge_reasoning(monkeypatch):
    monkeypatch.setenv("USD_TO_INR_RATE", "100")
    priced = run_log.calculate_call_cost({
        "provider": "groq",
        "model": "openai/gpt-oss-20b",
        "prompt_tokens": 1_000,
        "cached_prompt_tokens": 400,
        "completion_tokens": 500,
        "reasoning_tokens": 300,
    })
    # (600×.075 + 400×.075×.5 + 500×.30) / 1M
    assert priced["cost_usd"] == 0.00021
    assert priced["cost_inr"] == 0.021


def test_whisper_turbo_is_billed_by_audio_hour_not_fake_tokens(monkeypatch):
    monkeypatch.setenv("USD_TO_INR_RATE", "100")
    priced = run_log.calculate_call_cost({
        "provider": "groq",
        "model": "whisper-large-v3-turbo",
        "audio_seconds": 60,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    })
    assert priced["rates"]["audio_per_hour"] == 0.04
    assert priced["cost_usd"] == 0.000666667
    assert priced["cost_inr"] == 0.066667


def test_unknown_provider_is_explicitly_unpriced():
    priced = run_log.calculate_call_cost({
        "provider": "nvidia",
        "model": "some-model",
        "prompt_tokens": 1_000,
        "completion_tokens": 1_000,
    })
    assert priced["cost_usd"] is None
    assert priced["pricing_status"] == "unavailable_for_provider_or_model"


def test_openrouter_uses_provider_reported_cost(monkeypatch):
    monkeypatch.setenv("USD_TO_INR_RATE", "80")
    priced = run_log.calculate_call_cost({
        "provider": "openrouter",
        "model": "openai/gpt-oss-120b",
        "provider_cost_usd": 0.0025,
    })

    assert priced["cost_usd"] == 0.0025
    assert priced["cost_inr"] == 0.2
    assert priced["pricing_status"] == "provider_reported_openrouter_cost"


def test_cerebras_director_uses_cerebras_list_price(monkeypatch):
    monkeypatch.setenv("USD_TO_INR_RATE", "80")
    priced = run_log.calculate_call_cost({
        "provider": "cerebras",
        "model": "gpt-oss-120b",
        "prompt_tokens": 1_000,
        "completion_tokens": 2_000,
    })

    assert priced["cost_usd"] == 0.00185
    assert priced["cost_inr"] == 0.148
    assert priced["pricing_status"] == "estimated_from_cerebras_list_price"


def test_summary_and_call_log_preserve_detailed_usage(monkeypatch, tmp_path):
    monkeypatch.setattr(run_log, "LOGS_DIR", tmp_path)
    monkeypatch.setenv("USD_TO_INR_RATE", "95")
    run_log.log_llm_call(
        job_id="abc123abc123",
        stage="director_plan",
        provider="groq",
        model="openai/gpt-oss-120b",
        key_prefix="redacted",
        prompt_tokens=2_000,
        completion_tokens=800,
        total_tokens=2_800,
        cached_prompt_tokens=500,
        reasoning_tokens=250,
        latency_ms=1234,
        queue_time_ms=10,
        prompt_time_ms=20,
        completion_time_ms=1000,
        finish_reason="stop",
        response_id="response-1",
    )
    summary = run_log.get_summary("abc123abc123")
    calls = run_log.get_calls("abc123abc123")
    assert summary["total_prompt_tokens"] == 2_000
    assert summary["total_completion_tokens"] == 800
    assert summary["total_cached_prompt_tokens"] == 500
    assert summary["total_reasoning_tokens"] == 250
    assert summary["pricing"]["status"] == "complete"
    assert calls[0]["finish_reason"] == "stop"
    assert calls[0]["response_id"] == "response-1"
    assert calls[0]["cost_inr"] > 0


def test_both_video_uis_show_server_calculated_inr_cost():
    root = run_log.BASE_DIR
    batch = (root / "templates" / "index.html").read_text(encoding="utf-8")
    stream = (root / "templates" / "stream_player.html").read_text(encoding="utf-8")
    for source in (batch, stream):
        assert "estimated_inr_cost" in source or "cost_inr" in source
        assert "reasoning_tokens" in source
        assert "cached_prompt_tokens" in source
