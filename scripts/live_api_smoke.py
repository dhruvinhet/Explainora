"""End-to-end API and render smoke test against running local servers."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv
from supabase import create_client


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "frontend" / ".env.local")
BASE = "http://127.0.0.1:5173"
URL = os.environ["SUPABASE_URL"]
SECRET = os.environ["SUPABASE_SECRET_KEY"]
PUBLISHABLE = os.environ["VITE_SUPABASE_PUBLISHABLE_KEY"]


def assert_status(response: requests.Response, expected: int, label: str) -> None:
    assert response.status_code == expected, (label, response.status_code, response.text[:500])


def wait_for_job(headers: dict[str, str], job_id: str, timeout: int = 900) -> dict:
    started = time.monotonic()
    last_stage = None
    while time.monotonic() - started < timeout:
        response = requests.get(f"{BASE}/api/jobs/{job_id}", headers=headers, timeout=30)
        assert_status(response, 200, "job status")
        job = response.json()
        stage = (job.get("stage"), job.get("progress"), job.get("detail"))
        if stage != last_stage:
            print(json.dumps({"job": job_id, "stage": stage[0], "progress": stage[1], "detail": stage[2]}), flush=True)
            last_stage = stage
        if job.get("status") in {"completed", "failed"}:
            return job
        time.sleep(2)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout}s")


def verify_media(job_id: str, output_url: str) -> dict:
    response = requests.get(f"{BASE}{output_url}", timeout=120)
    assert_status(response, 200, "video output")
    assert response.headers.get("content-type", "").startswith("video/mp4")
    assert len(response.content) > 10_000
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as target:
        target.write(response.content)
        path = Path(target.name)
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=30,
        )
        data = json.loads(probe.stdout)
        stream_types = {stream.get("codec_type") for stream in data.get("streams", [])}
        assert {"video", "audio"}.issubset(stream_types)
        video = next(stream for stream in data["streams"] if stream.get("codec_type") == "video")
        assert video.get("width") == 1280 and video.get("height") == 720
        return {"bytes": len(response.content), "duration": round(float(data["format"]["duration"]), 2), "streams": sorted(stream_types)}
    finally:
        path.unlink(missing_ok=True)


def main() -> None:
    service = create_client(URL, SECRET)
    suffix = uuid.uuid4().hex
    email = f"simi-api-e2e-{suffix}@example.invalid"
    password = f"Tmp-{suffix}-Aa1!"
    created = service.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True,
         "user_metadata": {"full_name": "Simi API E2E"}}
    )
    user_id = str(created.user.id)
    remote_assets: list[tuple[str, str]] = []
    try:
        user_client = create_client(URL, PUBLISHABLE)
        session = user_client.auth.sign_in_with_password({"email": email, "password": password}).session
        assert session and session.access_token
        headers = {"Authorization": f"Bearer {session.access_token}"}

        checks = {}
        for label, method, path, kwargs, expected in [
            ("list without auth", "get", "/api/videos", {}, 401),
            ("list invalid token", "get", "/api/videos", {"headers": {"Authorization": "Bearer invalid"}}, 401),
            ("empty prompt", "post", "/api/videos", {"headers": headers, "json": {"prompt": ""}}, 400),
            ("duration text", "post", "/api/videos", {"headers": headers, "json": {"prompt": "x", "duration_seconds": "never"}}, 400),
            ("duration too short", "post", "/api/videos", {"headers": headers, "json": {"prompt": "x", "duration_seconds": 19}}, 400),
            ("duration too long", "post", "/api/videos", {"headers": headers, "json": {"prompt": "x", "duration_seconds": 1801}}, 400),
            ("unknown video", "get", "/api/videos/000000000000", {"headers": headers}, 404),
        ]:
            response = requests.request(method, f"{BASE}{path}", timeout=30, **kwargs)
            assert_status(response, expected, label)
            checks[label] = expected

        bad_file = {"document": ("notes.exe", b"not executable", "application/octet-stream")}
        response = requests.post(f"{BASE}/api/videos", headers=headers, data={"prompt": "test"}, files=bad_file, timeout=30)
        assert_status(response, 400, "unsupported document")
        checks["unsupported document"] = 400

        jobs = []
        cases = [
            {
                "name": "batch-json",
                "request": {"json": {"prompt": "Why is the daytime sky blue?", "duration_seconds": 20,
                                      "audience": "beginner", "style": "clean-diagrams", "quality_mode": "fast",
                                      "captions": True, "mode": "batch"}},
                "expected_mode": "batch",
            },
            {
                "name": "streaming-document",
                "request": {"data": {"prompt": "Explain binary search using the attached notes", "duration_seconds": "20",
                                      "audience": "software developers", "style": "blueprint", "quality_mode": "balanced",
                                      "captions": "false", "mode": "streaming"},
                            "files": {"document": ("e2e_notes.txt", (ROOT / "tests" / "fixtures" / "e2e_notes.txt").read_bytes(), "text/plain")}},
                "expected_mode": "streaming",
            },
        ]
        for case in cases:
            response = requests.post(f"{BASE}/api/videos", headers=headers, timeout=120, **case["request"])
            assert_status(response, 202, case["name"])
            accepted = response.json()
            assert accepted["mode"] == case["expected_mode"]
            job_id = accepted["job_id"]
            job = wait_for_job(headers, job_id)
            assert job["status"] == "completed", (case["name"], job.get("detail"), job.get("error"))
            media = verify_media(job_id, job["output_url"])
            detail = requests.get(f"{BASE}/api/videos/{job_id}", headers=headers, timeout=30)
            assert_status(detail, 200, "video detail")
            detail_data = detail.json()
            assert detail_data["id"] == job_id and detail_data["scenes"]
            jobs.append({"name": case["name"], "id": job_id, "mode": accepted["mode"], "media": media,
                         "scenes": len(detail_data["scenes"]), "title": detail_data["title"]})

        library = requests.get(f"{BASE}/api/videos", headers=headers, timeout=30)
        assert_status(library, 200, "video library")
        library_ids = {item["id"] for item in library.json()}
        assert {job["id"] for job in jobs}.issubset(library_ids)

        profile = service.table("profiles").select("videos_used,current_plan_id").eq("id", user_id).single().execute().data
        db = {
            "profile": profile,
            "jobs": len(service.table("video_jobs").select("id").eq("user_id", user_id).execute().data),
            "videos": len(service.table("videos").select("id").eq("user_id", user_id).execute().data),
            "scenes": len(service.table("video_scenes").select("id").eq("user_id", user_id).execute().data),
            "assets": len(service.table("video_assets").select("storage_bucket,storage_path").eq("user_id", user_id).execute().data),
            "documents": len(service.table("source_documents").select("storage_path").eq("user_id", user_id).execute().data),
            "events": len(service.table("generation_events").select("id").eq("user_id", user_id).execute().data),
        }
        assert profile == {"videos_used": 2, "current_plan_id": "free"}
        assert db["jobs"] == db["videos"] == 2 and db["scenes"] > 0 and db["assets"] >= 6 and db["documents"] == 1

        for row in service.table("video_assets").select("storage_bucket,storage_path").eq("user_id", user_id).execute().data:
            if row["storage_bucket"] in {"video-assets", "source-documents"}:
                remote_assets.append((row["storage_bucket"], row["storage_path"]))
        for row in service.table("source_documents").select("storage_path").eq("user_id", user_id).execute().data:
            if row.get("storage_path") and not str(row["storage_path"]).startswith(str(ROOT)):
                remote_assets.append(("source-documents", row["storage_path"]))

        print(json.dumps({"validation": checks, "renders": jobs, "database": db}, indent=2), flush=True)
    finally:
        for bucket, path in remote_assets:
            try:
                service.storage.from_(bucket).remove([path])
            except Exception:
                pass
        service.auth.admin.delete_user(user_id)
        print(json.dumps({"cleanup": "disposable auth user and database rows deleted", "storage_objects_attempted": len(remote_assets)}), flush=True)


if __name__ == "__main__":
    main()
