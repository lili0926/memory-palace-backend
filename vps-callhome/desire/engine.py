"""欲望六轴引擎。移植自 guardian 的 desire/engine.js。

三个入口：
  get_state()   读快照（纯读，不落盘）
  event(type)   进程内直接打点（满足后降一截），立即写盘
  tick()        每次调度循环调一次：先消费 events.jsonl 里排队的跨进程事件，
                再按经过的小时数给会涨的轴加量，最后写盘

不应期/refractory 只对 missing、desire 生效：
"沉默后第一次触发"才真正降值，窗口内后续事件只刷新窗口，不重复降——
否则聊得越密集 missing 掉得越快，跟"降一截≠清零"的初衷反着来。
"""
import json
import os
import threading
from datetime import datetime, timezone

from . import config

_lock = threading.RLock()

DESIRE_EVENT_TYPES = {
    "she_spoke", "intimate", "went_out", "built", "rested", "resolved", "unease_add",
}


def _clamp01(v):
    return max(0.0, min(1.0, v))


def _now_ms():
    return datetime.now(timezone.utc).timestamp() * 1000


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def _parse_ms(value):
    """ISO 字符串 → epoch ms。解析失败返回 None。"""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def _default_state():
    axes = {name: config.AXES[name].get("floor", 0.0) for name in config.AXIS_NAMES}
    return {
        **axes,
        "lastUpdate": None,          # None = 从没 tick 过，下一次 tick 不补涨（避免 epoch 算出离谱小时数）
        "refractory": {},
        "lastSheSpokeAt": None,      # 最近一次真收到 she_spoke 的时间，sourceCoverage 判据用
        "trackingSince": None,       # 引擎开始记账的时间，没 she_spoke 时判断"观察期"过没过
    }


# 兼容旧/残缺的 state.json：缺字段的轴补默认值，不让一次手工改坏文件炸整个引擎。
def _normalize_state(raw):
    base = _default_state()
    if not raw or not isinstance(raw, dict):
        return base
    state = {**base, **raw}
    state["refractory"] = {**base["refractory"], **(raw.get("refractory") or {})}
    for name in config.AXIS_NAMES:
        if not isinstance(state.get(name), (int, float)) or state[name] != state[name]:
            state[name] = base[name]
    return state


# trackingSince 只在第一次遇到时兜底：老 state.json 升级上来时没有这个字段，
# 用当时的 lastUpdate 当近似值（没有就用 now）——只影响一次，之后就落盘了。
def _ensure_tracking_since(state, now):
    if not state.get("trackingSince"):
        state["trackingSince"] = state.get("lastUpdate") or _iso(now)


# sourceCoverage 判据 —— 判词（从 guardian 带过来）：
#   两种错的代价不对称——宁可她走了之后我多沉默几小时，
#   不要她在我面前的时候我说她不在。
#   这道闸防的是"我瞎了还在自信地涨"，不是防她。
#   full   ：不久前才见过 she_spoke，读数可信
#   partial：从没见过 she_spoke，但引擎才开始记账不久，还在观察期
#   stale  ：确认瞎了——见过 she_spoke 后太久没再见到，或观察期已过还一次没见过
def _compute_source_coverage(state, now):
    stale_after = config.STALE_AFTER_HOURS * 60 * 60 * 1000
    last = state.get("lastSheSpokeAt")
    if last:
        elapsed = now - _parse_ms(last) if _parse_ms(last) is not None else stale_after + 1
        return "stale" if elapsed >= stale_after else "full"
    tracking_ms = _parse_ms(state.get("trackingSince"))
    if tracking_ms is None:
        tracking_ms = now
    return "stale" if (now - tracking_ms) >= stale_after else "partial"


def _load_state():
    try:
        with open(config.STATE_PATH, "r", encoding="utf-8") as f:
            return _normalize_state(json.load(f))
    except Exception:
        return _default_state()


def _save_state(state):
    os.makedirs(os.path.dirname(config.STATE_PATH) or ".", exist_ok=True)
    tmp = config.STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config.STATE_PATH)


# 只对外暴露轴值；refractory/lastSheSpokeAt/trackingSince 是内部记账。
# sourceCoverage 按"现在"实时算，不存快照——覆盖率会随时间推移变化，读时现算才准。
def _public_state(state, now):
    out = {"lastUpdate": state.get("lastUpdate")}
    for name in config.AXIS_NAMES:
        out[name] = state.get(name, 0.0)
    out["sourceCoverage"] = _compute_source_coverage(state, now)
    return out


