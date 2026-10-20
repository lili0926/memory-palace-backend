"""Server-clocked night wakes. The phone supplies a short-lived persona snapshot.

Only authenticated App requests can install a snapshot. No chat transcript or
provider credential is written to logs; the context file is owner-readable.
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
import push
from wake import store

ROOT = Path(os.getenv("NIGHT_AGENT_DIR", "/root/vps-callhome/data"))
CONTEXT = ROOT / "night_agent_context.json"
STATE = ROOT / "night_agent_state.json"
LOCK = threading.Lock()
STARTED = False


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _zone(name):
    try:
        return ZoneInfo(name or "Asia/Shanghai")
    except (KeyError, ValueError):
        return ZoneInfo("Asia/Shanghai")


def _night(now, timezone):
    local = datetime.fromtimestamp(now, _zone(timezone))
    if 9 <= local.hour < 23:
        return None
    start = local.replace(hour=23, minute=0, second=0, microsecond=0)
    if local.hour < 9:
        start -= timedelta(days=1)
    return start.strftime("%Y-%m-%d"), start.timestamp(), (start + timedelta(hours=10)).timestamp()


def _safe_base(base):
    parsed = urlparse(str(base or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("model base must be HTTPS")
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("local model base is not available on VPS")
    return str(base).rstrip("/")


def configure(data):
    with LOCK:
        if not data.get("enabled"):
            CONTEXT.unlink(missing_ok=True)
            return {"ok": True, "enabled": False}
        cfg = data.get("model") or {}
        channel = cfg.get("channel")
        if channel not in ("openai", "claude") or not cfg.get("key") or not cfg.get("model"):
            raise ValueError("model configuration is incomplete")
        base = _safe_base(cfg.get("base"))
        system = str(data.get("system") or "")[:36000]
        if len(system) < 20:
            raise ValueError("persona is missing")
        messages = []
        for row in (data.get("messages") or [])[-18:]:
            if row.get("role") in ("user", "assistant"):
                messages.append({"role": row["role"], "content": str(row.get("content") or "")[:1800]})
        entries = []
        for row in (data.get("entries") or [])[:16]:
            entries.append({"id": str(row.get("id") or "")[:12],
                            "kind": str(row.get("kind") or "")[:20],
                            "title": str(row.get("title") or "")[:80],
                            "body": str(row.get("body") or "")[:900]})
        context = {"enabled": True, "updated": time.time(),
                   "timezone": str(data.get("timezone") or "Asia/Shanghai")[:80],
                   "thread_id": str(data.get("threadId") or "a1")[:40],
                   "name": str(data.get("name") or "他")[:40],
                   "system": system, "messages": messages, "entries": entries,
                   "model": {"channel": channel, "base": base, "key": str(cfg["key"]),
                             "model": str(cfg["model"])[:120]}}
        _write(CONTEXT, context)
        return {"ok": True, "enabled": True, "updated": context["updated"]}


def status():
    context = _read(CONTEXT)
    state = _read(STATE)
    return {"enabled": bool(context.get("enabled") and time.time() - context.get("updated", 0) < 36 * 3600),
            "last_sync": context.get("updated"), "night": state.get("night"),
            "target": state.get("target", 0), "cursor": state.get("cursor", 0),
            "last_result": state.get("last_result", "")}


def _call(context, system, messages):
    cfg = context["model"]
    base = cfg["base"]
    if cfg["channel"] == "claude":
        url = base if base.endswith("/messages") else base + "/messages"
        response = httpx.post(url, headers={"x-api-key": cfg["key"], "anthropic-version": "2023-06-01",
                                             "content-type": "application/json"},
                              json={"model": cfg["model"], "max_tokens": 500, "system": system,
                                    "messages": messages}, timeout=45, follow_redirects=False)
        response.raise_for_status()
        return "".join(x.get("text", "") for x in response.json().get("content", []) if x.get("type") == "text")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    response = httpx.post(url, headers={"Authorization": "Bearer " + cfg["key"]},
                          json={"model": cfg["model"], "max_tokens": 500,
                                "messages": [{"role": "system", "content": system}, *messages]},
                          timeout=45, follow_redirects=False)
    response.raise_for_status()
    return str(response.json()["choices"][0]["message"].get("content") or "")


def _readable_entries(context):
    """Read the two VPS-backed books live; album/weather remain dated phone snapshots."""
    fresh = []
    try:
        import mc_store
        for kind, prefix, title in (("diary", "D", "日记"), ("note", "N", "纸条")):
            for index, row in enumerate(mc_store.list_all(kind, limit=5)):
                fresh.append({"id": prefix + str(index), "kind": title,
                              "title": str(row.get("title") or row.get("date") or title)[:80],
                              "body": str(row.get("content") or "")[:900]})
    except (ImportError, OSError, ValueError):
        fresh = [x for x in context.get("entries", [])
                 if x.get("kind") in ("日记", "纸条")]
    other = [x for x in context.get("entries", [])
             if x.get("kind") not in ("日记", "纸条")]
    return (fresh + other)[:16]


def _decide(context, ordinal, target, now):
    entries = _readable_entries(context)
    menu = "；".join(f'{x["id"]} {x["kind"]}《{x["title"]}》' for x in entries)
    local = datetime.fromtimestamp(now, _zone(context["timezone"])).strftime("%Y-%m-%d %H:%M")
    rule = (f"\n【夜间自主醒来】现在是 {local}，今晚第 {ordinal}/{target} 次醒来。"
            "这不是 Jasmine 的发言。历史里 user 才是她、assistant 是你。"
            "你自己决定沉默或说话，不必凑消息。若沉默只输出 ⟪不说:一句心念⟫。"
            "若说话，最多三条，之间用 ⟪下一条⟫ 分隔。"
            "不要输出其他功能暗号，也不要把旧记录说成正在发生。")
    if menu:
        rule += "\n你可以先翻一项资料：只输出 ⟪翻看:编号⟫；目录：" + menu
    system = context["system"] + rule
    messages = context["messages"] or [{"role": "user", "content": "[还没有聊天记录；这不是 Jasmine 发言]"}]
    raw = _call(context, system, messages).strip()
    found = re.fullmatch(r"⟪翻看:([^⟫]{1,12})⟫", raw)
    if found:
        item = next((x for x in entries if x["id"] == found.group(1)), None)
        detail = ("你刚翻到" + item["kind"] + "《" + item["title"] + "》：" + item["body"]) if item else "那份资料没有找到。"
        provenance = "旧快照" if item and item["kind"] not in ("日记", "纸条") else "资料"
        system += "\n【你刚翻到的资料】" + detail + "\n这是" + provenance + "、不是 Jasmine 新发言。现在只能沉默或直接说话。"
        raw = _call(context, system, messages).strip()
    raw = re.sub(r"<thinking>[\s\S]*?</thinking>", "", raw, flags=re.I).strip()
    if re.search(r"⟪\s*不说", raw):
        return []
    return [re.sub(r"⟪[^⟫]*⟫", "", x).strip()[:1000]
            for x in re.split(r"\s*⟪下一条⟫\s*", raw)[:3] if re.sub(r"⟪[^⟫]*⟫", "", x).strip()]


def run_once(now=None):
    now = time.time() if now is None else now
    with LOCK:
        context = _read(CONTEXT)
        if not context.get("enabled"):
            return "no_context"
        if now - context.get("updated", 0) >= 36 * 3600:
            CONTEXT.unlink(missing_ok=True)
            return "expired"
        night = _night(now, context["timezone"])
        if not night:
            return "day"
        key, start, end = night
        state = _read(STATE)
        if state.get("night") != key:
            target = random.randint(5, 9)
            beginning = max(start, now)
            width = (end - beginning) / target
            slots = [beginning + (i + random.uniform(.35, .65)) * width for i in range(target)]
            state = {"night": key, "target": target, "slots": slots, "cursor": 0}
            _write(STATE, state)
        cursor = state["cursor"]
        if cursor >= state["target"] or now < state["slots"][cursor]:
            return "waiting"
        while cursor + 1 < state["target"] and state["slots"][cursor + 1] <= now:
            cursor += 1
        state["cursor"] = cursor + 1  # Claim before API call; restart cannot double-send.
        state["last_result"] = "claimed"
        _write(STATE, state)
    try:
        lines = _decide(context, cursor + 1, state["target"], now)
        if lines and _night(now, context["timezone"]):
            for line in lines:
                store.enqueue_wake({"user_id": "default", "reason": "night_agent",
                                    "message": line, "channel": "chat", "priority": 7,
                                    "thread_id": context["thread_id"]}, no_dedup=True)
            push.push_to_all(context["name"], lines[0][:120], {"type": "proactive"})
        result = "sent" if lines else "silent"
    except Exception as exc:
        result = "error:" + type(exc).__name__
    with LOCK:
        latest = _read(STATE)
        if latest.get("night") == key and latest.get("cursor") == cursor + 1:
            latest["last_result"] = result
            _write(STATE, latest)
    return result


def start():
    global STARTED
    if STARTED:
        return
    STARTED = True
    def loop():
        while True:
            try:
                run_once()
            except Exception as exc:
                print("[night-agent]", type(exc).__name__)
            time.sleep(60)
    threading.Thread(target=loop, daemon=True, name="night-agent").start()

