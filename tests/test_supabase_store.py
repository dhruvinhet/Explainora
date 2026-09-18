import httpx

from supabase_store import SupabaseStore


def test_safe_executes_callback_when_client_exists():
    store = SupabaseStore()
    store.client = object()

    assert store._safe("test operation", lambda: "persisted") == "persisted"


def test_safe_contains_persistence_failures():
    store = SupabaseStore()
    store.client = object()

    assert store._safe("test operation", lambda: 1 / 0, default="fallback") == "fallback"


def test_safe_skips_callback_without_client():
    store = SupabaseStore()
    store.enabled = False
    store.client = None
    called = False

    def callback():
        nonlocal called
        called = True

    assert store._safe("test operation", callback, default="offline") == "offline"
    assert called is False


def test_authenticate_validates_bearer_token_with_auth_server(monkeypatch):
    store = SupabaseStore()
    store.enabled = True
    store.url = "https://example.supabase.co"
    store.secret_key = "server-key"

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"id": "user-id"}

    def fake_get(url, *, headers, timeout):
        assert url == "https://example.supabase.co/auth/v1/user"
        assert headers == {"apikey": "server-key", "Authorization": "Bearer access-token"}
        assert timeout == 10.0
        return Response()

    monkeypatch.setattr(httpx, "get", fake_get)
    assert store.authenticate("Bearer access-token") == ("user-id", None)


def test_reauthenticate_password_verifies_the_same_user(monkeypatch):
    store = SupabaseStore()
    store.enabled = True
    store.url = "https://example.supabase.co"
    store.secret_key = "server-key"

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"user": {"id": "user-id"}, "access_token": "temporary-token"}

    def fake_post(url, **kwargs):
        if url.endswith("/logout"):
            assert kwargs["headers"]["Authorization"] == "Bearer temporary-token"
            return Response()
        assert url == "https://example.supabase.co/auth/v1/token"
        assert kwargs["params"] == {"grant_type": "password"}
        assert kwargs["headers"]["apikey"] == "server-key"
        assert kwargs["json"] == {"email": "user@example.com", "password": "secret"}
        assert kwargs["timeout"] == 10.0
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    assert store.reauthenticate_password("user-id", "user@example.com", "secret") == (True, None)


def test_storage_cleanup_recursively_lists_only_objects():
    store = SupabaseStore()
    store.enabled = True

    class Storage:
        def list(self, path, _options):
            return {
                "user-id": [{"name": "videos", "id": None, "metadata": None}, {"name": "avatar.png", "id": "1"}],
                "user-id/videos": [{"name": "final.mp4", "id": "2"}],
            }.get(path, [])

    class StorageRoot:
        @staticmethod
        def from_(_bucket):
            return Storage()

    class Client:
        storage = StorageRoot()

    store.client = Client()
    assert set(store._storage_paths_below("video-assets", "user-id")) == {
        "user-id/avatar.png", "user-id/videos/final.mp4",
    }


def test_delete_account_locks_generation_before_storage_and_auth_cleanup(monkeypatch):
    store = SupabaseStore()
    store.enabled = True
    calls = []

    class Result:
        data = True

    class Call:
        @staticmethod
        def execute():
            calls.append("locked")
            return Result()

    class Bucket:
        @staticmethod
        def remove(paths):
            calls.append(("remove", paths))

    class Storage:
        @staticmethod
        def from_(bucket):
            calls.append(("bucket", bucket))
            return Bucket()

    class Admin:
        @staticmethod
        def delete_user(user_id, should_soft_delete):
            calls.append(("delete_user", user_id, should_soft_delete))

    class Auth:
        admin = Admin()

    class Client:
        storage = Storage()
        auth = Auth()

        @staticmethod
        def rpc(name, params):
            assert name == "prepare_account_deletion"
            assert params == {"p_user_id": "user-id"}
            return Call()

    store.client = Client()
    monkeypatch.setattr(store, "account_job_ids", lambda _user_id: ["abcdef123456"])
    monkeypatch.setattr(store, "_storage_paths_below", lambda bucket, _prefix: [f"user-id/{bucket}.bin"])
    assert store.delete_account("user-id") == ["abcdef123456"]
    assert calls[0] == "locked"
    assert calls[-1] == ("delete_user", "user-id", False)


def test_authenticate_rejects_expired_session(monkeypatch):
    store = SupabaseStore()
    store.enabled = True
    store.url = "https://example.supabase.co"
    store.secret_key = "server-key"

    class Response:
        status_code = 401

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: Response())
    assert store.authenticate("Bearer expired") == (None, "Invalid or expired session")


