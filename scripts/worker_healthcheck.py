"""Docker healthcheck for a worker process."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


path = Path(os.environ.get("WORKER_HEALTH_FILE", "/tmp/pencilvid-worker-heartbeat"))
max_age = max(30, int(os.environ.get("WORKER_HEALTH_MAX_AGE_SECONDS", "90")))
try:
    age = time.time() - path.stat().st_mtime
except OSError:
    raise SystemExit(1)
raise SystemExit(0 if age <= max_age else 1)
