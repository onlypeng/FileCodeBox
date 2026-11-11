# Introduction

FileCodeBox (驿码) is an anonymous, passcode-based sharing tool built with
**FastAPI + Vue3**: no sign-up, just enter a pickup code and collect the file — like
picking up a parcel from a locker. Beyond plain text/file sharing it ships an inbox
(a drop box), cross-device chat, and WebRTC peer-to-peer transfer.

## Features

| Capability | Description |
|---|---|
| Pickup-code sharing | Single file / multiple files / plain text; expiry by duration, pickup count, or never |
| Second passphrase | An extra secret on top of the code, delivered over another channel; only its hash is stored and a wrong guess does not consume a pickup |
| Inbox | Drop code to submit, pickup code to collect, manage code to administer; submitters can only upload and never see other people's files |
| Direct transfer | WebRTC P2P first, server relay as fallback without touching disk; room cache lets late joiners fetch too |
| Resumable transfer | Chunked upload plus Range download; resume after an interruption |
| Instant dedup | The server hashes the stream while writing, so identical content is stored once |
| Pluggable storage | Local / S3-compatible / OneDrive / WebDAV / OpenDAL, switchable from the panel |
| Admin panel | Dashboard, file and room management, hot-reloaded settings, audit log, TURN connectivity check |
| Security | PBKDF2 passwords, JWT, IP rate limits, encrypted sensitive settings, forced first-login password change, virus-scan hook |
| UI | Simplified/Traditional Chinese and English, light/dark themes, mobile-ready |

## Use cases

- Send a large file to a colleague without signing in to a cloud drive
- Collect homework or materials where submitters must not see each other's uploads
- Move a file between two devices without it ever being stored on the server
- Run a lightweight sharing service for an intranet or a small team

## Quick start

```bash
docker run -d --restart always -p 12345:12345 \
  -v /opt/FileCodeBox:/app/data --name filecodebox \
  onlypeng/filecodebox:latest
```

Open `http://<server-ip>:12345`; the admin panel lives at `/#/admin` with the default
password `FileCodeBox2023` (forced change on first login).
Manual deployment, environment variables and reverse proxy setup are in
[Getting Started](/en/guide/getting-started).

## Next steps

- [Getting Started](/en/guide/getting-started) — deploy, env vars, reverse proxy, multi-instance
- [Sharing](/en/guide/share) / [Upload](/en/guide/upload) — expiry, chunking
- [Inbox](/en/guide/collection) — the three-code model
- [Chat & Direct Transfer](/en/guide/realtime) — WebRTC, room cache
- [API Reference](/en/api/) — route details