def test_create_job_uses_atomic_quota_rpc():
    store = SupabaseStore()
    store.enabled = True

    class Response:
        data = {"allowed": True, "used": 5, "limit": 5, "remaining": 0}

    class Call:
        @staticmethod
        def execute():
            return Response()

    class Client:
        @staticmethod
        def rpc(name, params):
            assert name == "create_video_job_with_quota"
            assert params["p_user_id"] == "user-id"
            assert params["p_job_id"] == "abcdef123456"
            assert params["p_prompt"] == "Explain gravity"
            assert "source_text" not in params["p_request_payload"]
            return Call()

    store.client = Client()
    result = store.create_job("user-id", "abcdef123456", {
        "prompt": "Explain gravity",
        "duration_seconds": 60,
        "source_text": "private extracted document text",
    })
    assert result == {"allowed": True, "used": 5, "limit": 5, "remaining": 0}


def test_create_job_fails_closed_when_quota_rpc_is_unavailable():
    store = SupabaseStore()
    store.enabled = True

    class Client:
        @staticmethod
        def rpc(_name, _params):
            raise RuntimeError("RPC missing")

    store.client = Client()
    result = store.create_job("user-id", "abcdef123456", {
        "prompt": "Explain gravity",
        "duration_seconds": 60,
    })
    assert result["allowed"] is False
    assert "quota" in result["error"].lower()


def test_cancel_job_uses_owned_atomic_rpc():
    store = SupabaseStore()
    store.enabled = True

    class Response:
        data = {"outcome": "canceled", "status": "canceled"}

    class Call:
        @staticmethod
        def execute():
            return Response()

    class Client:
        @staticmethod
        def rpc(name, params):
            assert name == "cancel_video_job"
            assert params == {"p_user_id": "user-id", "p_job_id": "abcdef123456"}
            return Call()

    store.client = Client()
    assert store.cancel_job("user-id", "abcdef123456") == {
        "outcome": "canceled", "status": "canceled",
    }


def test_worker_can_read_authoritative_cancellation_state():
    store = SupabaseStore()
    store.enabled = True

    class Response:
        data = {"status": "canceled"}

    class Query:
        def select(self, columns):
            assert columns == "status"
            return self

        def eq(self, column, value):
            assert (column, value) == ("id", "abcdef123456")
            return self

        def maybe_single(self):
            return self

        @staticmethod
        def execute():
            return Response()

    class Client:
        @staticmethod
        def table(name):
            assert name == "video_jobs"
            return Query()

    store.client = Client()
    assert store.is_job_canceled("abcdef123456") is True


def test_owned_job_ids_fetches_user_jobs_in_one_query(monkeypatch):
    store = SupabaseStore()
    store.enabled = True
    store.url = "https://example.supabase.co"
    store.secret_key = "server-key"

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return [{"id": "aaaaaaaaaaaa"}, {"id": "bbbbbbbbbbbb"}]

    def fake_get(url, *, headers, params, timeout):
        assert url == "https://example.supabase.co/rest/v1/video_jobs"
        assert headers == {"apikey": "server-key", "Authorization": "Bearer server-key"}
        assert params == {"select": "id", "user_id": "eq.user-id"}
        assert timeout == 10.0
        return Response()

    monkeypatch.setattr(httpx, "get", fake_get)
    assert store.owned_job_ids("user-id") == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}


def test_owned_job_ids_allows_all_jobs_in_local_mode():
    store = SupabaseStore()
    store.enabled = False
    assert store.owned_job_ids(None) is None


def test_authorized_job_ids_uses_user_token_and_rls(monkeypatch):
    store = SupabaseStore()
    store.enabled = True
    store.url = "https://example.supabase.co"
    store.secret_key = "server-key"

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return [{"id": "aaaaaaaaaaaa"}]

    def fake_get(url, *, headers, params, timeout):
        assert url == "https://example.supabase.co/rest/v1/video_jobs"
        assert headers == {"apikey": "server-key", "Authorization": "Bearer user-token"}
        assert params == {"select": "id"}
        assert timeout == 10.0
        return Response()

    monkeypatch.setattr(httpx, "get", fake_get)
    assert store.authorized_job_ids("Bearer user-token") == ({"aaaaaaaaaaaa"}, None, 200)


def test_authorized_job_ids_rejects_missing_token():
    store = SupabaseStore()
    store.enabled = True
    assert store.authorized_job_ids("") == (set(), "Authentication required", 401)
