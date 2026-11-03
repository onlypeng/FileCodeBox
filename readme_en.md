<div align="center">

# FileCodeBox

### Anonymous File & Text Sharing with a Pickup Code

No sign-up. Share and retrieve with a short code.

[![GitHub stars](https://img.shields.io/github/stars/onlypeng/FileCodeBox?style=flat-square&logo=github)](https://github.com/onlypeng/FileCodeBox/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/onlypeng/FileCodeBox?style=flat-square&logo=github)](https://github.com/onlypeng/FileCodeBox/network)
[![GitHub issues](https://img.shields.io/github/issues/onlypeng/FileCodeBox?style=flat-square&logo=github)](https://github.com/onlypeng/FileCodeBox/issues)
[![GitHub license](https://img.shields.io/github/license/onlypeng/FileCodeBox?style=flat-square)](https://github.com/onlypeng/FileCodeBox/blob/master/LICENSE)

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.128+-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Vue.js](https://img.shields.io/badge/Vue.js-3.x-4FC08D?style=flat-square&logo=vue.js&logoColor=white)](https://vuejs.org)
[![WebRTC](https://img.shields.io/badge/WebRTC-P2P-blue?style=flat-square)](https://webrtc.org)

[中文](./readme.md) · [Documentation](./docs/index.md) · [Contributing](./docs/en/contributing.md) · [Security Policy](./SECURITY.md)

```bash
# One-line deploy (single instance, zero external dependencies)
docker run -d --restart always -p 12345:12345 \
  -v /opt/FileCodeBox:/app/data --name filecodebox \
  lanol/filecodebox:latest
```

</div>

---

## Table of Contents

- [What It Is](#what-it-is)
- [Current Capabilities](#current-capabilities)
- [Quick Start](#quick-start)
- [Environment Variables](#environment-variables)
- [Multi-Instance Deployment](#multi-instance-deployment)
- [Reverse Proxy](#reverse-proxy)
- [Usage Guide](#usage-guide)
- [curl Examples](#curl-examples)
- [API Overview](#api-overview)
- [Admin Panel](#admin-panel)
- [Configuration](#configuration)
- [Development](#development)
- [FAQ](#faq)
- [Disclaimer](#disclaimer)

---

## What It Is

FileCodeBox is a **self-hosted, lightweight file locker**: the sender gets a 6-character pickup code, hands it to the recipient, and the recipient enters the code to download. No accounts, no registration.

The backend is FastAPI + Tortoise ORM (SQLite by default; PostgreSQL / MySQL supported) and the frontend is Vue 3 + Vite. **The backend serves both the API and the built frontend on a single port**, so the smallest deployment is one process, one port, one data directory.

Beyond basic file/text sharing, this version ships with an inbox (file collection), cross-device chat, direct transfer (WebRTC P2P with server relay fallback), room file caching, an audit log, and a pluggable virus-scan hook.

> This project continues the work of [vastsa/FileCodeBox](https://github.com/vastsa/FileCodeBox). When documentation and code disagree, **the code in this repository is authoritative** — please open an issue.

### Typical Uses

| Use case | Feature |
|----------|---------|
| Send a file to a colleague | Pickup-code sharing with time/count-based expiry |
| Share a config snippet or log | Text-only sharing (no file upload, just a remark) |
| Collect assignments or documents | Inbox: others upload with a delivery code, you review with a manage code |
| Transfer large files between devices | Direct transfer: WebRTC P2P, server relay fallback, nothing written to disk |
| Recipient is offline | Room cache: file is staged server-side and pulled when they come online |
| Worried the code gets forwarded | Secondary passcode, communicated over a different channel |
| Private deployment on a company LAN | Fully self-hosted; data stays on your own disk or object storage |

---

## Current Capabilities

| Area | Included |
|------|----------|
| Sharing | Single/multi-file/text-only, time- and count-based expiry, extend expiry, revoke |
| Retrieval | Code lookup, read-only detail refresh, secondary passcode, signed download links, Range/206 resume, SHA-256 download verification |
| Upload | Single-request upload, chunked upload (resumable), S3 presigned direct upload, server-authoritative hash dedup |
| Inbox | Create/edit/close, separate delivery & retrieval codes, chunked delivery, ZIP download, admin management |
| Realtime | Cross-device chat, direct-transfer rooms, persisted room messages, WebRTC signaling, room file cache with quotas |
| Admin | Dashboard, file/room/inbox management, live configuration, audit log, TURN connectivity check |
| Security | PBKDF2 admin password, JWT, IP failure rate limiting, encrypted secrets, pluggable virus scanning, forced first-login password change |
| Storage | Local / S3-compatible / OneDrive / WebDAV / OpenDAL (pluggable) |
| Deployment | Single port, Docker, SQLite out of the box; PostgreSQL/MySQL + Redis for horizontal scaling |
| UI | Simplified Chinese, Traditional Chinese, English; light/dark themes; responsive |

---

## Quick Start

### Docker (recommended)

```bash
docker run -d --restart always -p 12345:12345 \
  -v /opt/FileCodeBox:/app/data \
  -e JWT_SECRET="$(openssl rand -hex 32)" \
  -e ENCRYPTION_KEY="$(openssl rand -hex 32)" \
  --name filecodebox lanol/filecodebox:latest
```

If Docker Hub is slow from China, use the mirror `docker.cnb.cool/aixk/filecodebox`.

Open `http://<server-ip>:12345`. The admin panel is at `/#/admin`; the **default admin password is `FileCodeBox2023` — change it immediately**.

### docker-compose (PostgreSQL + Redis, multi-instance)

```yaml
services:
  filecodebox:
    image: lanol/filecodebox:latest
    ports: ["12345:12345"]
    volumes: ["./data:/app/data"]
    environment:
      JWT_SECRET: ${JWT_SECRET}
      ENCRYPTION_KEY: ${ENCRYPTION_KEY}
      REDIS_URL: redis://redis:6379/0
      WORKERS: 2
      # dbType is an admin-panel setting; postgres/mysql is required for concurrent writes
    depends_on: [postgres, redis]
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: filecodebox
      POSTGRES_USER: filecodebox
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes: ["./pgdata:/var/lib/postgresql/data"]
  redis:
    image: redis:7-alpine
```

> **Do not copy `WORKERS=2` blindly**: both "database supports concurrent writes" and "cluster coordination enabled" must hold. See [Multi-Instance Deployment](#multi-instance-deployment). Otherwise startup logs an ERROR and room messages reach only half the participants — notoriously hard to diagnose.

### Manual deployment (frontend + backend as one)

```bash
# 1. Clone the backend repo (frontend build artifacts are already committed in frontend_dist)
git clone https://github.com/onlypeng/FileCodeBox.git
cd FileCodeBox

# 2. Create a venv and install dependencies
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # Linux/macOS

# 3. Start (default 0.0.0.0:12345)
.venv/Scripts/python.exe main.py
```

The frontend source lives in a **separate repository** ([FileCodeBoxFronted](https://github.com/onlypeng/FileCodeBoxFronted)). You only need it when changing the UI; a normal deployment uses the committed `frontend_dist`.

For local development you can run them separately: `npm run dev` (port 5173) proxies `/share`, `/admin`, etc. to the backend on 12345.

---

## Environment Variables

| Variable | Default | Applies to | Meaning |
|----------|---------|-----------|---------|
| `HOST` | `0.0.0.0` | Docker CMD only | Passed to `uvicorn --host`; manual startup uses the `serverHost` config key |
| `PORT` | `12345` | Docker CMD only | Passed to `uvicorn --port`; manual startup uses `serverPort` |
| `LOG_LEVEL` | `info` | Docker CMD only | uvicorn log level |
| `WORKERS` | `1` | Docker CMD + runtime capability check | Worker count. **Keep it at 1 by default**; see multi-instance prerequisites |
| `JWT_SECRET` | empty | Application | JWT signing key. When empty it is derived from the admin password, so **changing the admin password logs everyone out**. Set it explicitly in production |
| `ENCRYPTION_KEY` | empty | Application | Encryption key for S3/WebDAV secrets. Derived from the admin password if unset; set it explicitly in production |
| `REDIS_URL` | empty | Application | Redis URL for multi-instance coordination, e.g. `redis://127.0.0.1:6379/0`. Empty = single instance, no external dependency |

Generate keys with `openssl rand -hex 32`.

> `HOST` / `PORT` / `LOG_LEVEL` are shell variables in the Dockerfile `CMD` and only apply when using the image. Running `python main.py` directly uses `serverHost` / `serverPort` from `core/settings.py`.

---

## Multi-Instance Deployment

The default is **single instance**: the room WebSocket connection table and the room quota lock live in process memory, and SQLite is a single-writer database. This is a deliberate conservative choice — zero external dependencies and predictable behavior.

Before raising `WORKERS` or running multiple containers behind a load balancer, **both conditions must hold**:

| Condition | Why | How to verify |
|-----------|-----|---------------|
| Database switched to `postgres` / `mysql` | SQLite is single-writer; concurrent writes conflict | Admin panel → System config → `dbType`, then restart |
| `REDIS_URL` (or `redisUrl`) configured | Room membership, text signaling, and quota locks must be shared | Startup log: `cluster coordination: Redis mode` |

Startup logs state the conclusion honestly:

```
cluster coordination: local mode (single instance), redisUrl not configured
application initialization complete
deployment: database=sqlite multi-worker support=False
```

When both conditions hold:

```
cluster coordination: Redis mode instance=8f3c1a2b9d4e channel=filecodebox:cluster
deployment: database=postgres multi-worker support=True
```

If `WORKERS > 1` but conditions are missing, startup logs an `ERROR`.

### Three constraints you must know

1. **Room file relay needs session affinity.** Text signaling (join/leave, file cards, WebRTC negotiation) is synchronized across instances, but **binary file chunks are deliberately not forwarded across instances**: a single frame is up to 256KB and pub/sub would inflate throughput ~1.4× (base64) with unpredictable latency. Use consistent hashing / sticky sessions on room code. Pure P2P is unaffected; without affinity the sender gets an explicit `relay_failed` message rather than a silently stalled transfer.
2. **IP rate limiting counts per instance**, so the effective threshold is roughly `configured value × instance count`. An approximation, not a correctness issue.
3. **Redis is coordination, not the main path.** Brief outages drop some text signaling (membership snapshots self-heal every 5s); lock acquisition failure degrades to a process-local lock (better a tiny chance of exceeding quota than making uploads unavailable); if Redis is unreachable at startup, an error is logged and the app falls back to single instance.

Multi-instance requires `pip install redis` (not needed for the default single-instance setup, so it is not pinned in `requirements.txt`).

---

## Reverse Proxy

Nginx example (the important parts are forwarded headers and WebSocket upgrade):

```nginx
location / {
    proxy_pass http://127.0.0.1:12345;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 100m;

    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

The container trusts forwarded headers only from loopback (`--forwarded-allow-ips "127.0.0.1"`). Behind a reverse proxy, add the proxy address to the allowlist and set `trustedProxies` in the admin panel, otherwise rate limiting sees the gateway IP.

---

## Usage Guide

### Send and retrieve

1. Select one or more files (or just type text as a remark), set an expiry, submit → get a 6-character pickup code
2. The recipient enters the code → sees file info → downloads

Expiry styles: `day` / `hour` / `minute` / `count` (download count) / `forever`.

### Secondary passcode

Pickup codes travel in plaintext through chats and email; a leaked code means a leaked file. Add a 2–32 character **passcode** and communicate it over a different channel (voice, phone):

- Submitted with `access_code` at upload time; the server stores only a PBKDF2 hash, never the plaintext
- Retrieval without it → `403 passcode required`; the UI prompts and retries
- Wrong passcode → `403 incorrect passcode`, and **the download count is not consumed** (otherwise a few guesses would burn someone's quota). It still counts toward failure rate limiting and is written to the audit log
- The read-only detail endpoint returns only "passcode required" and expiry status to unauthenticated callers — no filename, size, or hash

Chunked and S3 presigned uploads support it too: the passcode is sent at the **completion** step, not stored in the upload session.

### Inbox (file collection)

Creating an inbox yields two sets of codes: a **delivery code** for submitters and **retrieval / manage codes** for the collector. Supports max file count, expiry, chunked delivery, and ZIP download.

### Direct transfer (WebRTC)

Inside a room, P2P is tried first; on failure the server relays binary frames **without writing them to disk**. Rooms support messages, file transfer, and camera sharing. If the peer is offline, room cache can stage the file server-side (default 72 hours; per-file and per-room quotas are configurable).

### Revoke

The sender can revoke a share using its `owner_token`. The server first checks whether the physical file is referenced by other records (dedup/instant upload shares files) — if so, only the record is deleted.

---

## curl Examples

<details>
<summary><b>Expand curl examples</b></summary>

**Upload a file** (default: 1 day)

```bash
curl -X POST "http://localhost:12345/share/file/" -F "file=@./a.txt"
```

**Expiry / count / multiple files / text only / passcode**

```bash
curl -X POST "http://localhost:12345/share/file/" \
  -F "file=@./a.txt" -F "expire_value=1" -F "expire_style=hour"

curl -X POST "http://localhost:12345/share/file/" \
  -F "file=@./a.txt" -F "expire_value=10" -F "expire_style=count"

curl -X POST "http://localhost:12345/share/file/" \
  -F "files=@a.txt" -F "files=@b.txt" -F "remark=batch"

# Text-only: send remark without any file
curl -X POST "http://localhost:12345/share/file/" -F "remark=text to share"

# With a secondary passcode
curl -X POST "http://localhost:12345/share/file/" \
  -F "file=@./a.txt" -F "access_code=pass2026"
```

**Retrieve**

```bash
# Read-only detail (does not consume a download)
curl "http://localhost:12345/share/select/info/?code=ABCD12"

# Actual retrieval (consumes a download, returns a signed download key)
curl -X POST "http://localhost:12345/share/select/" \
  -H "Content-Type: application/json" -d '{"code":"ABCD12"}'

# With passcode
curl -X POST "http://localhost:12345/share/select/" \
  -H "Content-Type: application/json" \
  -d '{"code":"ABCD12","access_code":"pass2026"}'

# Download (use the key returned by select)
curl -L "http://localhost:12345/share/download?code=ABCD12&key=<key>" -o a.txt
```

**Others**

```bash
# Detect code type (file / manage / delivery / retrieval / room)
curl "http://localhost:12345/share/check_code/?code=ABCD12"

# Revoke (requires owner token)
curl -X POST "http://localhost:12345/share/revoke" \
  -H "Content-Type: application/json" \
  -d '{"code":"ABCD12","owner_token":"<owner_token>"}'
```

All business responses share one shape:

```json
{ "code": 200, "message": "ok", "detail": { "code": "ABCD12", "name": "a.txt" } }
```

**When guest upload is disabled** (`openUpload=0`):

```bash
TOKEN=$(curl -s -X POST "http://localhost:12345/admin/login" \
  -H "Content-Type: application/json" \
  -d '{"password":"FileCodeBox2023"}' | python -c "import sys,json;print(json.load(sys.stdin)['detail']['token'])")

curl -X POST "http://localhost:12345/share/file/" \
  -H "Authorization: Bearer $TOKEN" -F "file=@./a.txt"
```

| `expire_style` | Meaning |
|----------------|---------|
| `day` / `hour` / `minute` | Time-based expiry |
| `count` | Expire after N downloads |
| `forever` | Never expires |

</details>

---

## API Overview

> Full reference: [docs/api](./docs/api/index.md). All business responses use `{ "code": 200, "message": "ok", "detail": {...} }`; `410` means expired.

| Prefix | Main endpoints | Purpose |
|--------|----------------|---------|
| `/share` | `POST /file/`, `GET /check_code/`, `GET /select/info/`, `POST /select/`, `POST /revoke`, `GET /download`, `GET /download/item/{id}`, `GET /zip/{code}` | Sharing & retrieval (passcode, multi-file, ZIP) |
| `/chunk` | `POST /upload/init/`, `POST /upload/`, `DELETE /upload/{id}`, `POST /complete/` | Resumable chunked upload |
| `/presign` | `POST /upload/init`, `PUT /upload/proxy/{id}`, `POST /upload/confirm/{id}`, `DELETE /upload/{id}` | S3 presigned direct / proxied upload |
| `/collection` | `POST /create/`, `PATCH /update/{code}`, `GET /status/{code}`, `GET /manage/{code}`, `GET /retrieve/{code}`, `GET /download/{id}`, `GET /zip/{code}`, `DELETE /delete/{id}` | Inbox |
| `/delivery` | `POST /upload/`, `POST /chunk/init|complete|cancel/`, `GET /{delivery_code}` | Inbox delivery |
| `/room` | Room management endpoints | Realtime rooms |
| `/ws` | `/ws/collection/{code}`, `/ws/room/{room_code}`, `/ws/direct/{room_code}`, `/ws/transfer/{room_code}` | WebSocket channels |
| `/admin` | `POST /login`, `GET /verify`, `GET /dashboard`, `GET|PATCH /file/*`, `GET|PATCH /room/*`, `GET /collection/*`, `GET /audit`, `PATCH /config/update`, `GET /network/turn-check` | Admin panel (all but login require a Bearer token) |

---

## Admin Panel

Visit `/#/admin` (or `/#/login`). Default password: `FileCodeBox2023`; **first login forces a password change**.

| Module | Capabilities |
|--------|--------------|
| Dashboard | File count, storage usage, online rooms, expiry trends |
| Files | List, detail, change expiry, extend, delete |
| Inboxes | List, view submitted files, delete, extend |
| Rooms | List, cache files, kick, delete, extend |
| System config | Full live configuration (except infrastructure settings) |
| Audit log | Logins, config changes, deletions, revocations |
| Network check | TURN server connectivity |

**Why database and Redis settings are not in the admin whitelist**: changing them cannot take effect in a running process (connection pools are already built), so exposing them would only mislead. Set them via environment variables / config file and restart.

---

## Configuration

Editable in the admin panel; takes effect on save (except structural settings).

| Key | Default | Meaning |
|-----|---------|---------|
| `openUpload` | `1` | Allow guest upload; `0` requires an admin token |
| `uploadSize` | `104857600` | Max single-file size in bytes (100MB) |
| `expireStyle` | `day/hour/minute/forever/count` | Available expiry modes |
| `enableChunk` / `uploadChunkSize` | `0` / `5` | Chunked upload toggle and chunk size (MB) |
| `codeLength` | `6` | Pickup code length |
| `maxMultiFileCount` | `20` | Max files per multi-file share |
| `errorMinute` / `errorCount` | `1` / `10` | Failure rate-limit window and count |
| `roomCacheEnabled` / `roomCacheDefaultHours` / `roomCacheMaxSize` / `roomCacheRoomQuota` | `1` / `72` / `100` / `500` | Room cache toggle, default hours, per-file limit (MB), per-room quota (MB) |
| `globalStorageQuotaMB` | `0` | Global storage quota; 0 = unlimited |
| `auditLogRetentionDays` | `90` | Audit log retention |
| `virusScanEnabled` / `virusScanHook` / `virusScanTimeoutSec` / `virusScanFailOpen` | `false` / empty / `30` / `true` | Pluggable virus-scan hook |
| `directRelayEnabled` / `directRelayChunkSize` / `directMaxRelaySize` | `1` / `64` / `0` | Relay toggle, chunk size (KB), per-file limit (0 = unlimited) |
| `shareDomain` | empty | Domain used when generating share links |
| `trustedProxies` | empty | Trusted proxy addresses for reverse-proxy deployments |
| `cors_origins` | `http://localhost:5173,http://127.0.0.1:5173` | Allowed CORS origins |
| `dbType` / `dbHost` / `dbPort` / `dbName` / `dbUser` / `dbPassword` / `dbSchema` | `sqlite` / … | Database connection (**requires restart**) |
| `redisUrl` | empty | Redis URL for cluster coordination (**requires restart**) |

### Virus-scan hook

No scanning engine is bundled, but uploads can be checked by an external scanner (local command or HTTP) after the file lands on disk.

| Item | Meaning |
|------|---------|
| `virusScanHook` | `cmd:<binary> <args...>` or an HTTP multipart endpoint |
| Return convention | Command: 0 = clean, 1 = infected, other = scan failure; HTTP: JSON `{ "infected": true/false }` |
| `virusScanFailOpen` | Allow upload when the scanner is unavailable (default `true`) — scanning is auxiliary and should not take the whole upload feature down |

Command arguments are passed as a list and **never through a shell**, so filenames cannot be injected into a command line.

---

## Development

### Two repositories

```
FileCodeBox/           backend, FastAPI (master)       ← this repo
FileCodeBoxFronted/    frontend, Vue 3 + Vite (main)   ← separate repo, no shared parent
```

The backend serves `FileCodeBox/frontend_dist`. **This directory must be committed** (the Dockerfile COPYs it). Frontend build artifacts must be synced back into the backend repo or the image will ship a stale UI.

### Local development

```bash
# Terminal 1: backend (CWD must be FileCodeBox/ — migration scanning uses a relative apps/ path)
cd FileCodeBox
.venv/Scripts/python.exe main.py

# Terminal 2: frontend
cd FileCodeBoxFronted
npm install
npm run dev        # 5173, Vite proxies to 12345
```

### Build and sync

```bash
cd FileCodeBoxFronted
npm run build      # clean-dist → architecture check → vue-tsc → vite build, ~40–100s

cd ..
python .workbuddy/sync_dist.py   # overlay dist → FileCodeBox/frontend_dist
git -C FileCodeBox add frontend_dist
```

`npm run build` includes a pre-build cleanup (`scripts/clean-dist.mjs`) to fix artifact accumulation caused by `emptyOutDir:false`. After syncing, verify the entry chunk is committed — the Dockerfile depends on it.

### Verification

```bash
cd FileCodeBox
.venv/Scripts/python.exe -m pytest -q
```

End-to-end scripts live in `.workbuddy/` (`e2e_server.py` starts a temporary server, `e2e_test.py` asserts). Each run uses a fresh temp data directory and never touches the real `data/`.

### Tech stack

| Layer | Choice |
|-------|--------|
| Backend | Python 3.12, FastAPI, Tortoise ORM, SQLite / PostgreSQL / MySQL |
| Frontend | Vue 3, Vite 7, TypeScript, Pinia, vue-i18n, Tailwind CSS |
| Realtime | WebSocket, WebRTC (P2P + TURN) |
| Storage | Local / S3 / OneDrive / WebDAV / OpenDAL |
| Cluster | Redis pub/sub (optional) |

Comment and documentation conventions are in [docs/en/contributing.md](./docs/en/contributing.md). The core rule: **comments explain why, not what**; changing an endpoint or config key requires updating the docs in the same commit.

---

## FAQ

**Forgot the admin password?** Reset `admin_token` in the `settings` row of the `keyvalue` table, or delete the data directory and reinitialize (loses all data). In production, set `JWT_SECRET` and `ENCRYPTION_KEY` and keep backups.

**Large upload fails?** Check `uploadSize` (100MB default), your reverse proxy's `client_max_body_size`, and whether chunked upload is enabled (`enableChunk=1`).

**Download link expired / 410?** The download URL returned by retrieval carries a signed, time-limited `key`. Retrieve again to get a fresh one. If the share itself expired (count exhausted or time elapsed), it cannot be recovered.

**Why is everyone logged out after changing the admin password?** Without `JWT_SECRET`, the signing key is derived from the admin password, so changing it rotates the key. Set `JWT_SECRET` to avoid this.

**Transfer stalls halfway with multiple instances?** Room file chunks are not forwarded across instances; configure session affinity on room code. See [Multi-Instance Deployment](#multi-instance-deployment).

**Why didn't instant upload (dedup) trigger?** File hashes are always computed server-side while streaming to disk; client-supplied values are not trusted (cache-poisoning defense). Only identical content dedups.

**Where is data stored?** The `data/` directory by default (mapped to `/app/data` in Docker). Configure S3/WebDAV credentials in the admin panel to switch.

---

## Disclaimer

This project is intended for lawful file sharing only. Users must comply with applicable laws and must not use it to distribute illegal or infringing content. The authors are not responsible for user conduct or its consequences.

For any public deployment: change the default admin password, set `JWT_SECRET` and `ENCRYPTION_KEY`, enable HTTPS, and configure a file-type allowlist / virus scanning as needed.

---

<div align="center">

Based on [vastsa/FileCodeBox](https://github.com/vastsa/FileCodeBox) · License: LGPL-3.0 · See [LICENSE](./LICENSE)

</div>
