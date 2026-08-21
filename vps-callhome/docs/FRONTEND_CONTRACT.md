# 前端契约（Memory Palace 电话页）

## 鉴权

可选 Header：`X-Auth-Token: <AUTH_TOKEN>`

## STT

`POST /stt`  
`Content-Type: multipart/form-data`  
字段：`file` 或 `audio`（webm/wav/m4a）

成功：

```json
{ "text": "今天有点累", "emotion": "sad", "tone": "quiet" }
```

`emotion` / `tone` 可选。

## MiniMax TTS 代理

`POST /tts/minimax`  
JSON body 可与官方 t2a_v2 类似，并允许：

```json
{
  "text": "...",
  "model": "speech-2.6-turbo",
  "voice_setting": { "voice_id": "female-shaonv" },
  "api_key": "可选，优先用服务端环境变量",
  "group_id": "",
  "endpoint": ""
}
```

响应与官方一致，至少：

```json
{ "data": { "audio": "<hex mp3>", "status": 2 }, "base_resp": { "status_code": 0 } }
```

## 来电

`POST /api/call/invite` `{ "reason": "想你了" }`  
→ `{ "ok": true, "invite": { "id", "reason", "status": "pending", "expires_at" } }`

`GET /api/call/invite`  
→ `{ "invite": { ... } | null }`

`POST /api/call/answer` `{ "id", "action": "accept"|"decline", "note": "" }`  
→ `{ "ok": true }`

DND：若服务端记录勿扰，invite 可返回 `{ "ok": false, "dnd": true }`。
