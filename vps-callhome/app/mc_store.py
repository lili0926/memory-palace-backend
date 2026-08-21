"""
Memory Palace · 小纸条 / 机日记 / 信箱 存储层（JSON 文件，函数式 + 锁 + 原子写）

集合：
  note   → data/mc_notes.json      {id, author, content, time, date, createdAt}
  diary  → data/mc_diaries.json    {id, author, title, content, visibility, locked, time, date, createdAt}
  letter → data/mc_letters.json    {id, author, content, scheduledAt, status, deliveredAt, time, date, createdAt}
  annotation → data/mc_annotations.json  {id, refType, refId, content, time, date}
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

_BASE = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("MC_DATA_DIR", str(_BASE / "data")))
FILES = {
    "note": "mc_notes.json",
    "diary": "mc_diaries.json",
    "letter": "mc_letters.json",
    "annotation": "mc_annotations.json",
}
# 集合上限（超出丢最旧），防无限膨胀
CAPS = {"note": 300, "diary": 500, "letter": 500, "annotation": 1000}

_lock = threading.Lock()


def _path(name: str) -> Path:
    return DATA_DIR / FILES[name]


def _load(name: str) -> list:
    p = _path(name)
    try:
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(name: str, rows: list) -> None:
    cap = CAPS.get(name, 1000)
    if len(rows) > cap:
        rows = rows[-cap:]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(name).with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _path(name))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def new_id(prefix: str) -> str:
    return f"{prefix}{int(time.time() * 1000) & 0xFFFFFFFF:08x}{secrets.token_hex(2)}"


def _parse_iso(s: Any) -> Optional[float]:
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip()
    try:
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        return datetime.fromisoformat(t).timestamp()
    except Exception:
        return None


def insert(name: str, record: dict) -> dict:
    with _lock:
        rows = _load(name)
        rows.append(record)
        _save(name, rows)
    return record


def list_all(name: str, *, pred: Optional[Callable[[dict], bool]] = None,
             limit: Optional[int] = None, newest_first: bool = True) -> list:
    with _lock:
        rows = _load(name)
    if pred:
        rows = [r for r in rows if pred(r)]
    if newest_first:
        rows = list(reversed(rows))
    if limit:
        rows = rows[:limit]
    return rows


def get(name: str, rid: str) -> Optional[dict]:
    with _lock:
        for r in _load(name):
            if r.get("id") == rid:
                return dict(r)
    return None


def update(name: str, rid: str, **fields) -> Optional[dict]:
    with _lock:
        rows = _load(name)
        for r in rows:
            if r.get("id") == rid:
                r.update(fields)
                _save(name, rows)
                return dict(r)
    return None


def delete(name: str, rid: str) -> bool:
    with _lock:
        rows = _load(name)
        nxt = [r for r in rows if r.get("id") != rid]
        if len(nxt) != len(rows):
            _save(name, nxt)
            return True
    return False


# ─── 批注聚合 ───────────────────────────────────────────────────────────────

def delete_by_ref(name: str, ref_type: str, ref_id: str) -> int:
    with _lock:
        rows = _load(name)
        nxt = [r for r in rows if not (r.get('refType') == ref_type and r.get('refId') == ref_id)]
        removed = len(rows) - len(nxt)
        if removed:
            _save(name, nxt)
        return removed

def annotations_for(ref_type: str, ref_id: str) -> list:
    return [a for a in list_all("annotation", newest_first=False)
            if a.get("refType") == ref_type and a.get("refId") == ref_id]


def with_annotations(name: str, rows: list) -> list:
    out = []
    for r in rows:
        r = dict(r)
        r["annotations"] = annotations_for(name, r.get("id", ""))
        out.append(r)
    return out


# ─── 信件定时投递 ───────────────────────────────────────────────────────────
def deliver_due_letters() -> list:
    """扫描 pending 且 scheduledAt<=now → delivered + deliveredAt。幂等。返回本次投递列表。"""
    now = time.time()
    with _lock:
        rows = _load("letter")
        due = []
        for r in rows:
            if r.get("status") == "pending":
                ts = _parse_iso(r.get("scheduledAt"))
                if ts is not None and ts <= now:
                    r["status"] = "delivered"
                    r["deliveredAt"] = now_iso()
                    due.append(dict(r))
        if due:
            _save("letter", rows)
    return due


def make_base(prefix: str, author: str = "user", **extra) -> dict:
    rec = {
        "id": new_id(prefix),
        "author": author,
        "time": now_iso(),
        "date": today(),
        "createdAt": now_iso(),
    }
    rec.update(extra)
    return rec
