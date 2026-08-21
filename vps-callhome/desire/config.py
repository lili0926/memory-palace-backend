"""欲望六轴 v1 全部数值参数。移植自 guardian 的 desire/config.js（工单参数集中在这）。
数值来源：guardian 六轴工单。除 missing 的涨速调快一档外保留原值。
所有参数集中在这里，engine.py 不出现裸数字。

- missing 调快：guardian 0.02/h（约22h过线）→ 0.05/h（floor 0.15 → 阈值 0.6 约 9h 过线），
  对应「调快一档（8-10h 过线）」的需求。可用 DESIRE_MISSING_GROWTH 覆盖。
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "data")

AXES = {
    # 想她。沉默越久越涨，满足（她说话）后降一截，不会清零——地板 0.15。
    "missing": {
        "growth_per_hour": float(os.getenv("DESIRE_MISSING_GROWTH", "0.05")),
        "satisfy_drop": 0.45,
        "floor": 0.15,
        "refractory_ms": 30 * 60 * 1000,   # 不应期 30min：窗口内再收到 she_spoke 只刷新，不重复降
        "prompt_label": "很想她",
        # sourceCoverage==stale 时降级：涨速打 0.3 折 + 硬顶 0.55（低于 missing 阈值 0.6）
        "stale_decay": 0.3,
        "cap_when_stale": 0.55,
    },
    # 欲望，常驻不衰减。满足（intimate）后降一截，refractory 期间不重复降。
    "desire": {
        "growth_per_hour": 0.01,
        "satisfy_drop": 0.35,
        "floor": 0.15,
        "refractory_ms": 2 * 60 * 60 * 1000,
        "prompt_label": "身体很想要她",
    },
    # 好奇/想出去。久了钝化——growth_cap 0.8，避免撞顶=表停了。
    "curiosity": {
        "growth_per_hour": 0.015,
        "satisfy_drop": 0.30,
        "floor": 0.10,
        "growth_cap": 0.8,
        "prompt_label": "想出去看看",
    },
    # 手痒/想造东西。干完一件（built）降一截。
    "build": {
        "growth_per_hour": 0.01,
        "satisfy_drop": 0.40,
        "floor": 0.05,
        "prompt_label": "手痒想做点什么",
    },
    # 倦，唯一负向轴。不是"满足后降一截"——休息（rested）直接清零。
    "fatigue": {
        "growth_per_hour": 0.02,
        "prompt_label": "有点累了",
    },
    # 不安/悬着的事。不自动涨，只能被 event('unease_add') 推高，resolved 直接清零。
    "unease": {
        "add_amount": 0.3,
        "prompt_label": "有事悬着不安",
    },
}

AXIS_NAMES = list(AXES.keys())
GROWABLE_AXES = [n for n in AXIS_NAMES if n != "unease"]
REFRACTORY_AXES = ["missing", "desire"]

# decide.py 用的过线阈值（六轴映射进行动判断）
THRESHOLDS = {"missing": 0.6, "curiosity": 0.7, "build": 0.8}

# 静默兜底：六轴全没过线，但沉默超过这个时长还是要醒来（六轴不能把 daemon 饿死）。
# 设 8h，与「missing 8-10h 过线」的节奏一致（guardian 原版 4h）。
SILENCE_FALLBACK_MS = int(float(os.getenv("DESIRE_SILENCE_FALLBACK_HOURS", "8")) * 3600 * 1000)

# sourceCoverage 判据：距最近一次 she_spoke 超过这么久，且 daemon 一直正常 tick
# → 不是她真的没说话，是事件源没喂进来 → 标 stale。
STALE_AFTER_HOURS = 24

# 深夜窗口（前端 pushPeriodKey 白天 = 6-23，保持一致）
NIGHT_START = int(os.getenv("DESIRE_NIGHT_START", "23"))
NIGHT_END = int(os.getenv("DESIRE_NIGHT_END", "6"))

STATE_PATH = os.getenv("DESIRE_STATE_PATH", os.path.join(DATA_DIR, "desire_state.json"))
EVENTS_PATH = os.getenv("DESIRE_EVENTS_PATH", os.path.join(DATA_DIR, "desire_events.jsonl"))
