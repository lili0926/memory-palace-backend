"""Background loop: every N seconds run wake rules."""
from __future__ import annotations

import os
import threading
import time

from .wake_engine import run_once

INTERVAL = int(os.getenv("WAKE_INTERVAL_SEC", "300"))
_started = False


def _loop():
    while True:
        try:
            run_once("default")
        except Exception as e:
            print("[wake] error:", e)
        time.sleep(max(60, INTERVAL))


def start_scheduler():
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_loop, name="wake-scheduler", daemon=True)
    t.start()
    print(f"[wake] scheduler started interval={INTERVAL}s")
