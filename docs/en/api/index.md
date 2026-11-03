# API Reference

> Compiled from the routes actually registered in this repository
> (`apps/base/views.py`, `apps/admin/views.py`). When in doubt, the code wins.
> Router prefixes are mounted in `main.py`.

## Conventions

### Response shape

All business responses use:

```json
{ "code": 200, "message": "ok", "detail": { } }
```

File-download endpoints return a binary stream directly, without this wrapper.

### Status and business codes

| Value | Meaning |
|-------|---------|
| `200` | OK |
| `400` | Bad parameters (invalid expiry style, remark too long, ...) |
| `403` | Auth failure (code mismatch, missing/wrong passcode, no admin privilege) |
| `404` | Not found |
| `410` | Share expired (time elapsed or download count exhausted) — **distinct from 404** |
| `423` | Failure rate limit hit |

### Authentication

| Method | Applies to |
|--------|-----------|
| None | Retrieval, delivery, read-only room info (subject to `openUpload`) |
| `Authorization: Bearer <JWT>` | All `/admin/*` except `POST /admin/login`; uploads when `openUpload=0` |
| Pickup `code` | Retrieval and download |
| `access_code` | Shares protected by a secondary passcode |
| `owner_token` | Revocation, sender viewing their own share details |
| Member token | Room write endpoints ("currently online **or** holds a member token") |

### Expiry

`expire_style`: `day` / `hour` / `minute` / `count` (download count) / `forever`.
`expire_value` is the number (count of downloads in `count` mode).

---

## Sharing `/share`

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/share/file/` | Upload file(s) / share text only |
| `GET` | `/share/check_code/` | Detect code type (file/manage/delivery/retrieval/room) |
| `GET` | `/share/select/` | Look up share info (does not consume a download) |
| `GET` | `/share/select/info/` | Read-only detail + live remaining count |
| `POST` | `/share/select/` | Actual retrieval (**consumes a download**), returns a signed download `key` |
| `POST` | `/share/revoke` | Revoke a share (requires `owner_token`) |
| `GET` | `/share/download` | Download single file (`code` + `key`) |
| `GET` | `/share/download/item/{item_id}` | Download one item of a multi-file share |
| `GET` | `/share/zip/{code}` | ZIP download (also works for single-file shares) |

### `POST /share/file/`

One endpoint serves three modes, distinguished by whether a file is present:

| Mode | Fields |
|------|--------|
| Single file | `file` + optional `remark` |
| Multi-file | multiple `files` + optional `remark` (limit `maxMultiFileCount`, default 20) |
| **Text only** | **`remark` only, no file field** |

> The `/share/text/` endpoint that appeared in older docs **does not exist**.
> Text-only sharing uses `/share/file/` with no file; the content is the `remark`,
> limited by `maxTextLength` (default 200000 characters).

Common form fields:

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `expire_value` | int | `1` | Expiry value |
| `expire_style` | str | `day` | `day` / `hour` / `minute` / `count` / `forever` |
| `remark` | str | `""` | Remark; the shared content itself when no file is attached |
| `access_code` | str | empty | Secondary passcode (2–32 chars); only a PBKDF2 hash is stored |

```bash
curl -X POST "http://localhost:12345/share/file/" \
  -F "file=@./a.txt" -F "expire_value=1" -F "expire_style=day" \
  -F "access_code=pass2026"