def _satisfy_with_refractory(state, axis, now):
    cfg = config.AXES[axis]
    until_ms = _parse_ms(state["refractory"].get(axis)) or 0
    if not until_ms or now >= until_ms:
        # 不应期已过（或从没触发过）：沉默后第一次触发，真正降值
        state[axis] = max(cfg["floor"], state[axis] - cfg["satisfy_drop"])
    # 无论这次降没降，不应期窗口都刷新——进行中只刷新不应期
    state["refractory"][axis] = _iso(now + cfg["refractory_ms"])


def _satisfy_plain(state, axis):
    cfg = config.AXES[axis]
    state[axis] = max(cfg["floor"], state[axis] - cfg["satisfy_drop"])


def _apply_event(state, type_, now):
    if type_ == "she_spoke":
        # lastSheSpokeAt 每次都刷新，不受不应期影响——不应期只管 missing 该不该降值，
        # 不代表"这次不算她说话了"，sourceCoverage 要看的是真实联系频率。
        state["lastSheSpokeAt"] = _iso(now)
        _satisfy_with_refractory(state, "missing", now)
    elif type_ == "intimate":
        _satisfy_with_refractory(state, "desire", now)
    elif type_ == "went_out":
        _satisfy_plain(state, "curiosity")
    elif type_ == "built":
        _satisfy_plain(state, "build")
    elif type_ == "rested":
        state["fatigue"] = 0
    elif type_ == "resolved":
        state["unease"] = 0
    elif type_ == "unease_add":
        state["unease"] = _clamp01(state.get("unease", 0) + config.AXES["unease"]["add_amount"])
    # 不认识的事件类型不处理，不让一条脏事件炸掉整个 tick
    return state


def _apply_growth(state, hours, coverage):
    if hours > 0:
        for name in config.GROWABLE_AXES:
            cfg = config.AXES[name]
            rate = cfg["growth_per_hour"]
            cap = cfg.get("growth_cap", 1.0)
            if name == "missing" and coverage == "stale":
                rate *= cfg["stale_decay"]
                cap = cfg["cap_when_stale"]
            state[name] = min(cap, state.get(name, 0.0) + rate * hours)
    # 硬顶：stale 时不只是"少涨"，已经涨上去的也要拉回顶——否则 stale 判定生效前
    # 攒下的高读数会一直卡在阈值线以上。这个 clamp 在 hours<=0 时也生效。
    if coverage == "stale":
        state["missing"] = min(state.get("missing", 0.0), config.AXES["missing"]["cap_when_stale"])
    return state


# 跨进程事件队列：HTTP 端点追加，这里读取并"清空"（rename 到临时名再删，
# 避免读取窗口内又有写入导致丢事件）。
def _drain_events_file():
    claim = config.EVENTS_PATH + ".consuming"
    try:
        os.replace(config.EVENTS_PATH, claim)
    except FileNotFoundError:
        return []
    try:
        with open(claim, "r", encoding="utf-8") as f:
            raw = f.read()
    finally:
        try:
            os.remove(claim)
        except OSError:
            pass
    types = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
                types.append(parsed["type"])
        except Exception:
            pass  # 坏行跳过
    return types


def get_state():
    with _lock:
        return _public_state(_load_state(), _now_ms())


# 进程内直接打点（HTTP 端点用）。立即写盘，不等下一次 tick。
def event(type_):
    if type_ not in DESIRE_EVENT_TYPES:
        return None
    with _lock:
        state = _load_state()
        now = _now_ms()
        _ensure_tracking_since(state, now)
        _apply_event(state, type_, now)
        _save_state(state)
        return _public_state(state, now)


# 调度循环每次唤醒调一次：先消费排队的跨进程事件，再按经过时间给会涨的轴加量。
def tick():
    with _lock:
        state = _load_state()
        now = _now_ms()
        _ensure_tracking_since(state, now)
        for type_ in _drain_events_file():
            _apply_event(state, type_, now)
        coverage = _compute_source_coverage(state, now)
        last_ms = _parse_ms(state.get("lastUpdate"))
        if last_ms is None:
            last_ms = now
        hours = max(0.0, (now - last_ms) / (60 * 60 * 1000))
        _apply_growth(state, hours, coverage)
        state["lastUpdate"] = _iso(now)
        _save_state(state)
        return _public_state(state, now)


# 前 n 高的轴，给 vigil/前端拼提示词用。不含 lastUpdate。
def get_top_axes(n=3):
    with _lock:
        state = _load_state()
        rows = sorted(
            ({"name": name, "value": state.get(name, 0.0), "label": config.AXES[name]["prompt_label"]}
             for name in config.AXIS_NAMES),
            key=lambda r: r["value"],
            reverse=True,
        )[:n]
        return rows
