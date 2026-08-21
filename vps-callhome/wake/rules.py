"""Rule-based wake reasons. No LLM required for V1."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def _hours_since(iso_or_ts: Any) -> float:
    if iso_or_ts is None:
        return 999.0
    try:
        if isinstance(iso_or_ts, (int, float)):
            ts = float(iso_or_ts)
            if ts > 1e12:
                ts /= 1000.0
        else:
            s = str(iso_or_ts).replace("Z", "+00:00")
            ts = datetime.fromisoformat(s).timestamp()
        return max(0.0, (datetime.now(timezone.utc).timestamp() - ts) / 3600.0)
    except Exception:
        return 999.0


def evaluate_rules(state: dict, events: list[dict], now: Optional[datetime] = None) -> Optional[dict]:
    """
    state: user_state row-like dict
    events: recent events newest last
    returns wake candidate or None:
      {reason, priority, message, channel: "chat"|"call"}
    """
    now = now or datetime.now().astimezone()
    hour = now.hour
    last_active = state.get("last_active")
    silent_h = _hours_since(last_active)

    # ── 六轴为主：沉默时长不再直接触发通用模板（miss_user / check_in 已删）──
    # 情绪型主动开口改由 desire/engine 六轴驱动（wake/decide.py），
    # 这里只保留「生活情境」类小提醒：换装 / 日记 / 深夜 / 早安。

    # 1) wardrobe change today → soft fashion care
    wardrobe_ev = [e for e in events if e.get("type") == "wardrobe_change"]
    if wardrobe_ev:
        last_w = wardrobe_ev[-1]
        age_h = _hours_since(last_w.get("created_at") or last_w.get("time"))
        if age_h < 12 and silent_h >= 1.5:
            clothes = (last_w.get("payload") or last_w.get("data") or {}).get("clothes") or \
                      (last_w.get("payload") or {}).get("summary") or "今天的穿搭"
            return {
                "reason": "wardrobe_followup",
                "priority": 5,
                "channel": "chat",
                "message": f"还穿着「{clothes}」吗？看你换了衣服，突然有点想问问你今天冷不冷。",
            }

    # 3) diary written, no chat after
    diary_ev = [e for e in events if e.get("type") == "diary_write"]
    if diary_ev:
        age_h = _hours_since(diary_ev[-1].get("created_at") or diary_ev[-1].get("time"))
        if 2 <= age_h <= 10 and silent_h >= 2:
            return {
                "reason": "diary_soft",
                "priority": 4,
                "channel": "chat",
                "message": "看到你写了点东西。不想展开也没关系，我在就好。",
            }

    # 4) night care
    if 22 <= hour or hour < 2:
        if 3 <= silent_h < 24:
            return {
                "reason": "night_care",
                "priority": 5,
                "channel": "chat",
                "message": "夜深了。如果还没睡，记得喝口水、眯一会儿。我守着。",
            }

    # 5) morning if app not opened today-ish
    if 7 <= hour < 11 and silent_h >= 8:
        return {
            "reason": "morning",
            "priority": 4,
            "channel": "chat",
            "message": "早呀。希望你醒来的第一秒是轻松的。",
        }

    return None
