# Changelog

Substantive changes in **this repository**, newest first. Dates are commit dates.
The project has two independent repositories (backend `FileCodeBox`, frontend
`FileCodeBoxFronted`); features usually need both sides, so they are recorded together.

---

## Unreleased (working tree)

### docs

- Fully rewrote `readme.md` and `readme_en.md` so both cover the same feature set.
  Fixed two contradictions with the implementation: a documented `/share/text/`
  endpoint that **does not exist** (text-only sharing uses `/share/file/` with only
  a `remark`), and a recommendation to set `WORKERS` to the CPU core count that
  contradicted the multi-instance section of the same file.
- Rewrote `SECURITY.md` (previously the untouched GitHub template placeholder) with
  a threat model, security-relevant design trade-offs, and a hardening checklist.
- Added `docs/contributing.md` and `docs/en/contributing.md` defining comment and
  documentation conventions: **comments explain why, not what**; changing an
  endpoint or config key requires updating docs in the same commit.
- Filled in this file and the Chinese changelog (both were 0-byte placeholders).
- **Trimmed the docs**: `readme.md` and `readme_en.md` went from 500+ lines to about 180,
  keeping only the feature table, screenshots, quick start and a docs index. The env var
  table, multi-instance section, reverse proxy config, curl examples, API overview and the
  full config table all moved into `docs/` to stop dual maintenance. Re-shot every
  screenshot in `.github/images/` (three new: pickup code, dashboard, settings).
- **Re-calibrated the configuration reference**: rewrote `docs/guide/configuration.md` and
  its English twin to cover all 88 keys in `DEFAULT_CONFIG`, adding the previously missing
  `roomCache*`, `virusScan*`, `chunkExpireHours`, `globalStorageQuotaMB`,
  `auditLogRetentionDays`, `shareDomain`, `wsBaseUrl`, `fileTypeWhitelist`, `codeLength`
  and `db*`. Removed `max_save_seconds` and `uploadCount`, which **do not exist** in the
  implementation (the real key is `uploadRateLimitCount`), and fixed the `uploadSize`
  default (100MB, not 10MB).
- Moved operations details that only lived in the README into `docs/`: the three
  multi-instance constraints (session affinity, per-instance rate counters, binary chunks
  not crossing instances), the `wsBaseUrl` troubleshooting order for CDNs that drop
  WebSocket, both virus-scan hook forms with the fail-open trade-off, and the room file
  receive-flow state machine with its source priority.
- Fixed six cross-references pointing at README sections that no longer exist
  (`api/index.md`, `guide/realtime.md`, `guide/configuration.md`,
  `guide/getting-started.md` and their English counterparts) and updated the sync rules in
  `contributing.md`: details belong in `docs/`, the README no longer carries tables.

### refactor(comments)

- Backend: added module-level docstrings and key function comments across all
  production modules in `core/` and `apps/`; removed the 2023-era IDE author
  headers from `main.py` and the `__init__.py` files; documented all 31 historical
  migrations (including the silent trap that the function **must** be named `migrate()`).
- Frontend: added module descriptions to all 136 `.ts` / `.vue` files under `src/`.
  Locale files and `vite-env.d.ts` are generated/pure-data and are intentionally excluded.

> Comment/doc changes only — no logic changes.

---

## 2026-10-29

### feat(cluster) Multi-instance deployment

- New `core/cluster.py` as the single coordination point: event broadcast,
  membership snapshots, distributed locks.
- Default `LocalHub` has zero dependencies and behaves exactly as before;
  setting `REDIS_URL` / `redisUrl` switches to `RedisHub`.
- Room membership synchronized across instances (incremental events + a 5s full
  snapshot that self-heals); room quota lock replaced with a renewable distributed lock.
- Binary file chunks are **deliberately not** forwarded across instances: session
  affinity is required at the load balancer, and `relay_failed` is sent otherwise.
- Capability report now requires both "database supports concurrent writes" and
  "cluster coordination enabled"; `WORKERS>1` with missing conditions logs an ERROR.
- Frontend: `relay_failed` marks the transfer as failed and suggests P2P or rejoining.

