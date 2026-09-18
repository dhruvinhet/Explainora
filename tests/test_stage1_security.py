from urllib.parse import urlsplit

import app as app_module
from supabase_store import SupabaseStore


JOB_ID = "abcdef123456"


def test_private_job_asset_and_log_routes_require_authentication(monkeypatch):
    monkeypatch.setattr(
        app_module.supabase_store,
        "authenticate",
        lambda _authorization: (None, "Authentication required"),
    )
    client = app_module.app.test_client()
    paths = [
        f"/api/logs/{JOB_ID}",
        f"/api/logs/{JOB_ID}/calls",
        f"/api/logs/{JOB_ID}/download",
        f"/media/jobs/{JOB_ID}/scenes/0.png",
        f"/outputs/{JOB_ID}/final.mp4",
        f"/segments/{JOB_ID}/stream.mp4",
        f"/stream/{JOB_ID}",
    ]
    for path in paths:
        response = client.get(path)
        expected = 404 if path.startswith("/stream/") and not app_module.ENABLE_STREAMING else 401
        assert response.status_code == expected, path


def test_private_job_routes_hide_resources_owned_by_someone_else(monkeypatch):
    monkeypatch.setattr(
        app_module.supabase_store,
        "authenticate",
        lambda _authorization: ("user-a", None),
    )
    monkeypatch.setattr(app_module.supabase_store, "owns_job", lambda _user, _job: False)
    response = app_module.app.test_client().get(
        f"/outputs/{JOB_ID}/final.mp4",
        headers={"Authorization": "Bearer valid"},
    )
    assert response.status_code == 404


def test_expiring_media_signature_is_accepted_without_bearer_token(monkeypatch):
    monkeypatch.setattr(
        app_module.supabase_store,
        "authenticate",
        lambda _authorization: (None, "Authentication required"),
    )
    signed = app_module._signed_media_url(f"/outputs/{JOB_ID}/missing.mp4")
    response = app_module.app.test_client().get(urlsplit(signed).path + "?" + urlsplit(signed).query)
    # Authorization passed; only the deliberately absent test file is missing.
    assert response.status_code == 404
    assert response.get_json()["error"] == "job output not found"


def test_legacy_generate_is_disabled_by_default():
    response = app_module.app.test_client().post("/generate", json={"text": "hello"})
    assert response.status_code == 404


def test_billing_endpoint_never_changes_a_plan_without_provider_confirmation(monkeypatch):
    monkeypatch.setattr(
        app_module.supabase_store,
        "authenticate",
        lambda _authorization: ("user-a", None),
    )
    response = app_module.app.test_client().post(
        "/api/billing/checkout",
        headers={"Authorization": "Bearer valid"},
        json={"plan": "pro"},
    )
    assert response.status_code == 503
    assert response.get_json()["code"] == "billing_not_configured"


def test_source_document_refuses_local_only_fallback(monkeypatch, tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("private notes", encoding="utf-8")
    store = SupabaseStore()
    store.enabled = True
    store.client = object()
    monkeypatch.setattr(store, "_upload_file", lambda *_args, **_kwargs: None)
    assert store.create_source_document(
        "user-a", "notes.txt", str(source), "text/plain", source.stat().st_size, 13
    ) is None


def test_production_queue_migration_revokes_demo_checkout():
    migration = (
        app_module.BASE_DIR
        / "supabase"
        / "migrations"
        / "20260830000000_stage1_security_architecture.sql"
    ).read_text(encoding="utf-8")
    assert "drop function public.complete_demo_checkout" in migration.lower()
    assert "for update skip locked" in migration.lower()
    assert "max_concurrent_jobs" in migration
    assert "max_duration_seconds" in migration


def test_plan_duration_migration_matches_composer_tiers():
    migration = (
        app_module.BASE_DIR
        / "supabase"
        / "migrations"
        / "20260901000000_align_plan_duration_tiers.sql"
    ).read_text(encoding="utf-8").lower()
    assert "when 'free' then 120" in migration
    assert "when 'pro' then 240" in migration
    assert "when 'pro_ultra' then 360" in migration
