"""Live Stage 1 verification using disposable Supabase users and objects."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from supabase import create_client


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "frontend" / ".env.local")
URL = os.environ["SUPABASE_URL"]
SECRET = os.environ["SUPABASE_SECRET_KEY"]
PUBLISHABLE = os.environ["VITE_SUPABASE_PUBLISHABLE_KEY"]
EXPECTED = {
    "free": {"limit": 5, "duration": 60, "concurrent": 1},
    "pro": {"limit": 10, "duration": 300, "concurrent": 2},
    "pro_ultra": {"limit": 20, "duration": 900, "concurrent": 3},
}


def service():
    return create_client(URL, SECRET)


def create_user(label: str) -> tuple[object, str, str, str]:
    admin = service()
    suffix = uuid.uuid4().hex
    email = f"simi-stage1-{label}-{suffix}@example.invalid"
    password = f"Tmp-{suffix}-Aa1!"
    created = admin.auth.admin.create_user({
        "email": email,
        "password": password,
        "email_confirm": True,
        "user_metadata": {"full_name": "Stage 1 Smoke"},
    })
    return admin, str(created.user.id), email, password


def assign_plan(admin, user_id: str, plan: str) -> None:
    if plan == "free":
        return
    admin.table("subscriptions").update({"status": "expired"}).eq(
        "user_id", user_id
    ).in_("status", ["trialing", "active", "past_due"]).execute()
    admin.table("subscriptions").insert({
        "user_id": user_id,
        "plan_id": plan,
        "status": "active",
        "provider": "stage1_fixture",
    }).execute()
    admin.table("profiles").update({
        "current_plan_id": plan,
        "videos_used": 0,
    }).eq("id", user_id).execute()


def reserve(user_id: str, duration: int, index: int) -> dict:
    job_id = uuid.uuid4().hex[:12]
    result = service().rpc("create_video_job_with_quota", {
        "p_user_id": user_id,
        "p_job_id": job_id,
        "p_source_document_id": None,
        "p_prompt": f"Stage 1 quota probe {index}",
        "p_audience": "beginner",
        "p_style": "default",
        "p_requested_duration_seconds": duration,
        "p_mode": "batch",
        "p_quality_mode": "balanced",
        "p_request_payload": {"stage1_smoke": True},
    }).execute().data
    result = dict(result or {})
    result["job_id"] = job_id
    return result


def test_plan(plan: str, limits: dict) -> dict:
    admin, user_id, _email, _password = create_user(plan)
    try:
        assign_plan(admin, user_id, plan)
        with ThreadPoolExecutor(max_workers=limits["concurrent"] + 2) as pool:
            first_wave = list(pool.map(
                lambda index: reserve(user_id, limits["duration"], index),
                range(limits["concurrent"] + 2),
            ))
        allowed = [row for row in first_wave if row.get("allowed")]
        denied = [row for row in first_wave if not row.get("allowed")]
        assert len(allowed) == limits["concurrent"], (plan, first_wave)
        assert all(row.get("reason") == "concurrency_limit" for row in denied)

        # One active job fails and must refund exactly one reserved slot. The
        # remainder complete, clearing concurrency while preserving usage.
        failed = allowed[0]["job_id"]
        admin.table("video_jobs").update({
            "status": "failed", "stage": "failed", "error_message": "fixture failure",
        }).eq("id", failed).execute()
        for row in allowed[1:]:
            admin.table("video_jobs").update({
                "status": "completed", "stage": "completed", "progress": 100,
            }).eq("id", row["job_id"]).execute()
        profile = admin.table("profiles").select("videos_used").eq(
            "id", user_id
        ).single().execute().data
        used = limits["concurrent"] - 1
        assert profile["videos_used"] == used

        too_long = reserve(user_id, limits["duration"] + 1, 9000)
        assert too_long.get("allowed") is False
        assert too_long.get("reason") == "duration_limit"

        while used < limits["limit"]:
            wave_size = min(limits["concurrent"], limits["limit"] - used)
            wave = [reserve(user_id, limits["duration"], used + index) for index in range(wave_size)]
            assert all(row.get("allowed") for row in wave), (plan, wave)
            for row in wave:
                admin.table("video_jobs").update({
                    "status": "completed", "stage": "completed", "progress": 100,
                }).eq("id", row["job_id"]).execute()
            used += wave_size

        exhausted = reserve(user_id, limits["duration"], 9999)
        assert exhausted.get("allowed") is False
        assert exhausted.get("reason") == "quota_limit"
        final_profile = admin.table("profiles").select("videos_used").eq(
            "id", user_id
        ).single().execute().data
        assert final_profile["videos_used"] == limits["limit"]
        return {
            "plan": plan,
            "concurrency": f"{len(allowed)} accepted / {len(denied)} denied",
            "duration_limit_seconds": limits["duration"],
            "quota_limit": limits["limit"],
            "failure_refund": True,
        }
    finally:
        admin.auth.admin.delete_user(user_id)


def test_queue() -> dict:
    admin, user_id, _email, _password = create_user("queue")
    try:
        accepted = reserve(user_id, 20, 1)
        assert accepted.get("allowed") is True
        worker_id = f"stage1-smoke-{uuid.uuid4().hex[:8]}"
        claimed = admin.rpc("claim_video_job", {"p_worker_id": worker_id}).execute().data
        assert claimed["id"] == accepted["job_id"]
        assert claimed["status"] == "running" and claimed["locked_by"] == worker_id
        heartbeat = admin.rpc("heartbeat_video_job", {
            "p_job_id": claimed["id"], "p_worker_id": worker_id,
        }).execute().data
        assert heartbeat is True
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        admin.table("video_jobs").update({
            "attempt_count": 3,
            "heartbeat_at": old,
            "locked_at": old,
        }).eq("id", claimed["id"]).execute()
        recovered = admin.rpc("recover_stale_video_jobs", {
            "p_stale_after_seconds": 60,
        }).execute().data
        row = admin.table("video_jobs").select("status,error_code").eq(
            "id", claimed["id"]
        ).single().execute().data
        profile = admin.table("profiles").select("videos_used").eq(
            "id", user_id
        ).single().execute().data
        assert int(recovered) >= 1
        assert row == {"status": "failed", "error_code": "worker_timeout"}
        assert profile["videos_used"] == 0
        return {"claim": True, "heartbeat": True, "stale_recovery": True, "quota_refund": True}
    finally:
        admin.auth.admin.delete_user(user_id)


def test_storage_and_demo_removal() -> dict:
    admin, user_id, email, password = create_user("storage")
    remote_objects: list[tuple[str, str]] = []
    old_output = os.environ.get("OUTPUT_DIR")
    rate_key: str | None = None
    try:
        user = create_client(URL, PUBLISHABLE)
        user.auth.sign_in_with_password({"email": email, "password": password})
        demo_removed = False
        try:
            user.rpc("complete_demo_checkout", {
                "target_plan": "pro", "amount_cents": 0,
            }).execute()
        except Exception:
            demo_removed = True
        assert demo_removed

        rate_key = f"stage1:{uuid.uuid4().hex}"
        rate = [admin.rpc("consume_rate_limit", {
            "p_bucket_key": rate_key, "p_limit": 2, "p_window_seconds": 60,
        }).execute().data for _ in range(3)]
        assert [row["allowed"] for row in rate] == [True, True, False]

        with tempfile.TemporaryDirectory(prefix="simi-stage1-storage-") as tmp:
            os.environ["OUTPUT_DIR"] = tmp
            from supabase_store import SupabaseStore

            store = SupabaseStore()
            job_id = uuid.uuid4().hex[:12]
            output = Path(tmp) / job_id
            output.mkdir(parents=True)
            (output / "final.mp4").write_bytes(b"stage1-private-video-probe" * 1024)
            manifest = {
                "job_id": job_id,
                "request": {"prompt": "Storage probe", "target_duration_seconds": 20,
                            "audience": "beginner", "style": "default"},
                "final_duration_seconds": 20,
                "plan": {"title": "Storage probe", "scenes": [], "must_cover": []},
            }
            (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            reservation = admin.rpc("create_video_job_with_quota", {
                "p_user_id": user_id, "p_job_id": job_id,
                "p_source_document_id": None, "p_prompt": "Storage probe",
                "p_audience": "beginner", "p_style": "default",
                "p_requested_duration_seconds": 20, "p_mode": "batch",
                "p_quality_mode": "balanced", "p_request_payload": {},
            }).execute().data
            assert reservation["allowed"] is True
            stored = store.complete_video(user_id, job_id, manifest)
            assert stored["video_storage_path"] and stored["manifest_storage_path"]
            remote_objects.extend([
                ("video-assets", stored["video_storage_path"]),
                ("video-assets", stored["manifest_storage_path"]),
            ])
            record = admin.table("videos").select(
                "video_storage_path,manifest_storage_path,video_url"
            ).eq("id", job_id).single().execute().data
            assert record["video_url"] is None
            signed = store.signed_storage_url("video-assets", record["video_storage_path"], 60)
            assert signed and requests.get(signed, timeout=20).content.startswith(b"stage1-private")

            source = Path(tmp) / "notes.txt"
            source.write_text("durable private source", encoding="utf-8")
            document_id = store.create_source_document(
                user_id, "notes.txt", str(source), "text/plain", source.stat().st_size, 22
            )
            assert document_id
            document = admin.table("source_documents").select("storage_path").eq(
                "id", document_id
            ).single().execute().data
            remote_objects.append(("source-documents", document["storage_path"]))
            restored = store.source_document_blob(document_id)
            assert restored and restored[1] == b"durable private source"

        return {
            "demo_checkout_removed": True,
            "rate_limit": "2 accepted / 1 denied",
            "private_video_upload": True,
            "signed_video_download": True,
            "source_round_trip": True,
        }
    finally:
        if old_output is None:
            os.environ.pop("OUTPUT_DIR", None)
        else:
            os.environ["OUTPUT_DIR"] = old_output
        for bucket, path in remote_objects:
            try:
                admin.storage.from_(bucket).remove([path])
            except Exception:
                pass
        if rate_key:
            try:
                admin.table("api_rate_limits").delete().eq("bucket_key", rate_key).execute()
            except Exception:
                pass
        admin.auth.admin.delete_user(user_id)


def test_api_boundary() -> dict:
    admin, owner_id, owner_email, owner_password = create_user("api-owner")
    _other_admin, other_id, other_email, other_password = create_user("api-other")
    try:
        owner_client = create_client(URL, PUBLISHABLE)
        owner_session = owner_client.auth.sign_in_with_password({
            "email": owner_email, "password": owner_password,
        }).session
        other_client = create_client(URL, PUBLISHABLE)
        other_session = other_client.auth.sign_in_with_password({
            "email": other_email, "password": other_password,
        }).session
        assert owner_session and other_session
        owner_headers = {"Authorization": f"Bearer {owner_session.access_token}"}
        other_headers = {"Authorization": f"Bearer {other_session.access_token}"}

        import app as app_module

        previous_mode = app_module.JOB_EXECUTION_MODE
        app_module.JOB_EXECUTION_MODE = "queue"
        try:
            client = app_module.app.test_client()
            assert client.get("/api/videos").status_code == 401
            assert client.get("/api/keys").status_code == 401
            assert client.get("/api/logs/abcdef123456").status_code == 401
            assert client.get("/outputs/abcdef123456/final.mp4").status_code == 401

            accepted = client.post("/api/videos", headers=owner_headers, json={
                "prompt": "Stage 1 API queue probe",
                "duration_seconds": 20,
                "audience": "beginner",
                "style": "default",
                "mode": "batch",
            })
            assert accepted.status_code == 202, accepted.get_json()
            accepted_data = accepted.get_json()
            assert accepted_data["queued"] is True
            job_id = accepted_data["job_id"]
            assert client.get(f"/api/jobs/{job_id}", headers=owner_headers).status_code == 200
            assert client.get(f"/api/jobs/{job_id}", headers=other_headers).status_code == 404
            assert client.get(f"/outputs/{job_id}/final.mp4", headers=other_headers).status_code == 404

            billing = client.post(
                "/api/billing/checkout", headers=owner_headers, json={"plan": "pro"}
            )
            assert billing.status_code == 503
            profile = admin.table("profiles").select("current_plan_id").eq(
                "id", owner_id
            ).single().execute().data
            assert profile["current_plan_id"] == "free"
            return {
                "unauthenticated_private_routes": "401",
                "cross_user_job_access": "404",
                "queue_submission": "202",
                "browser_plan_escalation": "blocked",
            }
        finally:
            app_module.JOB_EXECUTION_MODE = previous_mode
    finally:
        admin.auth.admin.delete_user(owner_id)
        admin.auth.admin.delete_user(other_id)


def main() -> None:
    admin = service()
    # Recover only leftovers created by an interrupted run of this script.
    stale_rate_rows = admin.table("api_rate_limits").select("bucket_key").like(
        "bucket_key", "stage1:%"
    ).execute().data
    for row in stale_rate_rows:
        admin.table("api_rate_limits").delete().eq("bucket_key", row["bucket_key"]).execute()
    queued = admin.table("video_jobs").select("id", count="exact").eq(
        "status", "queued"
    ).execute().count
    if queued:
        raise RuntimeError("Refusing queue smoke test while real jobs are queued")
    plans = admin.table("plan_catalog").select(
        "id,video_limit,max_duration_seconds,max_concurrent_jobs"
    ).execute().data
    actual = {row["id"]: {
        "limit": row["video_limit"],
        "duration": row["max_duration_seconds"],
        "concurrent": row["max_concurrent_jobs"],
    } for row in plans}
    assert actual == EXPECTED
    result = {
        "migration": "present",
        "plans": [test_plan(plan, limits) for plan, limits in EXPECTED.items()],
        "queue": test_queue(),
        "storage_and_security": test_storage_and_demo_removal(),
        "api_boundary": test_api_boundary(),
        "cleanup": "all disposable users, rows, rate buckets, and storage objects removed",
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
