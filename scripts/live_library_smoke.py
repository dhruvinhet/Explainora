"""Verify the authenticated library endpoint against existing local outputs."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv
from supabase import create_client


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "frontend" / ".env.local")
service = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
publishable = os.environ["VITE_SUPABASE_PUBLISHABLE_KEY"]
jobs = ["bbf1169e9416", "f8d7a9c23cc4"]
suffix = uuid.uuid4().hex
email = f"simi-library-e2e-{suffix}@example.invalid"
password = f"Tmp-{suffix}-Aa1!"
created = service.auth.admin.create_user({"email": email, "password": password, "email_confirm": True})
user_id = str(created.user.id)
remote_assets: list[tuple[str, str]] = []
try:
    for job_id in jobs:
        result = service.rpc("create_video_job_with_quota", {
            "p_user_id": user_id, "p_job_id": job_id, "p_source_document_id": None,
            "p_prompt": "Library performance probe", "p_audience": "beginner",
            "p_style": "default", "p_requested_duration_seconds": 20,
            "p_mode": "batch", "p_quality_mode": "balanced", "p_request_payload": {"test": True},
        }).execute().data
        assert result.get("allowed") is True
    user = create_client(os.environ["SUPABASE_URL"], publishable)
    session = user.auth.sign_in_with_password({"email": email, "password": password}).session
    headers = {"Authorization": f"Bearer {session.access_token}"}
    from supabase_store import SupabaseStore
    direct_store = SupabaseStore()
    manifest = json.loads((ROOT / "outputs" / jobs[0] / "manifest.json").read_text(encoding="utf-8"))
    direct_store.complete_video(user_id, jobs[0], manifest)
    scene_count = len(service.table("video_scenes").select("id").eq("job_id", jobs[0]).execute().data)
    asset_rows = service.table("video_assets").select("storage_bucket,storage_path").eq("user_id", user_id).execute().data
    assert scene_count == 3
    remote_assets.extend((row["storage_bucket"], row["storage_path"]) for row in asset_rows if row["storage_bucket"] == "video-assets")
    probe_started = time.monotonic()
    probe = direct_store.authorized_job_ids(headers["Authorization"])
    print({"direct_store_seconds": round(time.monotonic() - probe_started, 3), "result_count": len(probe[0] or []), "error": probe[1]})
    started = time.monotonic()
    response = requests.get("http://127.0.0.1:5000/api/videos", headers=headers, timeout=20)
    elapsed = time.monotonic() - started
    response.raise_for_status()
    ids = {row["id"] for row in response.json()}
    assert set(jobs).issubset(ids)
    for job_id in jobs:
        detail = requests.get(f"http://127.0.0.1:5000/api/videos/{job_id}", headers=headers, timeout=20)
        detail.raise_for_status()
        assert detail.json()["id"] == job_id
    print({"status": "pass", "library_seconds": round(elapsed, 3), "owned_outputs": len(jobs), "persisted_scenes": scene_count})
finally:
    for bucket, path in remote_assets:
        try:
            service.storage.from_(bucket).remove([path])
        except Exception:
            pass
    service.auth.admin.delete_user(user_id)
    print({"cleanup": "disposable library user deleted"})
