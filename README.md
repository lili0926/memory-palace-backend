# Memory Palace · 后端服务

Memory Palace（记忆宫殿）伴侣应用的 VPS 后端代码，GitHub 托管版。

## 目录

```
memory-palace-backend/
  vps-callhome/    # 通话 + 唤醒 + TTS/STT + desire 六轴 + vigil 守夜 + 纸条/日记/信箱
  aries-memory/    # 云端记忆库（Aries 记忆系统 v4 + chromadb 语义检索）
```

## 部署位置（VPS）

- `vps-callhome` → `/root/vps-callhome`（systemd: `memory-palace.service`，uvicorn 8787，nginx 反代 80/9090）
- `aries-memory` → `/opt/aries-memory`（systemd: `aries-memory.service`，flask 5000，nginx 反代 `/mem/`）

## 敏感文件说明

`.env`、`firebase-service-account.json`、`data/` 等均被 `.gitignore` 排除，**不要提交**。
部署前需在 VPS 上从 `.env.example` 复制出 `.env` 并填入真实密钥。

## 验证

```bash
curl -X POST -H "X-Auth-Token: $T" -H "Content-Type: application/json" \
  -d '{"type":"wardrobe_change","data":{"clothes":"粉色裙子"}}' \
  http://127.0.0.1:8787/event
```