## 2026-10-28

### feat(share) Secondary passcode

- `FileCodes.access_code_hash` (PBKDF2) + migration 033.
- Creation side covers all four paths: single-request upload (single/multi-file/text-only),
  chunk completion, and presign confirmation (proxy + direct).
- Retrieval side: `/share/select` and `/share/select/info` verify the passcode;
  `/share/download/item` and `/share/zip` require the short-lived token issued at
  retrieval for protected shares.
- **Wrong passcodes do not consume a download count** (otherwise a few guesses would
  be a denial of service); they still count toward failure rate limiting and the audit log.
- Frontend: optional passcode field on send; a dialog on 403 retries after input.

### feat(security) Pluggable virus-scan hook

- `core/scan.py`: command or HTTP scanner, arguments passed as a list and never
  through a shell.
- `virusScanFailOpen` controls behavior when the scanner is unavailable
  (default: allow — a dead scanner must not take uploads down).
- Wired into single-file, multi-file, and chunked upload paths.

### fix(admin)

- Added 11 missing keys to the admin config whitelist (audit retention, scan settings,
  room cache, global quota, themes, ...).
- Database and Redis settings are **deliberately excluded** from the whitelist:
  they cannot take effect at runtime, so online editing would only mislead.

## 2026-10-27

### feat(share) My shares and revocation

- `FileCodes.owner_token` (migration 032): sender capability for revocation and
  viewing one's own share.
- `POST /share/revoke`: distinct 403/404 semantics; checks physical file references
  before deletion (dedup means records share files).
- Frontend sent-record list supports early revocation; admin audit action label added.

### feat(share) End-to-end integrity verification

- Server computes an authoritative `file_hash` while streaming to disk; a
  client-supplied hash is never trusted (cache-poisoning defense).
- Frontend verifies SHA-256 in a Web Worker after download and reports the result.

### perf

- Room quota statistics moved to Tortoise `annotate(Sum)` (portable across databases,
  with a row-by-row fallback).
- Two synchronous disk I/O operations moved off the event loop (`asyncio.to_thread`).
- Periodic SQLite WAL `wal_checkpoint(TRUNCATE)` plus backup documentation.

### build

- New `scripts/clean-dist.mjs`: cleans `dist` before build, fixing artifact
  accumulation caused by `emptyOutDir:false` (falls back to renaming so a cleanup
  failure never aborts the build).

## 2026-10-26

### feat(audit) Audit log

- `audit_log` table (migration 031) and admin page; logins, config changes,
  deletions, revocations are recorded.
- `core/audit.py`'s `record()` swallows its own exceptions: audit failure never
  breaks the main workflow.

### Frontend

- Admin audit log page; Traditional Chinese locale.

## 2026-10-25

### feat(core) Configurable database type

- `dbType` supports `sqlite` / `postgres` / `mysql` with deployment capability
  auto-detection.

## 2026-10-24

- Forced admin password change on first login.
- Global storage usage statistics and quota warning (enabled by default).
- Instant upload / dedup: identical content stored once.
- TURN connectivity self-check endpoint.
- Large-file hashing moved into a Web Worker; P2P failure degradation notice.

## 2026-10-23

- Room member tokens: write endpoints require "currently online **or** holding a
  member token", removing the must-be-online constraint.
- Persisted room messages, server-side room file cache, multi-source retrieval,
  end-to-end integrity verification.

## 2026-08-27

- Backend serves the frontend build (`frontend_dist`) directly for single-port deployment.
- GitHub Actions: build the Docker image from the committed `frontend_dist` and push
  to Docker Hub.
- VitePress documentation site and GitHub Pages deployment.

---

## Notes

- There is **no separate versioned release line**; the current `master` / `main` commit is authoritative.
- Back up the `data/` directory (or database) and `JWT_SECRET` / `ENCRYPTION_KEY`
  before upgrading: losing the encryption key makes stored S3/WebDAV credentials unrecoverable.
- Migrations run automatically at every startup and must be idempotent. The function
  in a new migration **must** be named `migrate()`.
