import app as app_module


AUTH_USER = {"id": "11111111-1111-1111-1111-111111111111", "email": "user@example.com"}


def _disable_audit(monkeypatch):
    monkeypatch.setattr(app_module.supabase_store, "record_security_event", lambda *_args, **_kwargs: None)


def test_password_change_requires_current_password_reauthentication(monkeypatch):
    _disable_audit(monkeypatch)
    monkeypatch.setattr(app_module.supabase_store, "authenticated_user", lambda _auth: (AUTH_USER, None))
    monkeypatch.setattr(
        app_module.supabase_store,
        "reauthenticate_password",
        lambda *_args: (False, "Current password is incorrect"),
    )
    response = app_module.app.test_client().post(
        "/api/account/password",
        headers={"Authorization": "Bearer valid"},
        json={
            "current_password": "wrong-password",
            "new_password": "new-password-123",
            "new_password_confirmation": "new-password-123",
        },
    )
    assert response.status_code == 401
    assert response.get_json()["error"] == "Current password is incorrect"


def test_password_change_updates_only_after_successful_reauthentication(monkeypatch):
    events = []
    updates = []
    monkeypatch.setattr(app_module.supabase_store, "authenticated_user", lambda _auth: (AUTH_USER, None))
    monkeypatch.setattr(app_module.supabase_store, "reauthenticate_password", lambda *_args: (True, None))
    monkeypatch.setattr(app_module.supabase_store, "update_account_password", lambda *args: updates.append(args))
    monkeypatch.setattr(app_module.supabase_store, "record_security_event", lambda *args, **_kwargs: events.append(args))
    response = app_module.app.test_client().post(
        "/api/account/password",
        headers={"Authorization": "Bearer valid"},
        json={
            "current_password": "old-password",
            "new_password": "new-password-123",
            "new_password_confirmation": "new-password-123",
        },
    )
    assert response.status_code == 200
    assert updates == [(AUTH_USER["id"], "new-password-123")]
    assert events[-1][1:3] == ("password_change", "succeeded")


def test_account_deletion_requires_exact_confirmation(monkeypatch):
    _disable_audit(monkeypatch)
    monkeypatch.setattr(app_module.supabase_store, "authenticated_user", lambda _auth: (AUTH_USER, None))
    response = app_module.app.test_client().delete(
        "/api/account",
        headers={"Authorization": "Bearer valid"},
        json={"current_password": "password", "confirmation": "delete"},
    )
    assert response.status_code == 400


def test_account_deletion_blocks_while_a_render_is_active(monkeypatch):
    _disable_audit(monkeypatch)
    monkeypatch.setattr(app_module.supabase_store, "authenticated_user", lambda _auth: (AUTH_USER, None))
    monkeypatch.setattr(app_module.supabase_store, "reauthenticate_password", lambda *_args: (True, None))
    monkeypatch.setattr(
        app_module.supabase_store,
        "delete_account",
        lambda _user_id: (_ for _ in ()).throw(RuntimeError("active_jobs")),
    )
    response = app_module.app.test_client().delete(
        "/api/account",
        headers={"Authorization": "Bearer valid"},
        json={"current_password": "password", "confirmation": "DELETE MY ACCOUNT"},
    )
    assert response.status_code == 409
    assert "active" in response.get_json()["error"].lower()


def test_account_deletion_cleans_local_job_state_after_remote_delete(monkeypatch, tmp_path):
    _disable_audit(monkeypatch)
    job_id = "abcdef123456"
    work_root = tmp_path / "work"
    output_root = tmp_path / "outputs"
    (work_root / job_id).mkdir(parents=True)
    (output_root / job_id).mkdir(parents=True)
    monkeypatch.setattr(app_module, "WORK_DIR", work_root)
    monkeypatch.setattr(app_module, "OUTPUT_DIR", output_root)
    monkeypatch.setattr(app_module.supabase_store, "authenticated_user", lambda _auth: (AUTH_USER, None))
    monkeypatch.setattr(app_module.supabase_store, "reauthenticate_password", lambda *_args: (True, None))
    monkeypatch.setattr(app_module.supabase_store, "delete_account", lambda _user_id: [job_id])
    app_module._jobs[job_id] = {"status": "done"}

    response = app_module.app.test_client().delete(
        "/api/account",
        headers={"Authorization": "Bearer valid"},
        json={"current_password": "password", "confirmation": "DELETE MY ACCOUNT"},
    )
    assert response.status_code == 200
    assert not (work_root / job_id).exists()
    assert not (output_root / job_id).exists()
    assert job_id not in app_module._jobs


def test_account_lifecycle_migration_is_service_role_only():
    migration = (
        app_module.BASE_DIR / "supabase" / "migrations" / "20260902000000_account_lifecycle.sql"
    ).read_text(encoding="utf-8").lower()
    assert "account_security_audit" in migration
    assert "enable row level security" in migration
    assert "revoke all" in migration
    assert "to service_role" in migration
    assert "prepare_account_deletion" in migration
    assert "account_deletion_pending" in migration
