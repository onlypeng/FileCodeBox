---
layout: home

hero:
  name: "FileCodeBox"
  text: "File Express Box"
  tagline: Share text and files anonymously with a pickup code — no sign-up needed
  image:
    src: /logo_small.png
    alt: FileCodeBox
  actions:
    - theme: brand
      text: Get Started
      link: /en/guide/getting-started
    - theme: alt
      text: API Reference
      link: /en/api/
    - theme: alt
      text: View on GitHub
      link: https://github.com/onlypeng/FileCodeBox

features:
  - icon: 🚀
    title: Single-Port Deployment
    details: The backend serves both the API and the built frontend. One Docker command, SQLite out of the box, zero external dependencies
  - icon: 🔒
    title: Pickup Code + Passcode
    details: An optional secondary passcode travels over a separate channel; only a hash is stored and wrong guesses never burn the download count
  - icon: 📬
    title: Inbox
    details: Delivery and retrieval codes are separate — submitters can upload but cannot read other people's files
  - icon: ⚡
    title: Direct Transfer
    details: WebRTC P2P first, server relay fallback with nothing written to disk; room cache lets an offline peer fetch files later
  - icon: 📦
    title: Pluggable Storage
    details: Local / S3-compatible / OneDrive / WebDAV / OpenDAL; server-authoritative hash dedup stores identical content once
  - icon: 🛠️
    title: Full Admin Surface
    details: Dashboard, live config, audit log, TURN check; PostgreSQL/MySQL + Redis enables horizontal scaling
---

> This project continues the work of [vastsa/FileCodeBox](https://github.com/vastsa/FileCodeBox).
> Documentation follows the implementation in the current repository. See the
> [README](https://github.com/onlypeng/FileCodeBox), [API Reference](/en/api/),
> [Contributing](/en/contributing) and
> [Security Policy](https://github.com/onlypeng/FileCodeBox/blob/master/SECURITY.md).
