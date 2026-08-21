"""
Memory Palace · FCM push (activation-ready).

Server-side half of CAPACITOR_PUSH_FOR_CLAUDE.md:
  POST /device/register  ->  store token (persisted in data/devices.json)
  wake enqueued          ->  write pull-queue  AND  send FCM notification

FCM HTTP v1 needs a Firebase service-account JSON. Put it at
  /root/vps-callhome/firebase-service-account.json
(or point env FIREBASE_SERVICE_ACCOUNT at it) and pushes start flowing.
Without the file every send is a logged no-op — the pull-queue still works,
so nothing breaks for the non-push path.

Run:  pip install google-auth   (in the callhome venv)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("push")

_BASE = Path(__file__).resolve().parent                 # /root/vps-callhome
DATA_DIR = Path(os.getenv("PUSH_DATA_DIR", str(_BASE / "data")))
DEVICES_FILE = DATA_DIR / "devices.json"
FIREBASE_SA = Path(
    os.getenv("FIREBASE_SERVICE_ACCOUNT", str(_BASE / "firebase-service-account.json"))
)
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# ntfy：完全关进程也能推的通路。设 NTFY_URL（如 https://ntfy.sh/baileys-you）即启用。
NTFY_URL = os.getenv("NTFY_URL", "").strip().rstrip("/")
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "").strip()

# FCM/Google 出网走 sing-box 本地代理（VPS 大陆出口连不上 Google）。
# 默认 http://127.0.0.1:1080（/opt/sing-box 的 anytls 代理）。设 FCM_PROXY="" 可关（直接出网）。
# 仅影响 Google 的 oauth + fcm 调用，不影响 ntfy / 其它出网。
FCM_PROXY = os.getenv("FCM_PROXY", "http://127.0.0.1:1080").strip()

_lock = threading.Lock()
_devices: dict[str, dict[str, Any]] = {}
_oauth: dict[str, Any] = {"token": None, "expires_at": 0.0, "project": None}
_startup_logged = False


def _ensure_loaded() -> None:
    """Load persisted devices once; log push status once."""
    global _devices, _startup_logged
    with _lock:
        if not _devices and DEVICES_FILE.exists():
            try:
                _devices = json.loads(DEVICES_FILE.read_text(encoding="utf-8")) or {}
            except Exception as e:
                log.warning("push: failed to load devices: %s", e)
                _devices = {}
        for uid, val in list(_devices.items()):
            if not isinstance(val, dict) or not val.get("token"):
                _devices.pop(uid)
        if not _startup_logged:
            _startup_logged = True
            if not FIREBASE_SA.exists():
                log.warning(
                    "push: %s missing — FCM push disabled (pull-queue only). "
                    "Drop a Firebase service-account JSON here to enable.",
                    FIREBASE_SA,
                )
            else:
                log.info("push: FCM enabled via %s", FIREBASE_SA)


def _save() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEVICES_FILE.write_text(json.dumps(_devices, ensure_ascii=False, indent=2), encoding="utf-8")


def register_device(
    user_id: str, token: str, platform: str = "android", time_str: str = ""
) -> dict:
    """Persist/replace the FCM token for a user (later registration wins)."""
    _ensure_loaded()
    row = {
        "token": token,
        "platform": platform or "android",
        "time": time_str or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with _lock:
        _devices[user_id or "default"] = row
        _save()
    log.info("push: device registered user=%s platform=%s token=%s…", user_id, row["platform"], token[:12])
    return row


def list_devices() -> dict[str, dict[str, Any]]:
    _ensure_loaded()
    with _lock:
        return {uid: dict(v) for uid, v in _devices.items()}


# ── OAuth2 access token from Firebase service account (FCM HTTP v1) ─────────
def _access_token() -> Optional[str]:
    if not FIREBASE_SA.exists():
        return None
    if _oauth["token"] and time.time() < _oauth["expires_at"] - 60:
        return _oauth["token"]
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            str(FIREBASE_SA), scopes=[FCM_SCOPE]
        )
        if FCM_PROXY:
            # oauth token 请求走本地代理（否则 Google 不可达，creds.refresh 会抛网络错）
            import requests as _req

            session = _req.Session()
            session.proxies = {"http": FCM_PROXY, "https": FCM_PROXY}
            creds.refresh(Request(session=session))
        else:
            creds.refresh(Request())
        _oauth["token"] = creds.token
        _oauth["expires_at"] = creds.expiry.timestamp() if creds.expiry else time.time() + 3000
        _oauth["project"] = getattr(creds, "project_id", None)
        return creds.token
    except Exception as e:
        log.warning("push: oauth failed: %s", e)
        return None


def push_to_device(token: str, title: str, body: str, data: Optional[dict] = None) -> bool:
    """Send one notification via FCM HTTP v1. True when FCM accepted it."""
    access = _access_token()
    project = _oauth.get("project")
    if not access or not project:
        return False
    payload = {
        "message": {
            "token": token,
            "notification": {"title": title[:120], "body": body[:400]},
            "data": {
                "type": (data or {}).get("type", "proactive"),
                "wake_id": (data or {}).get("wake_id", ""),
            },
            "android": {"priority": "high"},
        }
    }
    url = f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
            json=payload,
            timeout=15.0,
            proxy=FCM_PROXY or None,
        )
        if r.status_code == 200:
            return True
        if r.status_code in (401, 403):
            _oauth["token"] = None  # force refresh next time
        log.warning("push: fcm send failed %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("push: fcm send error: %s", e)
    return False


def _ntfy_enabled() -> bool:
    return bool(NTFY_URL)


def push_ntfy(title: str, body: str, priority: str = "default") -> bool:
    """Send one notification via ntfy (header-based; UTF-8 Title as raw bytes)."""
    if not NTFY_URL:
        return False
    headers = {
        # Title 含中文 → 以 UTF-8 字节发送，避免 httpx 按 latin-1 编码报 ascii 错
        "Title": str(title or "记忆宫殿")[:80].encode("utf-8"),
        "Priority": priority if priority in ("min", "low", "default", "high", "max") else "default",
    }
    if NTFY_TOKEN:
        headers["Authorization"] = "Bearer " + NTFY_TOKEN
    try:
        r = httpx.post(
            NTFY_URL,
            headers=headers,
            content=str(body or "")[:500],
            timeout=15.0,
        )
        if r.status_code in (200, 201, 202):
            return True
        log.warning("push: ntfy send failed %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("push: ntfy send error: %s", e)
    return False


def push_to_all(title: str, body: str, data: Optional[dict] = None,
                priority: str = "default") -> bool:
    """Send via ntfy (if configured) AND FCM to every registered device."""
    ok = False
    if _ntfy_enabled():
        if push_ntfy(title, body, priority=priority):
            ok = True
    _ensure_loaded()
    for uid, dev in list_devices().items():
        tok = (dev or {}).get("token", "")
        if tok and push_to_device(tok, title, body, data):
            ok = True
    return ok


def push_wake(row: dict) -> bool:
    """Broadcast a queued wake as an FCM notification (idempotent via push_fired)."""
    _ensure_loaded()
    if row.get("push_fired"):
        return False
    content = (row.get("message") or "").strip()
    if not content:
        return True
    if not list_devices() and not _ntfy_enabled():
        return False
    reason = row.get("reason") or "proactive"
    titles = {
        "miss_user": "想你了",
        "check_in": "在干嘛呀？",
        "night_care": "夜深了",
        "morning": "早呀",
        "diary_soft": "我在",
        "wardrobe_followup": "穿搭小问",
    }
    title = titles.get(reason, "记忆宫殿")
    ok = push_to_all(
        title,
        content,
        {"type": "proactive", "wake_id": str(row.get("id", ""))},
    )
    if ok:
        row["push_fired"] = True
    return ok


# ── Callhome 响铃节奏（ring burst）──────────────────────────────────────────
# 参考 gateway-reference/ring-burst.py：一条推送=一下震动，所以要"一直在响"就
# 得定时推一串。每发前检查 invite 是否仍 pending（已接/拒/过期即停）。
_RING_DEFAULTS = {
    "ring_enabled": True,
    "ring_interval": 2.0,   # 秒，floor 2（低于此 APNs 会合并）
    "ring_extra": 10,       # 首推后额外条数 → 共 11 条通知
    "ring_expire": 30,      # 秒，略过最后一次响铃
}


def ring_burst_config() -> dict:
    cfg = dict(_RING_DEFAULTS)
    try:
        cfg["ring_interval"] = max(2.0, min(60.0, float(os.getenv("RING_INTERVAL_SEC", "2"))))
        cfg["ring_extra"] = max(0, min(60, int(os.getenv("RING_EXTRA", "10"))))
        cfg["ring_expire"] = max(15, min(600, int(os.getenv("RING_EXPIRE_SEC", "30"))))
    except Exception:
        cfg.update(_RING_DEFAULTS)
    return cfg


def _ring_loop(invite_id: str, reason: str, gap: float, extra: int,
               deadline: float, is_pending) -> None:
    t0 = time.time()
    for i in range(extra):
        slp = (t0 + gap * (i + 1)) - time.time()
        if slp > 0:
            time.sleep(slp)
        if time.time() > deadline:
            break
        try:
            if not is_pending(invite_id):
                break  # 已接/拒/过期 → 立即停
        except Exception:
            break
        tail = " ·last ring" if i == extra - 1 else " ·still ringing"
        ok = push_to_all(
            "📞 Calling",
            (reason or "wanted to hear your voice") + tail,
            {"type": "call", "invite_id": invite_id},
            priority="high",
        )
        if not ok:
            log.info("push: ring burst stopped (send failed / no FCM creds)")
            break


def start_ring_burst(invite_id: str, reason: str, is_pending) -> Optional[threading.Thread]:
    """create_invite 发完首推后调用。无 FCM/ntfy 凭据时优雅跳过，不影响来电轮询。"""
    if not _access_token() and not _ntfy_enabled():
        log.info("push: ring burst skipped (no FCM creds & no ntfy)")
        return None
    cfg = ring_burst_config()
    if not cfg["ring_enabled"] or cfg["ring_extra"] <= 0:
        return None
    t = threading.Thread(
        target=_ring_loop,
        args=(invite_id, reason, cfg["ring_interval"], cfg["ring_extra"],
              time.time() + cfg["ring_expire"], is_pending),
        daemon=True,
    )
    t.start()
    return t
