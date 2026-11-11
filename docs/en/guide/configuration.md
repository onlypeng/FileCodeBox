# Configuration Guide

Settings come from two places: `DEFAULT_CONFIG` in `core/settings.py` (factory defaults)
and the override stored in the database. **Change them from Admin → Settings** — saving
writes the DB and hot-reloads the running process, no restart needed.
Editing the DB directly is only for initial provisioning.

::: warning Keys that need a restart
`serverHost` / `serverPort` / `serverWorkers` are startup arguments; restart after changing.
`redisUrl` and the `db*` keys are **rejected** on save (HTTP 400) when the value actually
changes — the subscription connection and the pool cannot be hot-swapped. Set them via
environment variables and restart.
:::

## Site & appearance

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | `驿码 - FileCodeBox` | Site name in the page title and navbar |
| `description` | string | `开箱即用的文件快传系统` | Site description (SEO) |
| `keywords` | string | `FileCodeBox, 驿码, ...` | Site keywords (SEO) |
| `notify_title` / `notify_content` | string | `系统通知` / welcome text | Top banner title and body; HTML allowed |
| `page_explain` | string | legal notice | Footer notice text |
| `robotsText` | string | `User-agent: *\nDisallow: /` | robots.txt content |
| `showAdminAddr` | int | `0` | Show the admin entry on the home page (1 = yes) |
| `opacity` | float | `0.9` | UI opacity (0–1) |
| `background` | string | `""` | Custom background image URL; empty = default |
| `themesSelect` / `themesChoices` | string / list | `themes/2024` / two built-in themes | Legacy theme picker; **not used by the dist build**, kept for compatibility |
| `spaTheme` | string | `light` | Default SPA theme: `light` / `dark` / `enterprise`, written by the theme module |

## Upload

| Key | Type | Default | Description |
|---|---|---|---|
| `openUpload` | int | `1` | Allow uploads (0 = admin only) |
| `uploadSize` | int | `104857600` | Per-file limit in **bytes**, default 100MB |
| `fileTypeWhitelist` | string | `""` | Comma-separated extensions (`jpg,png,pdf`); empty = unrestricted |
| `expireStyle` | list | `day,hour,minute,forever,count` | Expiry modes offered to uploaders |
| `maxTextLength` | int | `200000` | Max **characters** for a text share or remark |
| `uploadMinute` | int | `1` | Upload rate-limit window (minutes) |
| `uploadRateLimitCount` | int | `10` | Uploads allowed per IP inside that window |
| `maxSendFiles` | int | `20` | Files per send operation |
| `maxMultiFileCount` | int | `20` | Records per multi-file share |
| `codeLength` | int | `6` | Pickup code length (4–12); shared by file, inbox and room codes |

### Chunked upload

| Key | Type | Default | Description |
|---|---|---|---|
| `enableChunk` | int | `0` | Enable chunked upload (1 = on) |
| `uploadChunkSize` | int | `5` | Chunk size in **MB**, only when chunking is on |
| `chunkExpireHours` | int | `24` | Hours before an unfinished chunk session is garbage-collected |

Chunked upload resumes from the current page only: `uploadId` + uploaded chunk indices +
the `File` object live in page memory. A refresh loses the `File` and forces a re-upload;
only an explicit abandon or leaving the page rolls back the consumed delivery quota.

## Expiry & cleanup

| Key | Type | Default | Description |
|---|---|---|---|
| `expiredRetentionStyle` | string | `day` | Unit for retention after expiry: `day` / `hour` / `minute` / `forever` |
| `expiredRetentionValue` | int | `1` | Retention amount; `forever` = never auto-clean, files accumulate |

Retention covers all three record kinds (file shares, inboxes, rooms); the background
task deletes records and physical files together. `forever` is an escape hatch: the
cleanup loop exits immediately, so disk usage only grows and you must prune it yourself.

## Security

| Key | Type | Default | Description |
|---|---|---|---|
| `admin_token` | string | `FileCodeBox2023` | Admin password; **forced change on first login** |
| `errorMinute` | int | `1` | Wrong-pickup-code rate-limit window (minutes) |
| `errorCount` | int | `10` | Wrong attempts allowed in that window |
| `auditLogRetentionDays` | int | `90` | Audit log retention in days (0 = never prune) |
| `trustedProxies` | string | `""` | Comma-separated trusted proxy IPs; required behind Nginx or you log the proxy IP |
| `cors_origins` | string | `http://localhost:5173,...` | CORS allowlist; empty for same-origin production |

Password storage details (PBKDF2, sensitive-value encryption, placeholder write-back) are
in [Security](/en/guide/security).

### Virus scanning (optional)

Off by default with zero overhead until a hook is set. When enabled, every upload calls an
external scanner after the file lands on disk.

| Key | Type | Default | Description |
|---|---|---|---|
| `virusScanEnabled` | bool | `false` | Master switch |
| `virusScanHook` | string | `""` | Command or HTTP endpoint, see below |
| `virusScanTimeoutSec` | int | `30` | Per-scan timeout (1–300 s) |
| `virusScanFailOpen` | bool | `true` | When the scanner is unavailable: `true` = allow + warn, `false` = reject the upload |

```bash
# Command form: {file} is replaced by the staged path; exit 0 = clean, 1 = infected
clamscan --no-summary {file}

# HTTP form: POST the file bytes, expect {"infected": true, "reason": "..."}
https://scan.example.com/check
```

