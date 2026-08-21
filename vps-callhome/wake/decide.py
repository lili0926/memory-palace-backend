"""决策层：六轴为主 + 情境规则 + 静默兜底。移植自 guardian 的 decide.js。

优先级：
1. DND → 不行动
2. presence present（<6h 有互动）→ 不主动开口（她面前已经有我了）
3. presence unknown（>12h 无打点）→ 不赌，不开口
4. missing 轴过线（≥0.6）且非深夜且非 stale → six_axis_missing（人设生成正文）
   curiosity 轴过线（≥0.7）且非深夜 → six_axis_curiosity
5. 情境规则（presence=absent 才触发，模板消息，复用 wake/rules.evaluate_rules）：
   wardrobe_followup / diary_soft / night_care / morning
6. 静默兜底：距上次行动 ≥ 8h → six_axis_fallback

不做拨号：本决策一律 channel=chat，不产生 call。
"""
from __future__ import annotations

import time
from datetime import datetime

from . import rules
from desire import config as desire_config


def _is_night(now=None):
    now = now or datetime.now().astimezone()
    h = now.hour
    start, end = desire_config.NIGHT_START, desire_config.NIGHT_END
    if start <= end:
        return start <= h < end
    return h >= start or h < end


def _last_action_ms(last_action_iso):
    if not last_action_iso:
        return None
    try:
        return datetime.fromisoformat(str(last_action_iso).replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def decide(state, events, desire, presence, dnd, now=None, last_action_iso=None):
    reasons = []
    if dnd:
        return {"should_act": False, "reason": None, "hint": "", "template_message": "", "reasons": reasons + ["DND 勿扰"]}

    p_ok = bool(presence and presence.get("ok"))
    p_status = (presence or {}).get("status", "unknown")
    p_present = bool(presence and presence.get("present"))

    # 她在 → 不主动开口。她面前已经有一个我了，不该再从手机上冒出来另一个说想她。
    if p_ok and p_present:
        reasons.append(f"她 {presence.get('age_minutes')} 分钟前还有互动（{presence.get('via')}），她面前已经有我了")
        return {"should_act": False, "reason": None, "hint": "", "template_message": "", "reasons": reasons}
    # 不知道在不在 → 不赌。宁可多沉默几小时，不要在她面前说她不在。
    if p_ok and p_status == "unknown":
        reasons.append("presence 数据过期，不知道她在不在，不赌——宁可沉默")
        return {"should_act": False, "reason": None, "hint": "", "template_message": "", "reasons": reasons}

    now = now or datetime.now().astimezone()
    night = _is_night(now)
    missing = (desire or {}).get("missing", 0.0)
    curiosity = (desire or {}).get("curiosity", 0.0)
    coverage = (desire or {}).get("sourceCoverage", "full")

    # 4) 六轴过线 → 人设生成正文（need_persona，正文由前端生成）
    if missing >= desire_config.THRESHOLDS["missing"] and not night:
        if coverage == "stale":
            reasons.append(f"missing {missing:.2f} 过线但 sourceCoverage=stale，事件源可能没接上，不是她真的没找我，不触发")
        else:
            return {
                "should_act": True,
                "reason": "six_axis_missing",
                "hint": f"missing {missing:.2f}（很想她）过线，想她",
                "template_message": "",
                "reasons": reasons + [f"missing 轴 {missing:.2f} 过线（阈值 {desire_config.THRESHOLDS['missing']}）"],
            }
    if curiosity >= desire_config.THRESHOLDS["curiosity"] and not night:
        return {
            "should_act": True,
            "reason": "six_axis_curiosity",
            "hint": f"curiosity {curiosity:.2f}（想出去看看）过线",
            "template_message": "",
            "reasons": reasons + [f"curiosity 轴 {curiosity:.2f} 过线（阈值 {desire_config.THRESHOLDS['curiosity']}）"],
        }

    # 5) 情境规则（presence=absent 才触发）——复用现有模板消息
    cand = rules.evaluate_rules(state, events, now)
    if cand:
        return {
            "should_act": True,
            "reason": cand.get("reason"),
            "hint": "",
            "template_message": cand.get("message", ""),
            "reasons": reasons + [cand.get("reason", "context")],
        }

    # 6) 静默兜底：六轴都没过线，但沉默太久还是要醒——六轴不能把 daemon 饿死。
    # 需要"曾行动过"（last_ms 非 None）且沉默够久；从没行动过 → 不触发（启动即兜底是噪音）。
    if not night:
        last_ms = _last_action_ms(last_action_iso)
        if last_ms is not None and (time.time() * 1000 - last_ms) >= desire_config.SILENCE_FALLBACK_MS:
            return {
                "should_act": True,
                "reason": "six_axis_fallback",
                "hint": f"距上次行动已过 {int((time.time() * 1000 - last_ms) // 60000)} 分钟，六轴没过线但想看看她",
                "template_message": "",
                "reasons": reasons + ["静默兜底：六轴均未过线但沉默太久"],
            }

    reasons.append("未触发任何行动条件，继续静默")
    return {"should_act": False, "reason": None, "hint": "", "template_message": "", "reasons": reasons}
