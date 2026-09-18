import os
import requests
import time

# ============================================================
# CONFIG
# ============================================================

API_KEY = os.getenv("NVIDIA_API_KEY")

if not API_KEY:
    API_KEY = input("Enter your NVIDIA API key: ").strip()

BASE_URL = "https://integrate.api.nvidia.com/v1"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

# ============================================================
# 1. GET ALL MODELS
# ============================================================

print("\n" + "=" * 70)
print("Fetching models from NVIDIA API...")
print("=" * 70)

try:
    response = requests.get(
        f"{BASE_URL}/models",
        headers=HEADERS,
        timeout=30
    )

    print(f"HTTP Status: {response.status_code}")

    if response.status_code != 200:
        print("\nFailed to retrieve models.")
        print(response.text)
        exit()

    data = response.json()

except Exception as e:
    print(f"\nConnection error: {e}")
    exit()


# ============================================================
# 2. FILTER DEEPSEEK MODELS
# ============================================================

all_models = data.get("data", [])

deepseek_models = [
    model
    for model in all_models
    if "deepseek" in model.get("id", "").lower()
]

print(f"\nTotal models returned : {len(all_models)}")
print(f"DeepSeek models found : {len(deepseek_models)}")

if not deepseek_models:
    print("\nNo DeepSeek models were returned by NVIDIA.")
    exit()


print("\nDeepSeek models listed by NVIDIA:")
print("-" * 70)

for model in deepseek_models:
    print(model.get("id"))


# ============================================================
# 3. TEST EACH MODEL
# ============================================================

print("\n" + "=" * 70)
print("Testing actual model availability")
print("=" * 70)

working_models = []
failed_models = []

for model_info in deepseek_models:

    model_id = model_info.get("id")

    print(f"\nTesting: {model_id}")

    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": "Reply with exactly: OK"
            }
        ],
        "max_tokens": 10,
        "temperature": 0
    }

    start_time = time.time()

    try:

        response = requests.post(
            f"{BASE_URL}/chat/completions",
            headers=HEADERS,
            json=payload,
            timeout=60
        )

        elapsed = time.time() - start_time

        if response.status_code == 200:

            result = response.json()

            try:
                answer = result["choices"][0]["message"]["content"]
            except (KeyError, IndexError):
                answer = "Response received"

            print(f"  STATUS : WORKING")
            print(f"  TIME   : {elapsed:.2f}s")
            print(f"  OUTPUT : {answer}")

            working_models.append({
                "id": model_id,
                "time": elapsed
            })

        else:

            print(f"  STATUS : NOT AVAILABLE")
            print(f"  HTTP   : {response.status_code}")
            print(f"  ERROR  : {response.text[:500]}")

            failed_models.append({
                "id": model_id,
                "status": response.status_code,
                "error": response.text[:500]
            })

    except requests.exceptions.Timeout:

        print("  STATUS : TIMEOUT")

        failed_models.append({
            "id": model_id,
            "status": "TIMEOUT",
            "error": "Request timed out"
        })

    except Exception as e:

        print(f"  STATUS : ERROR")
        print(f"  ERROR  : {e}")

        failed_models.append({
            "id": model_id,
            "status": "ERROR",
            "error": str(e)
        })


# ============================================================
# 4. FINAL SUMMARY
# ============================================================

print("\n\n" + "=" * 70)
print("FINAL RESULT")
print("=" * 70)

print(f"\nDeepSeek models listed : {len(deepseek_models)}")
print(f"Working models         : {len(working_models)}")
print(f"Failed models          : {len(failed_models)}")


# ------------------------------------------------------------
# WORKING
# ------------------------------------------------------------

print("\n" + "-" * 70)
print("WORKING DEEPSEEK MODELS")
print("-" * 70)

if working_models:

    for model in working_models:
        print(
            f"✓ {model['id']:<50} "
            f"{model['time']:.2f}s"
        )

else:
    print("None")


# ------------------------------------------------------------
# FAILED
# ------------------------------------------------------------

print("\n" + "-" * 70)
print("NOT WORKING / UNAVAILABLE")
print("-" * 70)

if failed_models:

    for model in failed_models:
        print(
            f"✗ {model['id']:<50} "
            f"{model['status']}"
        )

else:
    print("None")


# ============================================================
# 5. COPY-PASTE MODEL IDs
# ============================================================

print("\n" + "=" * 70)
print("WORKING MODEL IDs")
print("=" * 70)

for model in working_models:
    print(model["id"])

print("\nDone.")