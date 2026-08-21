"""
Memory Palace · Call + Wake API
STT / MiniMax TTS proxy / call invite / event+wake engine / FCM push
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

# allow `wake` / `push` packages next to app/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wake import store as wake_store
from wake.scheduler import start_scheduler
from wake.wake_engine import maybe_escalate, run_once
import push  # FCM device registry
from vigil.poll import start as start_vigil
from desire import engine as desire_engine  # 欲望六轴
import presence as presence_mod  # 在场感知（present/absent/unknown）

# 启动时加载 /root/vps-callhome/.env（TTS/STT Key、网关 Token 等环境变量）
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_GROUP_ID = os.getenv("MINIMAX_GROUP_ID", "")
MINIMAX_ENDPOINT = os.getenv(
    "MINIMAX_ENDPOINT", "https://api.minimaxi.chat/v1/t2a_v2"
)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
INVITE_TTL_SEC = int(os.getenv("INVITE_TTL_SEC", "30"))

app = FastAPI(title="Memory Palace Call+Wake API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_INVITE: dict[str, Any] = {}
_DND = False
_whisper = None


def require_auth(x_auth_token: Optional[str] = Header(default=None, alias="X-Auth-Token")):
    if not AUTH_TOKEN:
        return
    if x_auth_token != AUTH_TOKEN:
        raise HTTPException(401, "unauthorized")


@app.on_event("startup")
def _startup():
    push._ensure_loaded()  # log push status early
    start_scheduler()
    start_vigil()  # vigil 自主唤醒（独处意识循环）
    # callhome 升级拨号：静默太久 → 主动来电（一天最多一次，见 wake_engine.maybe_escalate）
    def _esc_loop():
        while True:
            try:
                reason = maybe_escalate("default")
                if reason:
                    create_invite(InviteIn(reason=reason, force=False))
            except Exception as e:
                print("[escalation] error:", e)
            time.sleep(max(120, int(os.getenv("ESCALATION_INTERVAL_SEC", "300"))))

    t = threading.Thread(target=_esc_loop, name="callhome-escalation", daemon=True)
    t.start()


@app.get("/health")
def health():
    return {"ok": True, "ts": time.time(), "wake": True}


# ─── STT ─────────────────────────────────────────────────────────────────────
def get_whisper():
    global _whisper
    if _whisper is not None:
        return _whisper
    try:
        from faster_whisper import WhisperModel  # type: ignore

        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        return _whisper
    except Exception as e:
        raise HTTPException(
            501,
            f"STT unavailable: install faster-whisper or plug SenseVoice. ({e})",
        )


def transcribe_audio(path: str) -> dict:
    model = get_whisper()
    segments, info = model.transcribe(path, language="zh", beam_size=1)
    text = "".join(seg.text for seg in segments).strip()
    return {"text": text, "emotion": "", "tone": ""}


@app.post("/stt")
async def stt(
    file: Optional[UploadFile] = File(default=None),
    audio: Optional[UploadFile] = File(default=None),
    _: None = Depends(require_auth),
):
    up = file or audio
    if not up:
        raise HTTPException(400, "missing file/audio")
    suffix = os.path.splitext(up.filename or "utt.webm")[1] or ".webm"
    tmp = f"/tmp/stt_{uuid.uuid4().hex}{suffix}"
    data = await up.read()
    if len(data) < 100:
        raise HTTPException(400, "audio too short")
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        result = transcribe_audio(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return result


# ─── MiniMax TTS proxy ───────────────────────────────────────────────────────
class TtsBody(BaseModel):
    model: str = "speech-2.6-turbo"
    text: str = Field(..., min_length=1)
    stream: bool = False
    language_boost: str = "Chinese"
    output_format: str = "hex"
    voice_setting: dict = Field(
        default_factory=lambda: {
            "voice_id": "female-shaonv",
            "speed": 1.0,
            "vol": 1.0,
            "pitch": 0,
        }
    )
    audio_setting: dict = Field(
        default_factory=lambda: {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        }
    )
    api_key: str = ""
    group_id: str = ""
    endpoint: str = ""


@app.post("/tts/minimax")
async def tts_minimax(body: TtsBody, _: None = Depends(require_auth)):
    key = body.api_key or MINIMAX_API_KEY
    if not key:
        raise HTTPException(400, "MINIMAX_API_KEY not configured")
    endpoint = (body.endpoint or MINIMAX_ENDPOINT).rstrip("/")
    group = body.group_id or MINIMAX_GROUP_ID
    if group:
        sep = "&" if "?" in endpoint else "?"
        endpoint = f"{endpoint}{sep}GroupId={group}"
    payload = {
        "model": body.model,
        "text": body.text[:5000],
        "stream": False,
        "language_boost": body.language_boost,
        "output_format": body.output_format or "hex",
        "voice_setting": body.voice_setting,
        "audio_setting": body.audio_setting,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return r.json()


# ─── Call invite ─────────────────────────────────────────────────────────────
class InviteIn(BaseModel):
    reason: str = "想打给你"
    force: bool = False


@app.post("/api/call/invite")
def create_invite(body: InviteIn, _: None = Depends(require_auth)):
    global _INVITE
    if (_DND or wake_store.get_state().get("dnd")) and not body.force:
        return {"ok": False, "dnd": True}
    now = time.time()
    inv = {
        "id": uuid.uuid4().hex,
        "reason": body.reason[:80],
        "status": "pending",
        "expires_at": now + INVITE_TTL_SEC,
        "created_at": now,
    }
    _INVITE = inv
    # callhome 响铃：先发首推，再后台一串"still ringing"（pending 消失即停）
    try:
        push.push_to_all("📞 Calling", body.reason[:120] + " ·still ringing",
                         {"type": "call", "invite_id": inv["id"]})

        def _pending(iid):
            cur = _INVITE
            return bool(cur and cur.get("id") == iid and cur.get("status") == "pending"
                        and time.time() < float(cur.get("expires_at", 0)))

        push.start_ring_burst(inv["id"], body.reason, _pending)
    except Exception as e:
        print("[ring] error:", e)
    return {"ok": True, "invite": inv}


@app.get("/api/call/invite")
def get_invite(_: None = Depends(require_auth)):
    global _INVITE
    inv = _INVITE
    if not inv or inv.get("status") != "pending":
        return {"invite": None}
    if time.time() > float(inv.get("expires_at", 0)):
        inv["status"] = "expired"
        _INVITE = inv
        # callhome 未接留言：过期那一次把留言写进拉取队列，前端拉到即进聊天
        if not inv.get("voicemail_done"):
            inv["voicemail_done"] = True
            reason = inv.get("reason") or "想打给你"
            try:
                wake_store.enqueue_wake({
                    "user_id": "default",
                    "reason": "voicemail",
                    "message": f"📞 未接来电\n留言：没接到你。{reason}——不急，回来跟我说一声。",
                    "channel": "chat",
                    "priority": 6,
                }, no_dedup=True)
            except Exception as e:
                print("[voicemail] error:", e)
        return {"invite": None, "expired": True, "reason": inv.get("reason")}
    return {"invite": inv}


class AnswerIn(BaseModel):
    id: str
    action: str
    note: str = ""


@app.post("/api/call/answer")
def answer_invite(body: AnswerIn, _: None = Depends(require_auth)):
    global _INVITE
    inv = _INVITE
    if not inv or inv.get("id") != body.id:
        return {"ok": False, "error": "not_found"}
    inv["status"] = "accepted" if body.action == "accept" else "declined"
    inv["note"] = body.note[:60]
    inv["answered_at"] = time.time()
    _INVITE = inv
    return {"ok": True, "invite": inv}


# ─── Wake: events + pull ─────────────────────────────────────────────────────
class EventIn(BaseModel):
    type: str
    time: Optional[str] = None
    data: Optional[dict] = None
    user_id: str = "default"


@app.post("/event")
@app.post("/api/event")
def post_event(body: EventIn, _: None = Depends(require_auth)):
    """App posts behavioral events for Wake Engine."""
    # 六轴事件：she_spoke / intimate / went_out / built / rested / resolved / unease_add
    # 直接喂给欲望引擎（同时也会落一条行为事件进 store，供 rules 情境规则用）。
    if body.type in desire_engine.DESIRE_EVENT_TYPES:
        try:
            desire_engine.event(body.type)
        except Exception as e:
            print("[desire] event error:", e)
    elif body.type == "chat_message":
        # 她说话了 → missing 轴被满足（降一截）；lastSheSpokeAt 刷新（sourceCoverage 判据）
        try:
            desire_engine.event("she_spoke")
        except Exception as e:
            print("[desire] she_spoke error:", e)

    allowed = {
        "app_open", "chat_message", "wardrobe_change", "diary_write",
        "room_change", "music_play", "sleep_time", "cabinet_change",
        "duty_done", "call_end",
    }
    if body.type not in allowed and not body.type.startswith("custom_"):
        # still accept, but mark
        pass
    ev = wake_store.add_event(body.user_id, body.type, body.data or {}, body.time)
    # opportunistic rule check on high-signal events
    if body.type in ("wardrobe_change", "diary_write", "app_open"):
        try:
            run_once(body.user_id)
        except Exception:
            pass
    return {"ok": True, "event": ev}


@app.get("/api/wake/pull")
@app.get("/proactive/pull")
def wake_pull(user_id: str = "default", _: None = Depends(require_auth)):
    """Frontend polls; maps to existing proactive/pull shape too."""
    items = wake_store.pull_wakes(user_id, limit=5)
    messages = []
    for w in items:
        messages.append({
            "id": w["id"],
            "content": w.get("message") or "",
            "text": w.get("message") or "",
            "reason": w.get("reason"),
            "channel": w.get("channel") or "chat",
            "priority": w.get("priority"),
            "threadId": w.get("thread_id") or None,
            "needPersona": bool(w.get("need_persona")),
        })
        # escalate to call invite
        if w.get("channel") == "call" and w.get("message"):
            create_invite(InviteIn(reason=w["message"][:80], force=False))
    return {"messages": messages, "data": messages}


class BgPushIn(BaseModel):
    content: str = ""
    threadId: str = "a1"
    reason: str = "bg"


@app.post("/proactive/push_bg")
def push_bg(body: BgPushIn, _: None = Depends(require_auth)):
    """后台生成完成的回复：入 pull 队列（带 threadId），前端拉到即落回原线程。"""
    text = (body.content or "").strip()
    if not text:
        return {"ok": False, "error": "empty_content"}
    row = wake_store.enqueue_wake({
        "user_id": "default",
        "reason": body.reason or "bg",
        "message": text,
        "channel": "chat",
        "priority": 6,
        "thread_id": body.threadId or "a1",
    }, no_dedup=True)
    return {"ok": True, "id": row["id"]}


@app.post("/api/wake/run")
def wake_run_now(user_id: str = "default", _: None = Depends(require_auth)):
    row = run_once(user_id)
    return {"ok": True, "wake": row}


@app.get("/api/wake/debug")
def wake_debug(_: None = Depends(require_auth)):
    return {
        "state": wake_store.get_state(),
        "events": wake_store.list_events(limit=20),
        "wakes": wake_store.list_wakes(20),
        "devices": push.list_devices(),
    }


# ─── Desire six-axis API（欲望六轴）────────────────────────────────────────
class DesireEventIn(BaseModel):
    type: str


@app.post("/api/desire/event")
def desire_event(body: DesireEventIn, _: None = Depends(require_auth)):
    """直接喂六轴事件：she_spoke / intimate / went_out / built / rested / resolved / unease_add。"""
    st = desire_engine.event(body.type)
    if st is None:
        raise HTTPException(400, f"unknown desire event type: {body.type}")
    return {"ok": True, "state": st}


@app.get("/api/desire/state")
def desire_state(_: None = Depends(require_auth)):
    """六轴当前读数 + 在场。前端身体状况页展示用。"""
    st = desire_engine.get_state()
    top = desire_engine.get_top_axes(3)
    presence = presence_mod.read_presence()
    return {
        "axes": st,
        "top3": top,
        "sourceCoverage": st.get("sourceCoverage"),
        "lastUpdate": st.get("lastUpdate"),
        "presence": presence,
    }


class DesireDebugIn(BaseModel):
    hours_ago: float = 9.0


@app.post("/api/desire/debug")
def desire_debug(body: DesireDebugIn, _: None = Depends(require_auth)):
    """测试用：把 state.json 的 lastUpdate 回填 hours_ago 小时前，再 tick 一次补涨。"""
    import datetime as _dt
    state_path = __import__("desire.config", fromlist=["config"]).STATE_PATH
    import json as _json
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            st = _json.load(f)
    except Exception:
        st = {}
    st["lastUpdate"] = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=body.hours_ago)).isoformat()
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path)
    st = desire_engine.tick()
    return {"ok": True, "state": st}


# ─── Device registry + FCM push (CAPACITOR_PUSH_FOR_CLAUDE.md) ───────────────
class DeviceIn(BaseModel):
    token: str
    platform: str = "android"
    user_id: str = "default"
    time: Optional[str] = None


@app.post("/device/register")
def device_register(body: DeviceIn, _: None = Depends(require_auth)):
    """App posts its FCM token after PushNotifications.register()."""
    tok = (body.token or "").strip()
    if len(tok) < 16:
        raise HTTPException(400, "invalid token")
    row = push.register_device(body.user_id, tok, body.platform, body.time)
    return {"ok": True, "device": row}


@app.get("/device/list")
def device_list(_: None = Depends(require_auth)):
    return {"ok": True, "devices": push.list_devices()}


@app.post("/device/test_push")
def device_test_push(_: None = Depends(require_auth)):
    """Send a test notification to every registered device (acceptance checklist)."""
    ok = push.push_to_all("记忆宫殿", "测试推送：如果你看到这条，说明通知链路已经打通。")
    return {"ok": ok, "registered": len(push.list_devices())}


class FlagIn(BaseModel):
    dnd: Optional[bool] = None


@app.post("/api/flags")
def flags(body: FlagIn, _: None = Depends(require_auth)):
    global _DND
    if body.dnd is not None:
        _DND = bool(body.dnd)
        wake_store.set_state(dnd=_DND)
    return {"ok": True, "dnd": _DND}


@app.get("/api/flags")
def get_flags(_: None = Depends(require_auth)):
    return {"dnd": _DND or wake_store.get_state().get("dnd")}


# ── MCP 反代（前端 MCP 大厅跨域转发用）─────────────────────────────────────
@app.post("/mcp/proxy")
async def mcp_proxy(request: Request):
    """接收 {proxy}/mcp/proxy?target=<MCP_URL>，把 JSON-RPC body 原样 POST 到 target 并回传。

    前端 mcpEffectiveUrl: proxy + "/mcp/proxy?target=" + urlencode(u)。公开端点（无 auth），
    供 MCP 服务器跨域转发；注意 SSRF——个人 VPS 使用可接受。
    """
    target = request.query_params.get("target", "")
    if not target:
        raise HTTPException(400, "missing target")
    body = await request.body()
    hdrs = {
        "Content-Type": request.headers.get("Content-Type", "application/json"),
        "Accept": request.headers.get("Accept", "application/json, text/event-stream"),
    }
    sid = request.headers.get("Mcp-Session-Id")
    if sid:
        hdrs["Mcp-Session-Id"] = sid
    # 透传 Bearer 授权（部分 MCP 服务器需要 token，如 galatea）
    auth = request.headers.get("Authorization")
    if auth:
        hdrs["Authorization"] = auth
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            resp = await client.post(target, content=body, headers=hdrs)
    except httpx.HTTPError as e:
        raise HTTPException(502, "proxy error: " + str(e))
    out_hdrs = {}
    if resp.headers.get("Mcp-Session-Id"):
        out_hdrs["Mcp-Session-Id"] = resp.headers["Mcp-Session-Id"]
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers=out_hdrs,
    )

# ═══ 小纸条 / 机日记 / 信箱（Machine Content）══════════════════════════════
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
import mc_store as _mc  # noqa: E402



class DeleteIn(BaseModel):
    id: str

class NoteIn(BaseModel):
    content: str
    author: str = "user"


class DiaryIn(BaseModel):
    title: str = ""
    content: str
    visibility: str = "visible"  # visible | private
    locked: bool = False
    author: str = "ai"


class LetterIn(BaseModel):
    content: str
    scheduledAt: Optional[str] = None
    author: str = "user"


class AnnotationIn(BaseModel):
    refType: str  # note | diary | letter
    refId: str
    content: str


@app.post("/note")
def mc_note_create(body: NoteIn, _: None = Depends(require_auth)):
    text = (body.content or "").strip()
    if not text:
        raise HTTPException(400, "empty content")
    rec = _mc.make_base("n", author=body.author or "user", content=text[:2000])
    _mc.insert("note", rec)
    return {"ok": True, "note": {**rec, "annotations": []}}


@app.get("/note/list")
def mc_note_list(author: Optional[str] = None, limit: int = 100,
                 _: None = Depends(require_auth)):
    pred = (lambda r: r.get("author") == author) if author else None
    rows = _mc.list_all("note", pred=pred, limit=min(limit, 300))
    return {"ok": True, "notes": _mc.with_annotations("note", rows)}


@app.post("/diary")
def mc_diary_create(body: DiaryIn, _: None = Depends(require_auth)):
    text = (body.content or "").strip()
    if not text:
        raise HTTPException(400, "empty content")
    vis = body.visibility if body.visibility in ("visible", "private") else "visible"
    rec = _mc.make_base("d", author=body.author or "ai",
                        title=(body.title or "").strip()[:60],
                        content=text[:4000],
                        visibility=vis,
                        locked=bool(body.locked))
    _mc.insert("diary", rec)
    return {"ok": True, "diary": {**rec, "annotations": []}}


@app.get("/diary/list")
def mc_diary_list(visibility: Optional[str] = None, author: Optional[str] = None,
                  limit: int = 100, _: None = Depends(require_auth)):
    def pred(r):
        if visibility and visibility != "all" and r.get("visibility") != visibility:
            return False
        if author and r.get("author") != author:
            return False
        return True
    rows = _mc.list_all("diary", pred=pred, limit=min(limit, 500))
    return {"ok": True, "diaries": _mc.with_annotations("diary", rows)}


@app.post("/letter")
def mc_letter_create(body: LetterIn, _: None = Depends(require_auth)):
    text = (body.content or "").strip()
    if not text:
        raise HTTPException(400, "empty content")
    ts = _mc._parse_iso(body.scheduledAt)
    if ts is None or ts <= time.time():
        # 立即投递
        rec = _mc.make_base("l", author=body.author or "user", content=text[:4000],
                            scheduledAt=_mc.now_iso(), status="delivered",
                            deliveredAt=_mc.now_iso())
    else:
        rec = _mc.make_base("l", author=body.author or "user", content=text[:4000],
                            scheduledAt=body.scheduledAt, status="pending",
                            deliveredAt=None)
    _mc.insert("letter", rec)
    return {"ok": True, "letter": {**rec, "annotations": []}}


@app.get("/letter/list")
def mc_letter_list(status: Optional[str] = None, limit: int = 100,
                   _: None = Depends(require_auth)):
    def pred(r):
        if status and status != "all" and r.get("status") != status:
            return False
        return True
    rows = _mc.list_all("letter", pred=pred, limit=min(limit, 500))
    return {"ok": True, "letters": _mc.with_annotations("letter", rows)}



@app.post("/note/delete")
def mc_note_delete(body: DeleteIn, _: None = Depends(require_auth)):
    if not _mc.delete("note", body.id):
        raise HTTPException(404, "not found")
    _mc.delete_by_ref("annotation", "note", body.id)
    return {"ok": True}

@app.post("/diary/delete")
def mc_diary_delete(body: DeleteIn, _: None = Depends(require_auth)):
    if not _mc.delete("diary", body.id):
        raise HTTPException(404, "not found")
    _mc.delete_by_ref("annotation", "diary", body.id)
    return {"ok": True}

@app.post("/letter/delete")
def mc_letter_delete(body: DeleteIn, _: None = Depends(require_auth)):
    if not _mc.delete("letter", body.id):
        raise HTTPException(404, "not found")
    _mc.delete_by_ref("annotation", "letter", body.id)
    return {"ok": True}

@app.post("/annotation")
def mc_annotation_create(body: AnnotationIn, _: None = Depends(require_auth)):
    text = (body.content or "").strip()
    if not text:
        raise HTTPException(400, "empty content")
    if body.refType not in ("note", "diary", "letter"):
        raise HTTPException(400, "bad refType")
    if _mc.get(body.refType, body.refId) is None:
        raise HTTPException(404, "target not found")
    rec = _mc.make_base("a", author="user", refType=body.refType,
                        refId=body.refId, content=text[:1000])
    _mc.insert("annotation", rec)
    return {"ok": True, "annotation": rec}


@app.get("/annotation/list")
def mc_annotation_list(refType: Optional[str] = None, refId: Optional[str] = None,
                       _: None = Depends(require_auth)):
    def pred(r):
        if refType and r.get("refType") != refType:
            return False
        if refId and r.get("refId") != refId:
            return False
        return True
    rows = _mc.list_all("annotation", pred=pred, limit=500, newest_first=False)
    return {"ok": True, "annotations": rows}


# 定时投递线程（幂等）
_letter_delivery_started = False


def start_letter_delivery():
    global _letter_delivery_started
    if _letter_delivery_started:
        return
    _letter_delivery_started = True

    def _loop():
        while True:
            try:
                due = _mc.deliver_due_letters()
                if due:
                    print(f"[letter] delivered {len(due)} letters")
            except Exception as e:
                print("[letter] error:", e)
            time.sleep(30)

    threading.Thread(target=_loop, name="letter-delivery", daemon=True).start()


@app.on_event("startup")
def _start_letter_delivery_event():
    start_letter_delivery()
