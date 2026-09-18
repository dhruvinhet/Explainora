from datetime import datetime, timezone
from pathlib import Path

import app as app_module
from supabase_store import SupabaseStore


def test_liveness_is_public_and_correlated():
    response = app_module.app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert response.headers["X-Request-ID"]


def test_readiness_returns_503_when_dependency_is_not_ready(monkeypatch):
    monkeypatch.setattr(
        "runtime_health.check_readiness",
        lambda **_kwargs: {
            "status": "not_ready",
            "checks": {"database": {"ok": False, "detail": "unreachable"}},
        },
    )
    response = app_module.app.test_client().get("/readyz")
    assert response.status_code == 503
    assert response.get_json()["status"] == "not_ready"


def test_maintenance_mode_blocks_user_traffic_but_keeps_health(monkeypatch):
    monkeypatch.setattr(app_module, "MAINTENANCE_MODE", True)
    client = app_module.app.test_client()

    health = client.get("/healthz")
    readiness = client.get("/readyz")
    api = client.get("/api/videos", headers={"Accept": "application/json"})
    page = client.get("/")

    assert health.status_code == 200
    assert readiness.status_code == 503
    assert readiness.get_json()["status"] == "maintenance"
    assert api.status_code == 503
    assert api.get_json()["error"] == "maintenance"
    assert page.status_code == 503
    assert b"Sharpening" in page.data
    assert page.headers["Retry-After"] == "300"


def test_supabase_readiness_checks_database_and_private_storage(monkeypatch):
    store = SupabaseStore()
    store.enabled = True
    store.url = "https://example.supabase.co"
    store.secret_key = "server-key"
    requested = []

    class Response:
        status_code = 200

    def fake_get(url, **kwargs):
        requested.append((url, kwargs))
        return Response()

    monkeypatch.setattr("httpx.get", fake_get)
    result = store.readiness()
    assert result["database"]["ok"] is True
    assert result["storage"]["ok"] is True
    assert requested[0][0].endswith("/rest/v1/plan_catalog")
    assert requested[1][0].endswith("/storage/v1/bucket/video-assets")
    assert requested[0][1]["headers"]["Authorization"] == "Bearer server-key"


def test_worker_readiness_requires_a_fresh_queue_heartbeat():
    store = SupabaseStore()
    store.enabled = True

    class Response:
        data = [{
            "status": "idle",
            "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }]

    class Query:
        def select(self, columns):
            assert columns == "status,last_heartbeat_at"
            return self

        def order(self, column, desc=False):
            assert (column, desc) == ("last_heartbeat_at", True)
            return self

        def limit(self, count):
            assert count == 1
            return self

        @staticmethod
        def execute():
            return Response()

    class Client:
        @staticmethod
        def table(name):
            assert name == "render_workers"
            return Query()

    store.client = Client()
    assert store.worker_readiness()["ok"] is True


def test_oracle_worker_has_private_queue_network_and_outbound_egress():
    compose = Path("deploy/oracle/compose.yaml").read_text(encoding="utf-8")
    worker_block = compose.split("  worker:\n", 1)[1].split("  caddy:\n", 1)[0]
    assert "      - internal\n      - egress\n" in worker_block
    assert "  egress:\n" in compose
    assert "  internal:\n    internal: true\n" in compose


def test_oracle_environment_template_preserves_role_based_llm_routing():
    template = Path("deploy/oracle/production.env.example").read_text(
        encoding="utf-8"
    )
    assert "LLM_PROVIDER=groq" in template
    assert "DIRECTOR_PROVIDER=cerebras" in template
    assert "PLANNER_PROVIDER=groq" in template
    assert "CEREBRAS_DIRECTOR_MODEL=gpt-oss-120b" in template
    assert "GROQ_PLANNER_MODEL=openai/gpt-oss-120b" in template
    assert "GROQ_FAST_PLANNER_MODEL=openai/gpt-oss-20b" in template


def test_production_image_and_compose_pin_piper_voice():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    oracle_compose = Path("deploy/oracle/compose.yaml").read_text(encoding="utf-8")
    local_compose = Path("compose.production.yaml").read_text(encoding="utf-8")
    environment = Path("deploy/oracle/production.env.example").read_text(
        encoding="utf-8"
    )

    assert "piper-tts==1.7.0" in Path("requirements.lock").read_text(encoding="utf-8")
    assert "TTS_BACKEND=piper" in dockerfile
    assert "PIPER_MODEL=/app/voices/en_US-lessac-medium.onnx" in dockerfile
    assert "ADD --checksum=sha256:5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f" in dockerfile
    for compose in (oracle_compose, local_compose):
        assert "TTS_BACKEND: piper" in compose
        assert "PIPER_BIN: piper" in compose
        assert "PIPER_MODEL: /app/voices/en_US-lessac-medium.onnx" in compose
    assert "TTS_SPEED_WPM=145" in environment
    assert "CONTAINER_TTS_BACKEND" not in environment
