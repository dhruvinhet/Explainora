"""Optional Supabase persistence for Explainora.

The app remains runnable without credentials. Once SUPABASE_URL and
SUPABASE_SECRET_KEY are present, authenticated requests are required and all
job/video state is mirrored into Postgres. Failures are logged and never leak
the server secret into responses.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

try:
    from supabase import Client, create_client
except ImportError:  # Allows the existing local-only app to start before install.
    Client = Any  # type: ignore[misc,assignment]
    create_client = None

LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SupabaseStore:
    def __init__(self) -> None:
        self.url = os.environ.get("SUPABASE_URL", "").strip()
        self.secret_key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        self.enabled = bool(self.url and self.secret_key and create_client)
        self._thread_clients = threading.local()

    @property
    def client(self) -> Optional[Client]:
        """Return one Supabase connection pool per Flask worker thread."""
        client = getattr(self._thread_clients, "client", None)
        if client is None and self.enabled and create_client:
            client = create_client(self.url, self.secret_key)
            self._thread_clients.client = client
        return client

    @client.setter
    def client(self, value: Optional[Client]) -> None:
        # Kept as a setter for focused tests and explicit dependency injection.
        self._thread_clients.client = value

    def _safe(self, operation: str, callback, default=None):
        if not self.client:
            return default
        try:
            return callback()
        except Exception as exc:  # Persistence must not terminate a render worker.
            LOGGER.error("Supabase %s failed: %s", operation, exc)
            return default

    def readiness(self) -> Dict[str, Dict[str, Any]]:
        """Verify both PostgREST schema access and the private video bucket."""
        failed = {
            "database": {"ok": False, "detail": "unreachable"},
            "storage": {"ok": False, "detail": "unreachable"},
        }
        if not self.enabled:
            return failed
        headers = {
            "apikey": self.secret_key,
            "Authorization": f"Bearer {self.secret_key}",
        }
        try:
            database = httpx.get(
                f"{self.url}/rest/v1/plan_catalog",
                headers=headers,
                params={"select": "id", "limit": "1"},
                timeout=5.0,
            )
            storage = httpx.get(
                f"{self.url}/storage/v1/bucket/video-assets",
                headers=headers,
                timeout=5.0,
            )
            return {
                "database": {
                    "ok": database.status_code == 200,
                    "detail": "available" if database.status_code == 200 else "unavailable",
                },
                "storage": {
                    "ok": storage.status_code == 200,
                    "detail": "available" if storage.status_code == 200 else "unavailable",
                },
            }
        except httpx.HTTPError:
            return failed

    def worker_readiness(self, max_age_seconds: int = 90) -> Dict[str, Any]:
        """Verify that a production render worker can reach the durable queue."""
        if not self.enabled:
            return {"ok": False, "detail": "database not configured"}
        try:
            response = self.client.table("render_workers").select(  # type: ignore[union-attr]
                "status,last_heartbeat_at"
            ).order("last_heartbeat_at", desc=True).limit(1).execute()
            rows = list(response.data or [])
            if not rows:
                return {"ok": False, "detail": "no worker registered"}
            row = rows[0]
            heartbeat = datetime.fromisoformat(
                str(row.get("last_heartbeat_at") or "").replace("Z", "+00:00")
            )
            age = max(
                0,
                int((datetime.now(timezone.utc) - heartbeat).total_seconds()),
            )
            status = str(row.get("status") or "unknown")
            ok = status in {"idle", "busy"} and age <= max(30, max_age_seconds)
            return {
                "ok": ok,
                "detail": (
                    f"{status}; heartbeat {age}s ago"
                    if ok
                    else "worker heartbeat stale"
                ),
            }
        except Exception as exc:
            LOGGER.error("Supabase worker readiness failed: %s", type(exc).__name__)
            return {"ok": False, "detail": "worker heartbeat unavailable"}

    def _upload_file(self, bucket: str, remote_path: str, local_path: Path, mime_type: str) -> Optional[str]:
        if not self.client or not local_path.is_file():
            return None
        try:
            with local_path.open("rb") as source:
                self.client.storage.from_(bucket).upload(
                    path=remote_path,
                    file=source,
                    file_options={"content-type": mime_type, "upsert": "true"},
                )
            return remote_path
        except Exception as exc:
            LOGGER.error("Supabase upload %s failed: %s", remote_path, exc)
            return None

    def authenticate(self, authorization: str) -> Tuple[Optional[str], Optional[str]]:
        user, error = self.authenticated_user(authorization)
        return (str(user.get("id")), None) if user else (None, error)

    def authenticated_user(self, authorization: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Validate a bearer token and return its Auth user without sharing client state."""
        if not self.enabled:
            return None, None
        if not authorization.startswith("Bearer "):
            return None, "Authentication required"
        token = authorization[7:].strip()
        if not token:
            return None, "Authentication required"
        try:
            # Do not reuse supabase-py's stateful Auth client across Flask
            # worker threads. A fresh request is deterministic and validates
            # the JWT against the project's Auth server on every request.
            response = httpx.get(
                f"{self.url}/auth/v1/user",
                headers={
                    "apikey": self.secret_key,
                    "Authorization": f"Bearer {token}",
                },
                timeout=10.0,
            )
            if response.status_code in {401, 403}:
                return None, "Invalid or expired session"
            response.raise_for_status()
            user = response.json()
            user_id = str(user.get("id") or "")
            return (user, None) if user_id else (None, "Invalid or expired session")
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            LOGGER.error("Supabase Auth is unreachable: %s", type(exc).__name__)
            return None, "Authentication service temporarily unavailable"
        except Exception as exc:
            LOGGER.warning("Supabase rejected the presented session: %s", type(exc).__name__)
            return None, "Invalid or expired session"

    def reauthenticate_password(
        self, user_id: str, email: str, password: str,
    ) -> Tuple[bool, Optional[str]]:
        """Verify the caller's current password directly with Supabase Auth."""
        try:
            response = httpx.post(
                f"{self.url}/auth/v1/token",
                params={"grant_type": "password"},
                headers={"apikey": self.secret_key, "Content-Type": "application/json"},
                json={"email": email, "password": password},
                timeout=10.0,
            )
            if response.status_code in {400, 401, 403}:
                return False, "Current password is incorrect"
            response.raise_for_status()
            payload = response.json()
            verified_id = str((payload.get("user") or {}).get("id") or "")
            access_token = str(payload.get("access_token") or "")
            if access_token:
                try:
                    httpx.post(
                        f"{self.url}/auth/v1/logout",
                        headers={"apikey": self.secret_key, "Authorization": f"Bearer {access_token}"},
                        timeout=10.0,
                    )
                except httpx.HTTPError:
                    LOGGER.warning("Temporary reauthentication session could not be revoked immediately")
            return (verified_id == user_id, None if verified_id == user_id else "Current password is incorrect")
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            return False, "Authentication service temporarily unavailable"
        except Exception as exc:
            LOGGER.error("Supabase password reauthentication failed: %s", type(exc).__name__)
            return False, "Authentication service temporarily unavailable"

    def update_account_password(self, user_id: str, new_password: str) -> None:
        if not self.client:
            raise RuntimeError("Account service is unavailable")
        self.client.auth.admin.update_user_by_id(user_id, {"password": new_password})

    def record_security_event(
        self,
        subject_hash: str,
        event_type: str,
        outcome: str,
        request_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a non-PII account security event; failures never expose secrets."""
        if not self.client:
            return
        row = {
            "subject_hash": subject_hash[:64],
            "event_type": event_type[:80],
            "outcome": outcome[:32],
            "request_id": request_id[:80] or None,
            "metadata": metadata or {},
        }
        try:
            self.client.table("account_security_audit").insert(row).execute()
        except Exception as exc:
            LOGGER.error("Supabase security audit failed: %s", type(exc).__name__)

    def account_job_ids(self, user_id: str) -> List[str]:
        response = self.client.table("video_jobs").select("id").eq(  # type: ignore[union-attr]
            "user_id", user_id
        ).execute()
        return [str(row["id"]) for row in (response.data or []) if row.get("id")]

    def _storage_paths_below(self, bucket: str, prefix: str) -> List[str]:
        """Recursively enumerate Storage objects beneath a user's private prefix."""
        storage = self.client.storage.from_(bucket)  # type: ignore[union-attr]
        paths: List[str] = []
        pending = [prefix.strip("/")]
        while pending:
            folder = pending.pop()
            offset = 0
            while True:
                rows = storage.list(folder, {"limit": 1000, "offset": offset})
                for row in rows:
                    name = str(row.get("name") or "").strip("/")
                    if not name:
                        continue
                    path = f"{folder}/{name}" if folder else name
                    if row.get("id") or row.get("metadata") is not None:
                        paths.append(path)
                    else:
                        pending.append(path)
                if len(rows) < 1000:
                    break
                offset += len(rows)
        return paths

    def delete_account(self, user_id: str) -> List[str]:
        """Remove private objects then hard-delete Auth; database rows cascade."""
        if not self.client:
            raise RuntimeError("Account service is unavailable")
        try:
            self.client.rpc("prepare_account_deletion", {"p_user_id": user_id}).execute()
        except Exception as exc:
            if "active_jobs" in str(exc).lower():
                raise RuntimeError("active_jobs") from exc
            raise
        job_ids = self.account_job_ids(user_id)
        for bucket in ("avatars", "source-documents", "video-assets"):
            storage = self.client.storage.from_(bucket)
            paths = self._storage_paths_below(bucket, user_id)
            for start in range(0, len(paths), 100):
                storage.remove(paths[start:start + 100])
        self.client.auth.admin.delete_user(user_id, should_soft_delete=False)
        return job_ids

    def create_job(self, user_id: Optional[str], job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled or not user_id:
            return {"allowed": True, "local_mode": True}
        params = {
            "p_user_id": user_id,
            "p_job_id": job_id,
            "p_source_document_id": payload.get("source_document_id"),
            "p_prompt": str(payload.get("prompt", "")),
            "p_audience": str(payload.get("audience", "beginner")),
            "p_style": str(payload.get("style", "default")),
            "p_requested_duration_seconds": round(float(payload.get("duration_seconds", 0))),
            "p_mode": str(payload.get("mode", "batch")),
            "p_quality_mode": str(payload.get("quality_mode", "balanced")),
            "p_request_payload": {key: value for key, value in payload.items() if key not in {"source_text", "full_source_text"}},
        }
        try:
            response = self.client.rpc("create_video_job_with_quota", params).execute()  # type: ignore[union-attr]
            result = response.data
            if isinstance(result, list):
                result = result[0] if result else None
            return result if isinstance(result, dict) else {"allowed": False, "error": "Invalid quota response"}
        except Exception as exc:
            LOGGER.error("Atomic job creation failed: %s", type(exc).__name__)
            return {"allowed": False, "error": "Video quota service is temporarily unavailable"}

    def create_source_document(self, user_id: Optional[str], filename: str, path: str, mime_type: Optional[str], size_bytes: int, text_length: int) -> Optional[str]:
        if not user_id:
            return None
        document_id = str(uuid.uuid4())
        local_path = Path(path)
        remote_path = f"{user_id}/sources/{document_id}/{filename}"
        stored_path = self._upload_file("source-documents", remote_path, local_path, mime_type or "application/octet-stream")
        if self.enabled and not stored_path:
            LOGGER.error("Source document upload failed; refusing local-only persistence")
            return None
        row = {
            "id": document_id, "user_id": user_id, "original_filename": filename,
            "storage_path": stored_path, "mime_type": mime_type, "size_bytes": size_bytes,
            "extracted_text_length": text_length, "metadata": {"storage": "supabase"},
        }
        try:
            self.client.table("source_documents").insert(row).execute()  # type: ignore[union-attr]
            return document_id
        except Exception as exc:
            LOGGER.error("Supabase create source document failed: %s", type(exc).__name__)
            if stored_path:
                self._safe(
                    "remove orphan source document",
                    lambda: self.client.storage.from_("source-documents").remove([stored_path]),  # type: ignore[union-attr]
                )
            return None

    def update_job(self, job_id: str, changes: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        allowed = {"status", "stage", "progress", "detail", "runtime_config", "llm_usage"}
        row = {key: value for key, value in changes.items() if key in allowed and value is not None}
        if "error" in changes:
            row["error_message"] = str(changes["error"])[:4000]
        if "llm_cost_usd" in changes:
            row["estimated_cost_usd"] = changes["llm_cost_usd"]
        if row.get("status") == "running":
            row["started_at"] = _now()
        if row.get("status") in {"completed", "failed", "canceled"}:
            row["completed_at"] = _now()
            row.update({
                "locked_by": None,
                "locked_at": None,
                "heartbeat_at": None,
            })
        if row:
            # Cancellation is terminal. A worker may finish an in-flight render
            # after the user cancels, but it must never revive that database row.
            self._safe(
                "update job",
                lambda: self.client.table("video_jobs").update(row).eq("id", job_id).neq(
                    "status", "canceled"
                ).execute(),  # type: ignore[union-attr]
            )

    def cancel_job(self, user_id: Optional[str], job_id: str) -> Dict[str, Any]:
        """Atomically cancel an owned active job and release its quota slot."""
        if not self.enabled:
            return {"outcome": "canceled", "status": "canceled"}
        if not user_id:
            return {"outcome": "not_found"}
        response = self.client.rpc("cancel_video_job", {  # type: ignore[union-attr]
            "p_user_id": user_id,
            "p_job_id": job_id,
        }).execute()
        data = response.data
        if isinstance(data, list):
            data = data[0] if data else None
        return data if isinstance(data, dict) else {"outcome": "unavailable"}

    def is_job_canceled(self, job_id: str) -> bool:
        """Read the authoritative cancellation flag for a render worker."""
        if not self.enabled:
            return False
        response = self.client.table("video_jobs").select("status").eq(  # type: ignore[union-attr]
            "id", job_id
        ).maybe_single().execute()
        row = response.data if response else None
        return bool(row and row.get("status") == "canceled")

    def replace_scenes(self, user_id: Optional[str], job_id: str, scenes: List[Dict[str, Any]]) -> None:
        if not user_id or not scenes:
            return
        rows = [{
            "job_id": job_id,
            "user_id": user_id,
            "scene_index": int(scene.get("index", index)),
            "title": str(scene.get("title", f"Scene {index + 1}")),
            "narration": str(scene.get("narration", "")),
            "status": str(scene.get("status", "pending")),
            "image_url": scene.get("image_url"),
        } for index, scene in enumerate(scenes)]
        self._safe("upsert scenes", lambda: self.client.table("video_scenes").upsert(rows, on_conflict="job_id,scene_index").execute())  # type: ignore[union-attr]

    def update_scene(self, job_id: str, scene_index: int, changes: Dict[str, Any]) -> None:
        row = {
            key: value for key, value in changes.items()
            if key in {"title", "narration", "status", "image_url", "duration_seconds"}
        }
        live_manifest = {
            key: changes[key]
            for key in ("video_url", "stream_total_seconds")
            if changes.get(key) is not None
        }
        if live_manifest:
            row["scene_manifest"] = {"live": live_manifest}
        if row:
            self._safe("update scene", lambda: self.client.table("video_scenes").update(row).eq("job_id", job_id).eq("scene_index", scene_index).execute())  # type: ignore[union-attr]

    def complete_video(self, user_id: Optional[str], job_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
        if not user_id:
            return {"local_mode": True}
        plan = manifest.get("plan") if isinstance(manifest.get("plan"), dict) else {}
        request_data = manifest.get("request") if isinstance(manifest.get("request"), dict) else {}
        scenes = plan.get("scenes") if isinstance(plan.get("scenes"), list) else manifest.get("segments", [])
        normalized_scenes = []
        for fallback_index, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                continue
            if "index" in scene:
                try:
                    scene_index = max(0, int(scene["index"]))
                except (TypeError, ValueError):
                    scene_index = fallback_index
            else:
                try:
                    scene_index = max(0, int(scene.get("id", scene.get("scene_id", fallback_index + 1))) - 1)
                except (TypeError, ValueError):
                    scene_index = fallback_index
            normalized_scenes.append({
                "index": scene_index,
                "title": str(scene.get("teaching_beat") or scene.get("title") or scene.get("visual_prompt") or f"Scene {scene_index + 1}"),
                "narration": str(scene.get("narration", "")),
                "status": "done",
                "image_url": scene.get("image_url"),
            })
        self.replace_scenes(user_id, job_id, normalized_scenes)
        output_dir = Path(os.environ.get("OUTPUT_DIR", Path(__file__).resolve().parent / "outputs")) / job_id
        video_path = self._upload_file("video-assets", f"{user_id}/videos/{job_id}/final.mp4", output_dir / "final.mp4", "video/mp4")
        manifest_path = self._upload_file("video-assets", f"{user_id}/videos/{job_id}/manifest.json", output_dir / "manifest.json", "application/json")
        evaluation_path = self._upload_file("video-assets", f"{user_id}/videos/{job_id}/evaluation.json", output_dir / "evaluation.json", "application/json")
        if self.enabled and (not video_path or not manifest_path):
            raise RuntimeError("Durable video upload failed; the render was not marked complete")
        scene_paths = self.upload_scene_images(user_id, job_id, normalized_scenes)
        poster_path = scene_paths.get(0)
        row = {
            "id": job_id,
            "user_id": user_id,
            "title": str(plan.get("title") or request_data.get("prompt") or "Untitled explanation"),
            "description": str(plan.get("scope_summary") or plan.get("learning_goal") or ""),
            "audience": str(request_data.get("audience", "beginner")),
            "style": str(request_data.get("style") or "default"),
            "duration_seconds": float(manifest.get("final_duration_seconds") or request_data.get("target_duration_seconds") or 0),
            "scene_count": len(scenes),
            "video_url": None,
            "video_storage_path": video_path,
            "poster_storage_path": poster_path,
            "manifest_storage_path": manifest_path,
            "evaluation_storage_path": evaluation_path,
            "manifest": manifest,
            "coverage": plan.get("must_cover", []),
        }
        self.client.table("videos").upsert(row).execute()  # type: ignore[union-attr]
        assets = [
            {"user_id": user_id, "job_id": job_id, "video_id": job_id, "asset_type": "video", "storage_bucket": "video-assets", "storage_path": video_path, "mime_type": "video/mp4"},
            {"user_id": user_id, "job_id": job_id, "video_id": job_id, "asset_type": "manifest", "storage_bucket": "video-assets", "storage_path": manifest_path, "mime_type": "application/json"},
        ]
        if evaluation_path:
            assets.append({"user_id": user_id, "job_id": job_id, "video_id": job_id, "asset_type": "evaluation", "storage_bucket": "video-assets", "storage_path": evaluation_path, "mime_type": "application/json"})
        self._safe(
            "record video assets",
            lambda: self.client.table("video_assets").upsert(assets, on_conflict="storage_bucket,storage_path").execute(),  # type: ignore[union-attr]
        )
        self._safe(
            "link scenes",
            lambda: self.client.table("video_scenes").update({"video_id": job_id}).eq("job_id", job_id).execute(),  # type: ignore[union-attr]
        )
        return {"video_storage_path": video_path, "manifest_storage_path": manifest_path, "poster_storage_path": poster_path}

    def upload_scene_images(self, user_id: Optional[str], job_id: str, scenes: List[Dict[str, Any]]) -> Dict[int, str]:
        if not user_id:
            return {}
        uploaded: Dict[int, str] = {}
        work_dir = Path(os.environ.get("WORK_DIR", Path(__file__).resolve().parent / "work")) / job_id
        for index, scene in enumerate(scenes):
            scene_index = int(scene.get("index", index))
            scene_number = scene_index + 1
            candidates = (
                work_dir / "segments" / f"scene_{scene_number:03d}" / "scene.png",
                work_dir / "scenes" / f"scene_{scene_number:03d}.png",
            )
            local_path = next((candidate for candidate in candidates if candidate.is_file()), None)
            if not local_path:
                continue
            remote_path = self._upload_file("video-assets", f"{user_id}/videos/{job_id}/scenes/{scene_number:03d}.png", local_path, "image/png")
            if remote_path:
                uploaded[scene_index] = remote_path
                self._safe("store scene image", lambda: self.client.table("video_scenes").update({"image_storage_path": remote_path}).eq("job_id", job_id).eq("scene_index", scene_index).execute())  # type: ignore[union-attr]
        return uploaded

    def signed_storage_url(self, bucket: str, storage_path: Optional[str], expires_in: int = 900) -> Optional[str]:
        """Return a short-lived URL for a private object without exposing the service key."""
        if not self.client or not storage_path:
            return None
        try:
            result = self.client.storage.from_(bucket).create_signed_url(storage_path, expires_in)
            if isinstance(result, dict):
                signed = result.get("signedURL") or result.get("signedUrl") or result.get("signed_url")
                if isinstance(signed, str) and signed.startswith("/"):
                    return f"{self.url.rstrip('/')}{signed}"
                return signed if isinstance(signed, str) else None
        except Exception as exc:
            LOGGER.error("Supabase signed URL failed for %s: %s", storage_path, type(exc).__name__)
        return None

    def list_video_records(self, user_id: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        if not self.enabled:
            return None
        if not user_id:
            return []
        response = self.client.table("videos").select(  # type: ignore[union-attr]
            "id,title,audience,duration_seconds,poster_storage_path,created_at"
        ).eq("user_id", user_id).order("created_at", desc=True).execute()
        return list(response.data or [])

    def video_record(self, user_id: Optional[str], job_id: str) -> Optional[Dict[str, Any]]:
        if not self.enabled or not user_id:
            return None
        response = self.client.table("videos").select("*").eq("id", job_id).eq(  # type: ignore[union-attr]
            "user_id", user_id
        ).maybe_single().execute()
        return response.data if response else None

    def scene_records(self, user_id: Optional[str], job_id: str) -> List[Dict[str, Any]]:
        if not self.enabled or not user_id:
            return []
        response = self.client.table("video_scenes").select("*").eq(  # type: ignore[union-attr]
            "job_id", job_id
        ).eq("user_id", user_id).order("scene_index").execute()
        return list(response.data or [])

    def job_record(self, user_id: Optional[str], job_id: str) -> Optional[Dict[str, Any]]:
        if not self.enabled or not user_id:
            return None
        response = self.client.table("video_jobs").select("*").eq("id", job_id).eq(  # type: ignore[union-attr]
            "user_id", user_id
        ).maybe_single().execute()
        return response.data if response else None

    def active_job_records(self, user_id: Optional[str]) -> List[Dict[str, Any]]:
        if not self.enabled or not user_id:
            return []
        response = self.client.table("video_jobs").select(  # type: ignore[union-attr]
            "id,prompt,audience,style,requested_duration_seconds,status,stage,progress,detail,created_at"
        ).eq("user_id", user_id).in_("status", ["queued", "running"]).order(
            "created_at", desc=True
        ).execute()
        return list(response.data or [])

    def claim_job(self, worker_id: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        response = self.client.rpc("claim_video_job", {"p_worker_id": worker_id}).execute()  # type: ignore[union-attr]
        data = response.data
        if isinstance(data, list):
            data = data[0] if data else None
        return data if isinstance(data, dict) else None

    def heartbeat_job(self, job_id: str, worker_id: str) -> bool:
        if not self.enabled:
            return False
        response = self.client.rpc("heartbeat_video_job", {  # type: ignore[union-attr]
            "p_job_id": job_id, "p_worker_id": worker_id,
        }).execute()
        return bool(response.data)

    def recover_stale_jobs(self, stale_after_seconds: int = 300) -> int:
        if not self.enabled:
            return 0
        response = self.client.rpc("recover_stale_video_jobs", {  # type: ignore[union-attr]
            "p_stale_after_seconds": max(60, stale_after_seconds),
        }).execute()
        return int(response.data or 0)

    def set_worker_status(
        self, worker_id: str, status: str, current_job_id: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        payload = {
            "worker_id": worker_id[:200],
            "status": status,
            "current_job_id": current_job_id,
            "last_heartbeat_at": _now(),
            "metadata": {"hostname": os.environ.get("HOSTNAME", "unknown")[:200]},
        }
        self.client.table("render_workers").upsert(  # type: ignore[union-attr]
            payload, on_conflict="worker_id"
        ).execute()

    def source_document_blob(self, document_id: Optional[str]) -> Optional[Tuple[str, bytes]]:
        if not self.enabled or not document_id:
            return None
        response = self.client.table("source_documents").select(  # type: ignore[union-attr]
            "original_filename,storage_path"
        ).eq("id", document_id).maybe_single().execute()
        row = response.data if response else None
        if not row or not row.get("storage_path"):
            return None
        body = self.client.storage.from_("source-documents").download(row["storage_path"])
        return str(row.get("original_filename") or "source.txt"), bytes(body)

    def delete_source_document(self, user_id: Optional[str], document_id: Optional[str]) -> None:
        if not self.enabled or not user_id or not document_id:
            return
        response = self.client.table("source_documents").select("storage_path").eq(  # type: ignore[union-attr]
            "id", document_id
        ).eq("user_id", user_id).maybe_single().execute()
        row = response.data if response else None
        if row and row.get("storage_path"):
            self._safe(
                "remove rejected source object",
                lambda: self.client.storage.from_("source-documents").remove([row["storage_path"]]),  # type: ignore[union-attr]
            )
        self._safe(
            "remove rejected source row",
            lambda: self.client.table("source_documents").delete().eq("id", document_id).eq("user_id", user_id).execute(),  # type: ignore[union-attr]
        )

    def consume_rate_limit(self, bucket_key: str, limit: int, window_seconds: int) -> Dict[str, Any]:
        if not self.enabled:
            return {"allowed": True, "local_mode": True}
        response = self.client.rpc("consume_rate_limit", {  # type: ignore[union-attr]
            "p_bucket_key": bucket_key,
            "p_limit": limit,
            "p_window_seconds": window_seconds,
        }).execute()
        data = response.data
        if isinstance(data, list):
            data = data[0] if data else None
        return data if isinstance(data, dict) else {"allowed": False, "retry_after_seconds": window_seconds}

    def quota_status(self, user_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not self.enabled or not user_id:
            return None
        try:
            profile = self.client.table("profiles").select("current_plan_id,videos_used,plan_reset_at").eq("id", user_id).single().execute().data  # type: ignore[union-attr]
            plan = self.client.table("plan_catalog").select("video_limit").eq("id", profile["current_plan_id"]).single().execute().data  # type: ignore[union-attr]
            reset_at = datetime.fromisoformat(str(profile["plan_reset_at"]).replace("Z", "+00:00"))
            used = int(profile.get("videos_used", 0))
            if reset_at <= datetime.now(timezone.utc):
                used = 0
                reset_at = datetime.now(timezone.utc) + timedelta(days=30)
                self.client.table("profiles").update({"videos_used": 0, "plan_period_start": _now(), "plan_reset_at": reset_at.isoformat()}).eq("id", user_id).execute()  # type: ignore[union-attr]
            limit = int(plan["video_limit"])
            return {"allowed": used < limit, "used": used, "limit": limit, "reset_at": reset_at.isoformat()}
        except Exception as exc:
            LOGGER.error("Supabase quota check failed: %s", exc)
            return {"allowed": False, "error": "Plan quota is temporarily unavailable"}

    def owns_job(self, user_id: Optional[str], job_id: str) -> bool:
        if not self.enabled:
            return True
        if not user_id:
            return False
        response = self._safe("check job owner", lambda: self.client.table("video_jobs").select("id").eq("id", job_id).eq("user_id", user_id).maybe_single().execute())  # type: ignore[union-attr]
        return bool(response and response.data)

    def owned_job_ids(self, user_id: Optional[str]) -> Optional[set[str]]:
        """Fetch ownership once for list views instead of issuing one query per file."""
        if not self.enabled:
            return None
        if not user_id:
            return set()
        try:
            response = httpx.get(
                f"{self.url}/rest/v1/video_jobs",
                headers={
                    "apikey": self.secret_key,
                    "Authorization": f"Bearer {self.secret_key}",
                },
                params={"select": "id", "user_id": f"eq.{user_id}"},
                timeout=10.0,
            )
            response.raise_for_status()
            rows = response.json()
        except Exception as exc:
            LOGGER.error("Supabase list owned jobs failed: %s", type(exc).__name__)
            return set()
        return {str(row["id"]) for row in rows if row.get("id")}

    def authorized_job_ids(self, authorization: str) -> Tuple[Optional[set[str]], Optional[str], int]:
        """Authenticate and list the caller's jobs in one RLS-protected request."""
        if not self.enabled:
            return None, None, 200
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            return set(), "Authentication required", 401
        token = authorization[7:].strip()
        try:
            response = httpx.get(
                f"{self.url}/rest/v1/video_jobs",
                headers={"apikey": self.secret_key, "Authorization": f"Bearer {token}"},
                params={"select": "id"},
                timeout=10.0,
            )
            if response.status_code in {401, 403}:
                return set(), "Invalid or expired session", 401
            response.raise_for_status()
            rows = response.json()
            return {str(row["id"]) for row in rows if row.get("id")}, None, 200
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            LOGGER.error("Supabase authenticated job list is unreachable: %s", type(exc).__name__)
            return set(), "Authentication service temporarily unavailable", 503
        except Exception as exc:
            LOGGER.warning("Supabase rejected authenticated job list: %s", type(exc).__name__)
            return set(), "Invalid or expired session", 401


supabase_store = SupabaseStore()
