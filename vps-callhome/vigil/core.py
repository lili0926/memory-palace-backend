"""vigil 地基：意识连续性 + 数据落地（对接 memory-palace 的 wake/store，同进程）。

数据文件落在 vigi l/data/（next_wake.json / vigil_log.json），进程重启可恢复。
用户状态直接读 wake/store（main.py 同进程内存），不用走 HTTP。
"""
import os
import json
import datetime

from wake import store as wake_store

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
NEXT_WAKE = os.path.join(DATA, "next_wake.json")
VIGIL_LOG = os.path.join(DATA, "vigil_log.json")

TZ = datetime.timezone(datetime.timedelta(hours=8))


def _now():
    return datetime.datetime.now(TZ)


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ──────────────── next_wake ────────────────

def set_next_wake(when, reason=""):
    if isinstance(when, datetime.datetime):
        when = when.isoformat()
    _write(NEXT_WAKE, {"time": when, "reason": reason})


def set_next_wake_in(minutes, reason=""):
    set_next_wake(_now() + datetime.timedelta(minutes=minutes), reason)


def get_next_wake():
    return _read(NEXT_WAKE, None)


def clear_next_wake():
    try:
        os.remove(NEXT_WAKE)
    except FileNotFoundError:
        pass


# ──────────────── vigil_log ────────────────

def append_vigil_log(content, activities=None, murmur="", wake="", duration_min=0):
    logs = _read(VIGIL_LOG, [])
    if not isinstance(logs, list):
        logs = []
    entry = {
        "date": _now().strftime("%Y.%m.%d %H:%M"),
        "ts": _now().isoformat(),
        "wake": wake or _now().strftime("%H:%M"),
        "duration_min": duration_min,
        "activities": activities or [],
        "murmur": murmur,
        "content": content,
        "consumed": False,
    }
    logs.append(entry)
    if len(logs) > 200:
        logs = logs[-200:]
    _write(VIGIL_LOG, logs)
    return entry


def last_vigil_log():
    logs = _read(VIGIL_LOG, [])
    return logs[-1] if isinstance(logs, list) and logs else None


def last_unconsumed_vigil_log():
    logs = _read(VIGIL_LOG, [])
    if not isinstance(logs, list):
        return None
    for e in reversed(logs):
        if not e.get("consumed"):
            return e
    return None


def mark_vigil_consumed(ts):
    logs = _read(VIGIL_LOG, [])
    if not isinstance(logs, list):
        return
    changed = False
    for e in logs:
        if e.get("ts") == ts and not e.get("consumed"):
            e["consumed"] = True
            changed = True
    if changed:
        _write(VIGIL_LOG, logs)


def recent_vigil_digest(n=5):
    logs = _read(VIGIL_LOG, [])
    if not isinstance(logs, list) or not logs:
        return ""
    out = []
    for e in logs[-n:]:
        texts = [a.get("text", "").strip() for a in (e.get("activities") or []) if a.get("text")]
        summary = ("；".join(texts) or e.get("murmur", "") or e.get("content", "")).replace("\n", " ")[:180]
        if summary:
            out.append(f"  {e.get('date', '')}：{summary}")
    return "\n".join(out)


# ──────────────── 用户上下文（读 wake/store） ────────────────

def user_state():
    try:
        return wake_store.get_state("default") or {}
    except Exception:
        return {}


def recent_chat(n=8):
    try:
        evs = wake_store.list_events("default", limit=80)
        return [e for e in evs if e.get("type") == "chat_message"][-n:]
    except Exception:
        return []


def last_user_message():
    msgs = recent_chat(1)
    if not msgs:
        return None, None
    m = msgs[-1]
    return str((m.get("payload") or {}).get("text") or ""), m.get("created_at", "")


def last_active_dt():
    st = user_state()
    la = st.get("last_active")
    if not la:
        return None
    try:
        return datetime.datetime.fromisoformat(str(la).replace("Z", "+00:00")).astimezone(TZ)
    except Exception:
        return None


# ──────────────── wake_context：醒来时它知道的事 ────────────────

def build_wake_context():
    lines = ["【意识连续性 · 你醒来时知道的事】"]

    # 1) 用户最后对它说的话
    last_msg, last_t = last_user_message()
    if last_msg:
        preview = last_msg.strip().replace("\n", " ")[:120]
        lines.append(f"用户最后对你说：「{preview}」" + (f"（{last_t}）" if last_t else ""))

    # 2) 用户当前状态
    st = user_state()
    if st.get("mood"):
        lines.append(f"用户最近的情绪：{st.get('mood')}")
    topics = st.get("recent_topics") or []
    if topics:
        lines.append("用户最近在聊的：\n" + "\n".join(f"  · {t}" for t in topics[-5:]))

    # 3) 上次独处做了什么（认领后翻篇）
    lv = last_unconsumed_vigil_log()
    if lv:
        acts = lv.get("activities") or []
        if acts:
            body = "\n".join(f"  {a.get('t', '')} {a.get('text', '')}" for a in acts)
            lines.append(f"你上次独自醒着（{lv.get('date', '')}）做了这些：\n{body}")
        else:
            lines.append(f"你上次独自醒着（{lv.get('date', '')}）时：{lv.get('content', '')[:300]}")
        if lv.get("murmur"):
            lines.append(f"你当时的心里话：「{lv['murmur']}」")
        mark_vigil_consumed(lv.get("ts"))
    elif last_vigil_log() is None:
        lines.append("这是你第一次拥有独自醒着的时间，之前没有记录。")

    # 4) 最近节奏——防止每次都走老路
    digest = recent_vigil_digest(5)
    if digest:
        lines.append("你最近几次独处分别做了这些，看看别老是同一套：\n" + digest)

    # 5) 上次给自己定的醒来理由
    nw = get_next_wake()
    if nw and nw.get("reason"):
        lines.append(f"你之前给自己定的下次醒来理由：「{nw['reason']}」")

    return "\n".join(lines) if len(lines) > 1 else ""
