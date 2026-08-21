"""In-memory store for V1. Swap for SQLite/Postgres later."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

# 「她在不在」的唯一可信信号持久化到 data/last_active.json（跨重启），presence.py 只读。
_LAST_ACTIVE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "last_active.json"
)

_lock = threading.Lock()
_events: list[dict] = []
_wake_queue: list[dict] = []
_user_state: dict[str, Any] = {
    "user_id": "default",
    "last_active": None,
    "mood": "",
    "recent_topics": [],
    "relationship_score": 50,
    "dnd": False,
}


def _persist_last_active(created_at: str, via: str) -> None:
    """把 last_active 落盘，presence.py 跨重启也能读到「她在不在」。"""
    try:
        os.makedirs(os.path.dirname(_LAST_ACTIVE_PATH) or ".", exist_ok=True)
        tmp = _LAST_ACTIVE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": created_at, "via": via or "unknown", "note": ""}, f, ensure_ascii=False)
        os.replace(tmp, _LAST_ACTIVE_PATH)
    except Exception:
        pass  # 落盘失败不阻塞事件写入


def _load_persisted_last_active() -> None:
    """重启后从 data/last_active.json 恢复 last_active，让 rules/presence 读到同一个值。"""
    try:
        with open(_LAST_ACTIVE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        ts = d.get("ts")
        if ts:
            _user_state["last_active"] = ts
    except Exception:
        pass


_load_persisted_last_active()


def add_event(user_id: str, type_: str, payload: dict | None = None, time_str: str | None = None) -> dict:
    ev = {
        "id": uuid.uuid4().hex,
        "user_id": user_id or "default",
        "type": type_,
        "payload": payload or {},
        "created_at": time_str or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ts": time.time(),
    }
    with _lock:
        _events.append(ev)
        if len(_events) > 2000:
            del _events[:500]
        st = _user_state
        st["last_active"] = ev["created_at"]
        if type_ == "chat_message":
            topics = st.get("recent_topics") or []
            text = str((payload or {}).get("text") or "")[:40]
            if text:
                topics = (topics + [text])[-8:]
                st["recent_topics"] = topics
    _persist_last_active(ev["created_at"], type_)
    return ev


def list_events(user_id: str = "default", limit: int = 50) -> list[dict]:
    with _lock:
        rows = [e for e in _events if e.get("user_id") == user_id]
        return rows[-limit:]


def get_state(user_id: str = "default") -> dict:
    with _lock:
        return dict(_user_state)


def set_state(**kwargs) -> dict:
    with _lock:
        _user_state.update({k: v for k, v in kwargs.items() if v is not None})
        return dict(_user_state)


def enqueue_wake(item: dict, no_dedup: bool = False) -> dict:
    row = {
        "id": uuid.uuid4().hex,
        "user_id": item.get("user_id") or "default",
        "reason": item.get("reason") or "",
        "message": item.get("message") or "",
        "priority": int(item.get("priority") or 5),
        "channel": item.get("channel") or "chat",
        "thread_id": item.get("thread_id") or "",
        # need_persona=true：正文应由前端用 agent 人设生成，message 只是欲望上下文 hint
        "need_persona": bool(item.get("need_persona")),
        "status": "pending",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ts": time.time(),
    }
    with _lock:
        if no_dedup:
            # 直通入队（如未接留言），不被更高优先级的 pending wake 吞掉
            _wake_queue.append(row)
            if len(_wake_queue) > 200:
                del _wake_queue[:50]
            return row
        # dedupe same reason within 2h
        for w in reversed(_wake_queue):
            if w.get("reason") == row["reason"] and w.get("status") == "pending":
                if row["ts"] - float(w.get("ts") or 0) < 7200:
                    return w
            if w.get("status") == "pending" and row["ts"] - float(w.get("ts") or 0) < 1800:
                # already have a recent pending wake
                if w.get("priority", 0) >= row["priority"]:
                    return w
        _wake_queue.append(row)
        if len(_wake_queue) > 200:
            del _wake_queue[:50]
    return row


def pull_wakes(user_id: str = "default", limit: int = 5) -> list[dict]:
    out = []
    with _lock:
        for w in _wake_queue:
            if w.get("user_id") != user_id:
                continue
            if w.get("status") != "pending":
                continue
            w["status"] = "delivered"
            out.append(dict(w))
            if len(out) >= limit:
                break
    return out


def list_wakes(limit: int = 20) -> list[dict]:
    with _lock:
        return list(_wake_queue[-limit:])
