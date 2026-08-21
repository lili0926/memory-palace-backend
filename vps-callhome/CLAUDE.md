# Claude Code 任务

仓库：Memory Palace 通话 + **Wake Engine** 后端。

## 必做

1. `uvicorn app.main:app --host 0.0.0.0 --port 8787` 能跑。
2. `POST /event` 存事件；`GET /api/wake/pull` 能取出规则生成的关心消息。
3. `POST /tts/minimax`、`POST /stt`、call invite 保持可用。
4. 契约字段勿改（见 docs/FRONTEND_CONTRACT.md、docs/WAKE.md）。

## 可选

- events/wake_queue 换 SQLite
- faster-whisper 或 SenseVoice
- 夜间 reflection LLM
- FCM 推送（离线）

## 验收

```bash
curl -X POST -H "X-Auth-Token: $T" -H "Content-Type: application/json" \
  -d '{"type":"wardrobe_change","data":{"clothes":"粉色裙子"}}' \
  http://127.0.0.1:8787/event

curl -X POST -H "X-Auth-Token: $T" http://127.0.0.1:8787/api/wake/run

curl -H "X-Auth-Token: $T" http://127.0.0.1:8787/api/wake/pull
```
