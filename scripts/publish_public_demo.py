"""Publish validated PencilVid demo media to the public Supabase bucket.

The populated .env remains local. This command creates the narrowly scoped
public bucket if needed, replaces the two stable marketing objects, and prints
only their public URLs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv


BUCKET = "public-demo"
VIDEO_OBJECT = "pencilvid-demo-30s.mp4"
POSTER_OBJECT = "pencilvid-demo-poster.jpg"
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_POSTER_BYTES = 5 * 1024 * 1024


def _require_file(path: Path, *, max_bytes: int) -> None:
    if not path.is_file():
        raise SystemExit(f"Missing media file: {path}")
    size = path.stat().st_size
    if size <= 0 or size > max_bytes:
        raise SystemExit(f"Unexpected media size for {path.name}: {size} bytes")


def _request(client: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise SystemExit(
            f"Supabase Storage request failed ({response.status_code}): {response.text[:500]}"
        )
    return response


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("poster", type=Path)
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    secret_key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not supabase_url or not secret_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SECRET_KEY are required in the local .env")

    _require_file(args.video, max_bytes=MAX_VIDEO_BYTES)
    _require_file(args.poster, max_bytes=MAX_POSTER_BYTES)

    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    with httpx.Client(base_url=f"{supabase_url}/storage/v1", headers=headers, timeout=60) as client:
        bucket = client.get(f"/bucket/{BUCKET}")
        bucket_payload = {
            "id": BUCKET,
            "name": BUCKET,
            "public": True,
            "file_size_limit": MAX_VIDEO_BYTES,
            "allowed_mime_types": ["video/mp4", "image/jpeg", "image/png"],
        }
        bucket_missing = bucket.status_code == 404 or (
            bucket.status_code == 400 and "Bucket not found" in bucket.text
        )
        if bucket_missing:
            _request(client, "POST", "/bucket", json=bucket_payload)
        elif bucket.status_code >= 400:
            _request(client, "GET", f"/bucket/{BUCKET}")
        else:
            update_payload = dict(bucket_payload)
            update_payload.pop("id")
            _request(client, "PUT", f"/bucket/{BUCKET}", json=update_payload)

        for local_path, object_name, content_type in (
            (args.video, VIDEO_OBJECT, "video/mp4"),
            (args.poster, POSTER_OBJECT, "image/jpeg"),
        ):
            with local_path.open("rb") as source:
                _request(
                    client,
                    "POST",
                    f"/object/{BUCKET}/{object_name}",
                    headers={
                        "x-upsert": "true",
                        "cache-control": "public, max-age=3600",
                        "content-type": content_type,
                    },
                    content=source.read(),
                )

    public_base = f"{supabase_url}/storage/v1/object/public/{BUCKET}"
    print(json.dumps({
        "VITE_PUBLIC_DEMO_URL": f"{public_base}/{VIDEO_OBJECT}",
        "VITE_PUBLIC_DEMO_POSTER_URL": f"{public_base}/{POSTER_OBJECT}",
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
