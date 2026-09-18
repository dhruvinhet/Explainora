"""Destructive-to-test-data-only Supabase integration smoke test.

Creates disposable confirmed users, exercises plan checkout and atomic quota
reservation concurrently, verifies refund behavior, then deletes every test
user so all related rows are removed by cascade.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "frontend" / ".env.local")

URL = os.environ["SUPABASE_URL"]
SECRET = os.environ["SUPABASE_SECRET_KEY"]
EXPECTED = {
    "free": {"limit": 5, "color": "#60a5fa"},
    "pro": {"limit": 10, "color": "#1e40af"},
    "pro_ultra": {"limit": 20, "color": "#7c3aed"},
}


def service_client():
    return create_client(URL, SECRET)


def wait_for_profile(client, user_id: str) -> dict:
    for _ in range(20):
        rows = client.table("profiles").select("*").eq("id", user_id).execute().data
        if rows:
            return rows[0]
        time.sleep(0.25)
    raise AssertionError("Auth profile trigger did not create a profile")


def reserve(user_id: str, plan: str, index: int) -> dict:
    job_id = uuid.uuid4().hex[:12]
    result = service_client().rpc(
        "create_video_job_with_quota",
        {
            "p_user_id": user_id,
            "p_job_id": job_id,
            "p_source_document_id": None,
            "p_prompt": f"Quota smoke test {plan} {index}",
            "p_audience": "beginner",
            "p_style": "default",
            "p_requested_duration_seconds": 20,
            "p_mode": "batch",
            "p_quality_mode": "balanced",
            "p_request_payload": {"test": True},
        },
    ).execute().data
    result["job_id"] = job_id
    return result


def run_plan(plan: str, expected_limit: int) -> dict:
    admin = service_client()
    suffix = uuid.uuid4().hex
    email = f"simi-e2e-{suffix}@example.invalid"
    password = f"Tmp-{suffix}-Aa1!"
    created = admin.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True,
         "user_metadata": {"full_name": "Simi E2E Test"}}
    )
    user_id = str(created.user.id)
    try:
        profile = wait_for_profile(admin, user_id)
        assert profile["current_plan_id"] == "free"

        plan_assignment_verified = plan == "free"
        if plan != "free":
            # Billing is intentionally not simulated through a browser-callable
            # RPC. Service-role setup assigns the fixture plan only for quota
            # testing; real upgrades must come from a verified provider webhook.
            admin.table("subscriptions").update({"status": "expired"}).eq(
                "user_id", user_id
            ).in_("status", ["trialing", "active", "past_due"]).execute()
            admin.table("subscriptions").insert({
                "user_id": user_id, "plan_id": plan, "status": "active",
                "provider": "test_fixture",
            }).execute()
            admin.table("profiles").update({
                "current_plan_id": plan, "videos_used": 0,
            }).eq("id", user_id).execute()
            upgraded_profile = wait_for_profile(admin, user_id)
            plan_assignment_verified = upgraded_profile["current_plan_id"] == plan
            assert plan_assignment_verified

        with ThreadPoolExecutor(max_workers=min(expected_limit + 3, 12)) as pool:
            results = list(pool.map(lambda i: reserve(user_id, plan, i), range(expected_limit + 3)))
        allowed = [result for result in results if result.get("allowed")]
        denied = [result for result in results if not result.get("allowed")]
        assert len(allowed) == expected_limit, (plan, len(allowed), expected_limit)
        assert len(denied) == 3, (plan, len(denied))

        profile = wait_for_profile(admin, user_id)
        jobs = admin.table("video_jobs").select("id,status").eq("user_id", user_id).execute().data
        assert profile["videos_used"] == expected_limit
        assert len(jobs) == expected_limit

        refunded_job = allowed[0]["job_id"]
        admin.table("video_jobs").update({"status": "failed"}).eq("id", refunded_job).execute()
        after_refund = wait_for_profile(admin, user_id)
        assert after_refund["videos_used"] == expected_limit - 1
        replacement = reserve(user_id, plan, expected_limit + 99)
        assert replacement.get("allowed") is True
        final_profile = wait_for_profile(admin, user_id)
        assert final_profile["videos_used"] == expected_limit

        payments = admin.table("payment_transactions").select("id").eq("user_id", user_id).execute().data
        subscriptions = admin.table("subscriptions").select("plan_id,status").eq("user_id", user_id).execute().data
        return {
            "plan": plan,
            "limit": expected_limit,
            "allowed": len(allowed),
            "denied": len(denied),
            "refund_and_replacement": True,
            "fixture_plan_assignment": plan_assignment_verified,
            "payment_rows": len(payments),
            "subscription_rows": len(subscriptions),
        }
    finally:
        admin.auth.admin.delete_user(user_id)


def main() -> None:
    client = service_client()
    catalog = client.table("plan_catalog").select("id,video_limit,theme_color").execute().data
    actual = {row["id"]: {"limit": row["video_limit"], "color": row["theme_color"]} for row in catalog}
    assert actual == EXPECTED, {"expected": EXPECTED, "actual": actual}
    results = [run_plan(plan, details["limit"]) for plan, details in EXPECTED.items()]
    print(json.dumps({"catalog": actual, "plans": results, "cleanup": "all disposable users deleted"}, indent=2))


if __name__ == "__main__":
    main()
