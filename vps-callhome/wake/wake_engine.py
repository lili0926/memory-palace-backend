"""Core: desire six-axis → decide → queue; optional AI reflection hook."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Optional

from . import decide, rules, store
from desire import engine as desire_engine
import presence as presence_mod

# optional HTTP call to your chat LLM for nightly reflection
REFLECTION_URL = os.getenv("REFLECTION_URL", "")  # e.g. internal generate endpoint

# 静默兜底用的"上次行动"时间戳（跨重启），写在 data/last_action.json
_LAST_ACTION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "last_action.json"
)


def _read_last_action() -> Optional[str]:
    try:
        with open(_LAST_ACTION_PATH, encoding="utf-8") as f:
            d = json.load(f) or {}
        return d.get("at")
    except Exception:
        return None


def _ensure_last_action() -> None:
    """启动时若从未行动过，先把 last_action 记为"现在"——静默兜底不能因为
    「没有历史」就立刻触发（否则每次重启都当场来一条）。要真正沉默够时长才兜底。"""
    if not os.path.exists(_LAST_ACTION_PATH):
        _write_last_action()


def _write_last_action() -> None:
    try:
        os.makedirs(os.path.dirname(_LAST_ACTION_PATH), exist_ok=True)
        tmp = _LAST_ACTION_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": datetime.now().astimezone().isoformat()}, f, ensure_ascii=False)
        os.replace(tmp, _LAST_ACTION_PATH)
    except Exception:
        pass


def run_once(user_id: str = "default") -> Optional[dict]:
    state = store.get_state(user_id)
    if state.get("dnd"):
        return None
    events = store.list_events(user_id, limit=40)
    # 六轴：先消费 events.jsonl 跨进程事件 + 按经过小时补涨，状态落盘（App 关着也在涨）
    desire = desire_engine.tick()
    # 在场：读 last_active（前端每次 app_open/chat_message 都会更新）
    presence = presence_mod.read_presence()
    decision = decide.decide(
        state, events, desire, presence, state.get("dnd"),
        last_action_iso=_read_last_action(),
    )
    if not decision["should_act"]:
        return None

    if decision["reason"] and decision["reason"].startswith("six_axis"):
        # 人设生成正文：只入队 + 标记 need_persona，正文由前端拉取后生成。
        # 不直接 FCM 推 hint（那是给前端生成的上下文，不是给她看的话）。
        row = store.enqueue_wake({
            "user_id": user_id,
            "reason": decision["reason"],
            "message": decision["hint"],
            "channel": "chat",
            "priority": 6,
            "need_persona": True,
        })
    else:
        # 情境规则（换装/日记/深夜/早安）：模板消息直接可用，照常 push。
        row = store.enqueue_wake({
            "user_id": user_id,
            "reason": decision["reason"],
            "message": decision["template_message"],
            "channel": "chat",
            "priority": 6,
        })
        # CAPACITOR_PUSH_FOR_CLAUDE.md: queue + FCM notification together.
        try:
            import push  # type: ignore

            push.push_wake(row)
        except Exception:
            pass
    _write_last_action()
    return row


# 模块加载时初始化 last_action（防启动即兜底）
_ensure_last_action()


def run_reflection_stub(user_id: str = "default") -> Optional[dict]:
    """
    V2 placeholder. Wire to your LLM:
    POST REFLECTION_URL with today's events summary → {wake, message, reason}
    """
    if not REFLECTION_URL:
        return None
    # left for Claude Code / operator to implement with httpx
    return None


# ── Callhome 升级拨号（escalation）───────────────────────────────────────────
# 参考 gateway-reference/escalation.js：静默≥5h + 本地时 12–23 + 当天未拨 + DND关
# → 生成一句来电理由（模板兜底）。返回 reason 或 None；由 main.py 定时线程调 create_invite。
_ESC_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "escalation_state.json")
_esc_lock = threading.Lock()
ESCALATION_MIN_SILENT_H = float(os.getenv("ESCALATION_MIN_SILENT_H", "5"))
ESCALATION_HOUR_START = int(os.getenv("ESCALATION_HOUR_START", "12"))
ESCALATION_HOUR_END = int(os.getenv("ESCALATION_HOUR_END", "23"))


def _esc_today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def maybe_escalate(user_id: str = "default") -> Optional[str]:
    """升级拨号判定：命中返回来电理由（已写当天状态），否则 None。"""
    state = store.get_state(user_id)
    if state.get("dnd"):
        return None
    last_active = state.get("last_active")
    if not last_active:
        return None
    silent_h = rules._hours_since(last_active)
    if silent_h < ESCALATION_MIN_SILENT_H:
        return None
    hour = datetime.now().astimezone().hour
    if not (ESCALATION_HOUR_START <= hour < ESCALATION_HOUR_END):
        return None
    today = _esc_today()
    with _esc_lock:
        try:
            with open(_ESC_STATE_PATH, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
        if data.get("last_date") == today:
            return None
        try:
            os.makedirs(os.path.dirname(_ESC_STATE_PATH), exist_ok=True)
            with open(_ESC_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"last_date": today, "at": datetime.now().isoformat()}, f, ensure_ascii=False)
        except Exception:
            pass
    hrs = max(5, int(silent_h))
    return f"{hrs} 个小时没有你的消息了，有点想你，打来看看。"