# Text-only
curl -X POST "http://localhost:12345/share/file/" -F "remark=text to share"
```

Response `detail`: `{ "code": "ABCD12", "owner_token": "..." }`. Keep `owner_token` —
you need it to revoke or to view your own share details.

### `POST /share/select/`

```json
{ "code": "ABCD12", "access_code": "pass2026" }
```

- A missing/wrong passcode returns `403` and **does not consume a download count**
  (otherwise a few guesses become a denial of service). It still counts toward failure
  rate limiting and the audit log.
- For passcode-protected shares the response includes `access_key`; subsequent
  `/share/download/item` and `/share/zip` calls must carry it (those two endpoints
  used to accept only the pickup code, which allowed bypassing the passcode).
- Count-based shares decrement only after all checks pass.

### `GET /share/select/info/`

Read-only, **does not consume a download**. Same shape as `select` plus `expired_count`.
For callers who have not passed the passcode it returns **only "passcode required" and
expiry status** — no filename, size, or hash. The sender can see everything with `owner_token`.

### `GET /share/download`

`?code=...&key=<signed key from select>`. Supports HTTP Range / 206 (provided by
Starlette `FileResponse`). The signed key is time-limited; retrieve again for a fresh one.

---

## Chunked upload `/chunk`

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/chunk/upload/init/` | Create session, returns `upload_id` and chunk parameters |
| `POST` | `/chunk/upload/chunk/{upload_id}/{chunk_index}` | Upload one chunk |
| `GET` | `/chunk/upload/status/{upload_id}` | List received chunks (for resume) |
| `POST` | `/chunk/upload/complete/{upload_id}` | Merge chunks and issue a pickup code |
| `DELETE` | `/chunk/upload/{upload_id}` | Abort session, clean temp chunks |

The **passcode, expiry, and remark are submitted at the `complete` step**, not stored
in the upload session. While merging, the server streams and computes an authoritative
SHA-256; a client-supplied hash is never trusted (cache-poisoning defense).

## Presigned upload `/presign`

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/presign/upload/init` | Create a direct-upload session |
| `PUT` | `/presign/upload/proxy/{upload_id}` | Proxied upload (server forwards to storage backend) |
| `POST` | `/presign/upload/confirm/{upload_id}` | Confirm after client-side S3 upload, get pickup code |
| `GET` | `/presign/upload/status/{upload_id}` | Session status |
| `DELETE` | `/presign/upload/{upload_id}` | Cancel session |

`access_code` / `expire_value` / `expire_style` / `remark` are accepted at proxy or confirm.

---

## Inbox `/collection` and delivery `/delivery`

### Inbox

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/collection/create/` | Create inbox; returns delivery / retrieval / manage codes |
| `PATCH` | `/collection/update/{collection_code}` | Change settings (max files, expiry, ...) |
| `GET` | `/collection/status/{collection_code}` | Status |
| `GET` | `/collection/manage/{collection_code}` | Manage view (manage code) |
| `GET` | `/collection/manage-by-delivery/{delivery_code}` | Manage info via delivery code |
| `GET` | `/collection/retrieve/{retrieve_code}` | Retrieval view (retrieval code) |
| `GET` | `/collection/download/{file_id}` | Download one submission |
| `GET` | `/collection/zip/{code}` | ZIP download |
| `DELETE` | `/collection/delete/{file_id}` | Delete a submission |

### Delivery

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/delivery/{delivery_code}` | Delivery page info |
| `POST` | `/delivery/upload/` | Single-request delivery |
| `POST` | `/delivery/chunk/init/` | Chunked delivery init |
| `POST` | `/delivery/chunk/complete/` | Chunked delivery complete |
| `POST` | `/delivery/chunk/cancel/` | Cancel chunked delivery |

Delivery and retrieval codes are separate: submitters can only upload; the collector
needs the manage/retrieval code to view and download.

---

## Realtime rooms `/room` and `/ws`

### HTTP

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/room/create/` | Create room |
| `GET` | `/room/info/{room_code}` | Room info |
| `GET` | `/room/history/{room_code}` | Message history (reconnect backfill) |
| `GET` | `/room/{room_code}/files` | Cached file list |
| `POST` | `/room/{room_code}/file/upload` | Upload cached file (quota applies) |
| `GET` | `/room/{room_code}/file/{transfer_id}/link` | Refresh signed download link |
| `GET` | `/room/{room_code}/file/{transfer_id}/download` | Download cached file |
| `DELETE` | `/room/{room_code}/file/{transfer_id}` | Delete cached file |

### WebSocket

