# Wake Engine（VPS 版 · 非 Supabase）

来源思想：galatea-garden-wake-bridge / GPT 总结 + guardian（欲望六轴）。
绑定你的栈：App → VPS → AI/TTS/电话，不依赖 Supabase Realtime。

## 数据流

```
App 重要动作 → POST /event（chat_message 同时喂六轴 she_spoke）
                    ↓
              events（内存/可换库）  +  desire/ 欲望六轴引擎（state.json 落盘）
                    ↓
         Scheduler 每 5 分钟：desire.tick() → presence → decide()
                    ↓
              wake_queue pending（six_axis_* 项带 need_persona=true）
                    ↓
App 轮询 GET /api/wake/pull 或 /proactive/pull
                    ↓
         聊天塞消息；needPersona 项 → 前端用人设生成正文；channel=call 则创建 invite
```

## 事件类型（前端会发）

app_open, chat_message, wardrobe_change, diary_write,
music_play, sleep_time, cabinet_change, duty_done, call_end

六轴事件（`/api/desire/event` 或 `/event` 直接发）：
she_spoke（chat_message 自动映射）、intimate、went_out、built、rested、resolved、unease_add

## 欲望六轴（desire/engine.py · 移植 guardian）

| 轴 | 含义 | 涨速/h | 过线阈值 | 满足事件 |
|----|------|--------|----------|----------|
| missing | 想她 | 0.05（约9h过线） | 0.6 | she_spoke |
| desire | 欲念 | 0.01 | — | intimate |
| curiosity | 好奇 | 0.015 | 0.7 | went_out |
| build | 造物欲 | 0.01 | 0.8 | built |
| fatigue | 疲惫 | 0.02 | — | rested 清零 |
| unease | 不安 | 手动 | — | resolved 清零 |

- 参数全部在 `desire/config.py`，可用环境变量覆盖（如 `DESIRE_MISSING_GROWTH`）
- sourceCoverage 三态（full/partial/stale）：24h 没收到 she_spoke 且 daemon 正常 tick
  → 标 stale，missing 涨速打 3 折 + 硬顶 0.55（不信"她沉默"的高读数）
- 在场感知 `presence.py`：读 `data/last_active.json`，<6h present / <12h absent / >12h unknown；
  present 与 unknown 都不主动开口（宁可沉默，不在她面前说她不在）

## 决策（wake/decide.py · 优先级）

1. DND → 不行动
2. presence present → 不主动开口
3. presence unknown → 不赌
4. missing 过线且非深夜 → `six_axis_missing`（need_persona，前端生成正文）
   curiosity 过线 → `six_axis_curiosity`
5. 情境规则（presence=absent）：换装跟进 / 日记 / 深夜 / 早安（模板消息直接推）
6. 静默兜底：距上次行动 ≥8h → `six_axis_fallback`

## API

- `GET /api/desire/state` — 六轴当前读数 + top3 + 在场（前端身体状况页展示）
- `POST /api/desire/event` — 喂六轴事件
- `POST /api/desire/debug` — 测试：回填 lastUpdate 验证涨速
- `POST /event` — 行为事件；chat_message 自动映射 she_spoke
- pull 响应新增 `needPersona` 字段：true 表示正文应由前端用人设生成

## V1 情境规则（wake/rules.py · 已删通用模板）

- 换装后 12h 内、沉默 ≥1.5h → wardrobe_followup
- 写日记后 2–10h → diary_soft
- 深夜关心（22:00-02:00）→ night_care
- 早安（07-11 且沉默 ≥8h）→ morning

（沉默时长触发的 miss_user/check_in 通用模板已删，改由六轴情绪驱动。）

## 与前端

Memory Palace 已有 proactive/pull；本 API 同时挂在 `/proactive/pull` 与 `/api/wake/pull`。
six_axis_* 项带 needPersona=true，前端拉取后用 agent 人设生成正文再进聊天。
vigil（LLM 独处意识流）可选注入六轴：`VIGIL_SIX_AXIS=0` 关闭，默认开。