::: tip Why fail-open is the default
Scanning is an auxiliary control; a flaky scanner should not take the whole upload feature
down. Set it to `false` when security must win over availability, accepting that uploads
fail entirely while the scanner is unreachable.
:::

## Storage

| Key | Type | Default | Description |
|---|---|---|---|
| `file_storage` | string | `local` | Backend: `local` / `s3` / `onedrive` / `webdav` / `opendal` |
| `storage_path` | string | `""` | Local directory; empty = `data/storage` |

Backend credentials (`s3_*`, `onedrive_*`, `webdav_*`, `opendal_*`) are documented in
[Storage](/en/guide/storage).

## Inbox

| Key | Type | Default | Description |
|---|---|---|---|
| `maxCollectionFiles` | int | `100` | Hard cap on files per inbox |
| `collectionDefaultMaxFiles` | int | `20` | Default file cap when creating |
| `collectionDefaultExpireDays` | int | `7` | Default lifetime in days when creating |

## Direct transfer & rooms

| Key | Type | Default | Description |
|---|---|---|---|
| `directRelayEnabled` | int | `1` | Server relay toggle (1 = on); off means P2P only |
| `directRelaySpeedLimit` | int | `0` | Relay speed limit in KB/s (0 = unlimited) |
| `directRelayChunkSize` | int | `64` | Relay chunk size in KB (16–256). An encrypted frame is chunk + ~40B; gateways often cap at 256KB, so do not max it out |
| `directMaxRelaySize` | int | `0` | Max size of a single relayed file in MB (0 = unlimited); P2P is not affected |
| `directTurnServers` | list | `[]` | WebRTC TURN list for symmetric-NAT fallback |
| `defaultMaxMembers` | int | `10` | Default room member cap (0 = unlimited); a room may pick 1..this |
| `mediaDefaultQuality` | string | `auto` | Default shared-stream quality: `low` / `sd` / `hd` / `auto` |

```json
// One entry of directTurnServers
[{ "urls": "turn:host:3478?transport=udp", "username": "xxx", "credential": "yyy" }]
```

### Room file cache

The room's server cache stages files on local disk so late joiners can fetch them without
the sender being online.

| Key | Type | Default | Description |
|---|---|---|---|
| `roomCacheEnabled` | int | `1` | Allow staging room files on the server; off means P2P multi-source only |
| `roomCacheDefaultHours` | int | `72` | Default retention in hours (0 = follow the room lifetime) |
| `roomCacheMaxSize` | int | `100` | Per-file cap in MB (0 = unlimited) |
| `roomCacheRoomQuota` | int | `500` | Per-room quota in MB (0 = unlimited); over quota rejects new uploads, released as files expire |

Source priority is fixed: **online holder that already downloaded (P2P) > server cache >
no source**. Users who already hold a copy are never asked again whether to serve it,
which is what prevents duplicate prompts.

## Global quota

| Key | Type | Default | Description |
|---|---|---|---|
| `globalStorageQuotaMB` | int | `0` | Global quota in MB (0 = unlimited). Counts file-share records plus the room cache directory |

Exceeding the quota **does not block uploads** — the panel keeps warning and shows the
ratio. Set a value if anonymous uploads are open, otherwise the disk fills up silently.

## Outbound links

| Key | Type | Default | Description |
|---|---|---|---|
| `shareDomain` | string | `""` | Site address used to build share/pickup links (download URL, QR code, wget command), e.g. `https://share.example.com`; empty = current origin |
| `wsBaseUrl` | string | `""` | Dedicated WebSocket origin. Set it when a CDN/edge layer drops `Upgrade`; empty = same origin |

`shareDomain` only affects outbound share links; API calls still use the real origin.
`wsBaseUrl` only affects WebSockets; HTTP still goes through the CDN. Both are delivered at
runtime with the public config, so a page refresh applies them. Setup and troubleshooting
are in [Getting Started](/en/guide/getting-started).

## Server & database

| Key | Type | Default | Description |
|---|---|---|---|
| `serverHost` | string | `0.0.0.0` | Bind address (restart) |
| `serverPort` | int | `12345` | Port (restart) |
| `serverWorkers` | int | `1` | Worker processes (restart). **Keep 1** unless the multi-instance prerequisites hold |
| `redisUrl` | string | `""` | Redis URL for multi-instance coordination; empty = single instance. Restart required |
| `dbType` | string | `sqlite` | `sqlite` / `postgres` / `mysql`; SQLite is single-writer, single process only |
| `dbHost` / `dbPort` | string / int | `127.0.0.1` / `0` | Port 0 means the type default (postgres 5432 / mysql 3306) |
| `dbUser` / `dbPassword` | string | `""` | Database credentials |
| `dbName` | string | `filecodebox` | Database name |
| `dbSchema` | string | `public` | PostgreSQL schema; ignored elsewhere |

`redisUrl` and the `db*` keys are **deliberately not hot-editable**: the admin panel posts
the *entire* config object back, so the backend only rejects a request when a value truly
changed, otherwise touching one unrelated switch would break the whole form. Inject these
through `REDIS_URL`, `DB_TYPE`, etc. and restart.

## Next steps

- [Getting Started](/en/guide/getting-started) — deploy, env vars, reverse proxy, multi-instance
- [Storage](/en/guide/storage) — backend-specific parameters
- [Security](/en/guide/security) — passwords, rate limits, allowlist, virus scanning
- [Management](/en/guide/management) — what each admin module does