| Path | Purpose |
|------|---------|
| `/ws/collection/{code}` | Inbox live updates |
| `/ws/room/{room_code}` | Cross-device chat |
| `/ws/direct/{room_code}` | Direct transfer |
| `/ws/transfer/{room_code}` | File transfer |

Room write endpoints require "currently online **or** a member token"; the token is
issued by the server on WS join.

### Compatibility endpoints

`POST /transfer/create/`, `POST /direct/create/`, `GET /transfer/info/{room_code}`,
`GET /direct/info/{room_code}` remain for older clients.
**The `/chat/*` endpoints were removed** — chat now uses `/room` and `/ws/room/{room_code}`.

### Multi-instance limitation

Text signaling is synchronized across instances; **binary file chunks are deliberately
not forwarded** (a single frame is up to 256KB; pub/sub would inflate it ~1.4× with
unpredictable latency). Use session affinity on room code; otherwise the sender
receives `relay_failed`. See the multi-instance section of the README.

---

## Admin `/admin`

> All routes except `POST /admin/login` require `Authorization: Bearer <JWT>`.
> `admin_api` attaches the `admin_required` dependency at router level — **missing
> it on one route is an unauthorized-access bug that functional tests won't reveal**.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/admin/login` | Login, returns JWT (only public endpoint) |
| `GET` | `/admin/verify` | Validate token (read-only, no side effects) |
| `POST` | `/admin/logout` | Logout (stateless JWT; client discards the token) |
| `GET` | `/admin/dashboard` | Dashboard stats |
| `GET` | `/admin/audit` | Audit log |
| `GET` | `/admin/network/turn-check` | TURN connectivity check |
| `GET` | `/admin/config/get` | Read full config |
| `PATCH` | `/admin/config/update` | Update config (whitelisted keys only) |
| `GET` | `/admin/file/list` | File list |
| `GET` | `/admin/file/{file_id}` | File detail |
| `PATCH` | `/admin/file/{file_id}` | Update file |
| `PATCH` | `/admin/file/{file_id}/expire` | Change expiry |
| `POST` | `/admin/file/extend` | Extend |
| `DELETE` | `/admin/file/delete` | Delete file |
| `GET` | `/admin/collection/list` | Inbox list |
| `GET` | `/admin/collection/{id}/files` | Submissions in an inbox |
| `DELETE` | `/admin/collection/{id}` | Delete inbox |
| `PATCH` | `/admin/collection/{id}/expire` | Change inbox expiry |
| `POST` | `/admin/collection/{id}/extend` | Extend inbox |
| `GET` | `/admin/room/list` | Room list |
| `GET` | `/admin/room/{id}/files` | Cached files in a room |
| `DELETE` | `/admin/room/{id}` | Delete room (kicks users across instances) |
| `DELETE` | `/admin/room/cache-file/{file_id}` | Delete a cached file |
| `PATCH` | `/admin/room/{id}` | Update room |
| `PATCH` | `/admin/room/{id}/expire` | Change room expiry |
| `POST` | `/admin/room/{id}/extend` | Extend room |

### Config whitelist

`PATCH /admin/config/update` accepts whitelisted keys only. `dbType` / `dbHost` /
`dbPort` / `dbName` / `dbUser` / `dbPassword` / `dbSchema` / `redisUrl` are
**deliberately excluded**: they cannot take effect in a running process (connection
pools already exist), so editing them online would only mislead. Set them via
environment variables / config file and restart.

---

## Other endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Frontend SPA (static artifacts) |
| `POST` | `/` | Public config (fetched by the frontend at startup) |
| `GET` | `/robots.txt` | Generated from the `robotsText` config |

---

## Migration notes

- Migrations run automatically at every startup, live in `apps/base/migrations/`,
  and must be **idempotent**.
- A new migration's function **must be named `migrate()`**: the runner checks
  `hasattr(module, "migrate")`, so naming it `upgrade()` causes a **silent skip**
  (the log still prints "executing migration" but nothing happens) until a runtime
  `no such column` error exposes it.
- Latest migration: `migrations_033.py` (`FileCodes.access_code_hash`).
