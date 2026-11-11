# Getting Started

FileCodeBox is a single-binary-style file sharing service: one process serves both the API
and the prebuilt frontend, so a single port is the whole application.

## Deployment

### Docker (recommended)

```bash
docker run -d --restart=always -p 12345:12345 \
  -v /opt/FileCodeBox:/app/data \
  -e JWT_SECRET="$(openssl rand -hex 32)" \
  -e ENCRYPTION_KEY="$(openssl rand -hex 32)" \
  --name filecodebox onlypeng/filecodebox:latest
```

### Docker Compose

```yaml
services:
  file-code-box:
    image: onlypeng/filecodebox:latest
    volumes:
      - fcb-data:/app/data:rw
    restart: unless-stopped
    ports:
      - "12345:12345"
    environment:
      - WORKERS=1
      - LOG_LEVEL=info
      - JWT_SECRET=${JWT_SECRET}
      - ENCRYPTION_KEY=${ENCRYPTION_KEY}
volumes:
  fcb-data:
    external: false
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address (only used by the image CMD) |
| `PORT` | `12345` | Port (only used by the image CMD) |
| `WORKERS` | `1` | Worker processes. **Keep 1** unless both multi-instance prerequisites hold |
| `LOG_LEVEL` | `info` | `debug` / `info` / `warning` / `error` |
| `JWT_SECRET` | empty | JWT signing key; derived from the admin password when empty |
| `ENCRYPTION_KEY` | empty | Key for encrypting sensitive settings; derived when empty |
| `REDIS_URL` | empty | Redis URL for multi-instance coordination; empty = single instance |

::: tip Set `JWT_SECRET` and `ENCRYPTION_KEY` explicitly in production
Leaving them empty derives both from the admin password, which is convenient but has two
side effects: **changing the admin password invalidates every issued token immediately**
(the signing key changes with it), and leaking the password leaks the signing key.
Generate with `openssl rand -hex 32`.
:::

::: warning Multi-instance prerequisites
`WORKERS > 1` requires **both**: a `postgres`/`mysql` database (SQLite allows a single
writer) and `REDIS_URL` configured (room state and quota locks must be shared across
processes). Without them startup logs an ERROR, and room messages exhibit the
"only half the members receive it" symptom that is very hard to diagnose.

Even with both in place, two constraints remain:

1. **Binary chunks are deliberately not forwarded across instances.** A frame is ≤256KB;
   pub/sub would inflate it ~1.4× with unpredictable latency. File relay therefore
   requires both peers on the same instance → configure **session affinity on room code**
   at the load balancer, otherwise the sender gets `relay_failed`.
2. **Rate limits are counted per instance.** `uploadRateLimitCount`, login-failure lockout
   and similar counters live in process memory, so the effective threshold is
   "configured value × instance count". For strict limits, count by IP at Nginx or a WAF.
:::

### Reverse proxy (Nginx)

```nginx
location / {
    # WebSocket upgrade forwarding (required by /ws/room and /ws/collection;
    # missing it shows "disconnected" in rooms and freezes inbox live progress)
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";

    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_pass http://localhost:12345;

    # Long-lived WebSocket connections need generous timeouts
    # (the 60s default kills an idle room connection)
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

::: tip When a CDN / edge layer drops WebSocket
Some CDNs (EdgeOne, ESA and others with default settings) proxy HTTP only and swallow the
`Upgrade` request. Symptoms: rooms show "disconnected" while every normal page works.
Do not replace the CDN — open **Admin → Settings → WebSocket origin** (`wsBaseUrl`) and
point it at an origin-reachable domain:

```
wss://direct.example.com     # wss:// https:// or a bare domain both work
```

Only WebSocket connections use it; HTTP still goes through the CDN. The value is delivered
at runtime with the public config, so **a page refresh applies it — no image rebuild**.
Empty or invalid values fall back to same-origin.

Troubleshooting order: confirm the image ships the WS protocol implementation
(`docker exec <container> python -c "import uvicorn, websockets"`), then check the reverse
proxy forwards `Upgrade`, and only then suspect the CDN.
:::

### Manual deployment

```bash
git clone https://github.com/onlypeng/FileCodeBox.git
cd FileCodeBox
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Linux/macOS: source .venv/bin/activate && pip install -r requirements.txt
.venv/Scripts/python.exe main.py
```

The repository already contains the built frontend in `frontend_dist`, so no extra build
step is needed.

## Usage

1. Open `http://localhost:12345`
2. Upload: drag files or pick them, set the expiry mode, then note the pickup code
3. Download: enter the pickup code on the home page and collect
4. Admin: `http://localhost:12345/#/admin`, default password `FileCodeBox2023`
   (forced change on first login)
5. More: [Inbox](/en/guide/collection) and
   [Chat & Direct Transfer](/en/guide/realtime)

## Next steps

- [Configuration](/en/guide/configuration) — every admin setting
- [Upload](/en/guide/upload) — plain, chunked, presigned
- [Inbox](/en/guide/collection) — the three-code model
- [Realtime](/en/guide/realtime) — chat, WebRTC, room cache
- [Storage](/en/guide/storage) — backend parameters
- [Security](/en/guide/security) — hardening
- [API](/en/api/) — route reference
