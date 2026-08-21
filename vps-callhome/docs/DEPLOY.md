# 部署建议

1. 域名 + HTTPS（Let's Encrypt）；麦克风与安全上下文需要 https。
2. systemd 或 docker 跑 uvicorn。
3. Nginx 反代到 8787，注意上传大小（`client_max_body_size 20m`）。
4. 防火墙只开 443。
5. 前端与 APK 填同一 Base URL + Token。

### Nginx 片段

```nginx
location / {
  proxy_pass http://127.0.0.1:8787;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  client_max_body_size 20m;
}
```

### Docker（可选）

自行写 Dockerfile：`FROM python:3.12-slim`，安装 ffmpeg（whisper 解码需要）。
