# Memory Palace · 通话配套 VPS（给 Claude Code）

前端 `index.html` 已支持：

- MiniMax TTS（可直连或走代理）
- 按住说话 → `POST {sttUrl|/stt}` multipart
- 来电轮询 `GET /api/call/invite`
- 接听/拒接 `POST /api/call/answer`

本目录是 **最小可跑后端骨架**，请在 VPS 上补全 STT 模型与（可选）推送。

## 目录

```
vps-callhome/
  app/main.py          # FastAPI：/stt /tts/minimax /api/call/*
  app/requirements.txt
  docs/FRONTEND_CONTRACT.md
  docs/DEPLOY.md
  .env.example
```

## 环境变量

见 `.env.example`。

## 本地试跑

```bash
cd vps-callhome
python -m venv .venv && source .venv/bin/activate
pip install -r app/requirements.txt
cp .env.example .env   # 填入 MINIMAX_API_KEY、AUTH_TOKEN
uvicorn app.main:app --host 0.0.0.0 --port 8787
```

前端电话页填写：

- STT URL: `https://你的域名/stt`
- TTS 代理: `https://你的域名`（会请求 `/tts/minimax`）
- 网关 Base: `https://你的域名`
- Token: 与 `AUTH_TOKEN` 一致

## STT 现状

`POST /stt` 默认用 **faster-whisper**（若已安装）；否则返回明确错误，便于换成 SenseVoice。

SenseVoice 接入点：改 `transcribe_audio()` 即可，保持返回：

```json
{ "text": "...", "emotion": "optional", "tone": "optional" }
```

## 推送 / APK

本骨架 **不包含** FCM/APNs。来电仅靠前端轮询 invite。  
APK 侧：用原生推送点进来电页；或 WebView 打开同一前端并保持轮询。

## 协议对齐 Callhome（简化）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/call/invite | body `{reason}` → 创建 pending invite |
| GET  | /api/call/invite | 返回当前 pending 或 null |
| POST | /api/call/answer | `{id, action: accept\|decline, note?}` |

