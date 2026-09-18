from pathlib import Path

import app as app_module


JOB_ID = "abcdef123456"
USER_ID = "11111111-1111-1111-1111-111111111111"


def _authorize(monkeypatch):
    monkeypatch.setattr(
        app_module.supabase_store, "authenticate", lambda _header: (USER_ID, None)
    )
    monkeypatch.setattr(
        app_module.supabase_store, "owns_job", lambda user_id, job_id: (
            user_id == USER_ID and job_id == JOB_ID
        )
    )


def test_cancel_active_job_marks_local_state_and_signals_worker(monkeypatch):
    _authorize(monkeypatch)
    monkeypatch.setattr(app_module.supabase_store, "enabled", True)
    monkeypatch.setattr(
        app_module.supabase_store,
        "cancel_job",
        lambda user_id, job_id: {"outcome": "canceled", "status": "canceled"},
    )
    app_module._clear_job_cancellation(JOB_ID)
    with app_module._jobs_lock:
        app_module._jobs[JOB_ID] = {"status": "running", "stage": "visuals"}

    response = app_module.app.test_client().delete(
        f"/api/jobs/{JOB_ID}", headers={"Authorization": "Bearer valid"}
    )

    assert response.status_code == 200
    assert response.get_json() == {"job_id": JOB_ID, "status": "canceled"}
    assert app_module._get_job(JOB_ID)["status"] == "canceled"
    try:
        app_module._raise_if_job_canceled(JOB_ID)
    except app_module.JobCanceled:
        pass
    else:
        raise AssertionError("active worker did not receive the cancellation signal")
    finally:
        app_module._clear_job_cancellation(JOB_ID)
        with app_module._jobs_lock:
            app_module._jobs.pop(JOB_ID, None)


def test_cancel_completed_job_returns_conflict(monkeypatch):
    _authorize(monkeypatch)
    monkeypatch.setattr(app_module.supabase_store, "enabled", True)
    monkeypatch.setattr(
        app_module.supabase_store,
        "cancel_job",
        lambda _user_id, _job_id: {"outcome": "not_cancelable", "status": "completed"},
    )

    response = app_module.app.test_client().delete(
        f"/api/jobs/{JOB_ID}", headers={"Authorization": "Bearer valid"}
    )

    assert response.status_code == 409
    assert response.get_json()["status"] == "completed"


def test_job_polling_prefers_durable_worker_state_over_stale_web_memory(monkeypatch):
    _authorize(monkeypatch)
    monkeypatch.setattr(app_module.supabase_store, "enabled", True)
    monkeypatch.setattr(
        app_module.supabase_store,
        "job_record",
        lambda _user_id, _job_id: {
            "status": "failed",
            "stage": "failed",
            "progress": 5,
            "detail": "AI provider is not configured",
            "error_message": "AI provider is not configured",
            "prompt": "Explain gravity",
            "audience": "beginner",
            "style": "default",
            "requested_duration_seconds": 120,
            "created_at": "2026-08-30T16:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        app_module.supabase_store, "scene_records", lambda _user_id, _job_id: []
    )
    with app_module._jobs_lock:
        app_module._jobs[JOB_ID] = {"status": "queued", "progress": 0}

    try:
        response = app_module.app.test_client().get(
            f"/api/jobs/{JOB_ID}", headers={"Authorization": "Bearer valid"}
        )
        body = response.get_json()
        assert response.status_code == 200
        assert body["status"] == "failed"
        assert body["error"] == "AI provider is not configured"
    finally:
        with app_module._jobs_lock:
            app_module._jobs.pop(JOB_ID, None)


def test_cancellation_migration_is_service_role_only_and_refund_compatible():
    migration = Path("supabase/migrations/20260904000000_job_cancellation.sql").read_text(
        encoding="utf-8"
    )
    assert "status = 'canceled'" in migration
    assert "status not in ('queued', 'running')" in migration
    assert "grant execute on function public.cancel_video_job(uuid, text) to service_role" in migration
    assert "from public, anon, authenticated" in migration
