# Security Policy

## Supported Versions

Security fixes are applied to the current `master` branch of this repository.
There is no separate LTS line; please upgrade to the latest revision before reporting an issue.

| Version | Supported |
|---------|-----------|
| Current `master` | :white_check_mark: |
| Older releases | :x: (upgrade first) |

## Reporting a Vulnerability

**Please do not open a public issue for security vulnerabilities.** A public report
puts every self-hosted instance at risk before operators have a chance to patch.

Preferred channels, in order:

1. **GitHub Security Advisories** — use the "Report a vulnerability" button on this
   repository's Security tab. It creates a private discussion with maintainers.
2. **Private contact** — if advisories are unavailable, open a regular issue asking
   for a private contact address, without including any exploit details.

Please include:

- Affected component and, if known, the file/route
- A minimal reproduction (request sequence is enough; no real user data)
- Impact assessment: what an attacker can actually obtain or change
- Whether the instance uses the default single-instance setup or the multi-instance setup

What to expect:

| Stage | Target |
|-------|--------|
| Acknowledgement | within 7 days |
| Initial assessment (confirmed / needs more info / declined) | within 14 days |
| Fix or mitigation for confirmed issues | depends on severity; coordinated disclosure with the reporter |

We will credit reporters in the changelog unless they prefer to stay anonymous.
Please give us a reasonable window to ship a fix before public disclosure.

## Threat Model

FileCodeBox is **self-hosted software**. The operator controls the server, the
database, and the storage backend; the project's job is to keep those boundaries
honest. Understanding the following assumptions matters more than any single setting:

- **The pickup code is a bearer credential.** Anyone who has it can retrieve the
  file. It commonly travels through chat and email in plaintext. Use the optional
  **secondary passcode** (`access_code`) over a separate channel for sensitive files.
- **There is no per-user account system for sharing.** Authorization for a share is
  its code (plus optional passcode); the `owner_token` is the only sender-side
  capability and is used for revocation and viewing one's own share details.
- **The admin panel is the only privileged surface.** All `/admin/*` routes except
  `POST /admin/login` require a Bearer JWT.
- **Server-side computed hashes are authoritative.** The server recomputes a SHA-256
  while streaming a file to disk and never trusts a client-supplied hash; otherwise
  a client could poison the dedup cache and make other users download different bytes.

## Security-Relevant Design Decisions

These are deliberate trade-offs, documented so that operators do not assume a
guarantee that does not exist:

| Decision | Rationale | Consequence to be aware of |
|----------|-----------|----------------------------|
| Passcode failures do **not** consume a download count | Otherwise a few guesses would burn the recipient's quota — a trivial denial of service | Wrong guesses are still rate-limited and written to the audit log |
| Read-only detail endpoint masks name/size/hash until the passcheck passes | A leaked code alone should not reveal what the file is | The sender's own record view uses `owner_token` to bypass the mask |
| `/share/download/item` and `/share/zip` require the short-lived `access_key` for passcode-protected shares | Those endpoints previously accepted only the pickup code, which allowed bypassing the passcode | The key is issued by `/share/select` at retrieval time |
| Download links are signed and time-limited | Prevents permanent, shareable direct URLs | Changing the admin password invalidates outstanding links when `JWT_SECRET` is unset (key is derived from the password) |
| Dedup means several records can share one physical file | Saves disk for repeated uploads | **Every deletion path must check references first**, or other shares become dead links |
| Virus scan is fail-open by default (`virusScanFailOpen=true`) | A dead scanner should not take the entire upload feature down | Set it to `false` if your policy requires fail-closed; a scanner outage will then reject uploads |
| Audit logging swallows its own exceptions | A broken audit table must never break the main workflow | An audit outage can leave gaps; monitor the log for warnings |
| Room file chunks are **not** forwarded across instances | 256KB frames over pub/sub would be base64-inflated ~1.4× with unpredictable latency | Multi-instance deployments need session affinity on room code |
| IP rate limiting is per instance in multi-instance mode | Coordination overhead is not worth it for an approximate control | Effective threshold ≈ configured value × instance count |
| Database/Redis settings are excluded from the admin config whitelist | They cannot be hot-swapped once connection pools exist | They must be set via environment/config and require a restart |

## Hardening Checklist for Public Deployments

1. **Change the default admin password** (`FileCodeBox2023`). First login forces this;
   do not skip it.
2. **Set `JWT_SECRET` and `ENCRYPTION_KEY`** to independent random values
   (`openssl rand -hex 32`). Otherwise both are derived from the admin password,
   which couples password rotation to token invalidation and secret decryption.
3. **Use HTTPS** and put the app behind a reverse proxy that sets
   `X-Forwarded-For` / `X-Forwarded-Proto` correctly.
4. **Configure `trustedProxies`** when behind a reverse proxy. Otherwise rate
   limiting sees the proxy's address (or, if forwarded headers are trusted too
   broadly, an attacker can spoof their IP to evade limits).
5. **Set a file-type allowlist** (`fileTypeWhitelist`) and enable the virus-scan
   hook if the service accepts uploads from untrusted users.
6. **Set `openUpload=0`** if only authenticated users should be able to upload;
   guest upload then requires an admin Bearer token.
7. **Set expiry limits and storage quotas** (`globalStorageQuotaMB`,
   `roomCacheRoomQuota`) so a single user cannot fill the disk.
8. **Back up the `data/` directory / database** and keep `JWT_SECRET` and
   `ENCRYPTION_KEY` in the backup — losing the encryption key makes stored
   S3/WebDAV credentials unrecoverable.
9. **Keep `WORKERS=1`** unless you have both PostgreSQL/MySQL and Redis configured.
   See the multi-instance section of the README.

## Out of Scope

- Vulnerabilities in third-party storage providers, TURN servers, or the reverse
  proxy itself
- Issues that require the attacker to already possess valid admin credentials
- Denial-of-service caused by an operator setting an unbounded upload size or quota

Report anything else privately. We would rather triage a false positive than miss
a real issue.
