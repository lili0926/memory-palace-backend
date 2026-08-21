"""「她在不在」的唯一可信信号。移植自 guardian 的 presence.js。

任何正在跟她说话的入口写 data/last_active.json（wake/store.add_event 持久化），
daemon 只读。三态：present(<6h) / absent(<12h) / unknown(>12h)。
present 与 unknown → 不主动开口（宁可多沉默几小时，不要在她面前说她不在）。

FRESH 6h：6h 内 = 她此刻在；2×FRESH 以外 = 完全不知道。
"""
import json
import os
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
LAST_ACTIVE_PATH = os.getenv("PRESENCE_PATH", os.path.join(BASE, "data", "last_active.json"))

FRESH_MS = 6 * 60 * 60 * 1000     # 她此刻在
UNKNOWN_MS = FRESH_MS * 2         # 完全不知道在不在


def _parse_ts(value):
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts / 1000.0 if ts > 1e12 else ts
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def read_presence():
    try:
        with open(LAST_ACTIVE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        # 文件不存在 = 从来没打过点，不等于她不在。返回 ok:false 让上层当「没信号」。
        return {"ok": False, "reason": "还没有任何打点", "present": False, "status": "unknown",
                "age_minutes": None, "via": "unknown", "note": "", "ts": None}
    except Exception as e:
        return {"ok": False, "reason": str(e), "present": False, "status": "unknown",
                "age_minutes": None, "via": "unknown", "note": "", "ts": None}
    ts = _parse_ts(data.get("ts") or data.get("time"))
    if ts is None:
        return {"ok": False, "reason": "ts 不可解析", "present": False, "status": "unknown",
                "age_minutes": None, "via": "unknown", "note": "", "ts": None}
    age_ms = max(0.0, (time.time() - ts) * 1000)
    if age_ms <= FRESH_MS:
        status = "present"
    elif age_ms <= UNKNOWN_MS:
        status = "absent"
    else:
        status = "unknown"
    return {
        "ok": True,
        "present": age_ms <= FRESH_MS,
        "status": status,
        "age_ms": age_ms,
        "age_minutes": int(age_ms // 60000),
        "via": data.get("via", "unknown"),
        "note": data.get("note", ""),
        "ts": data.get("ts"),
    }


def mark_present(via="app", note=""):
    os.makedirs(os.path.dirname(LAST_ACTIVE_PATH) or ".", exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "via": via or "unknown",
        "note": note or "",
    }
    tmp = LAST_ACTIVE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, LAST_ACTIVE_PATH)
    return read_presence()
