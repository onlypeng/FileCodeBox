<div align="center">

# FileCodeBox · 驿码

### 匿名口令分享文本与文件

无需注册，输入提取码即可分享与取件

[![GitHub stars](https://img.shields.io/github/stars/onlypeng/FileCodeBox?style=flat-square&logo=github)](https://github.com/onlypeng/FileCodeBox/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/onlypeng/FileCodeBox?style=flat-square&logo=github)](https://github.com/onlypeng/FileCodeBox/network)
[![GitHub issues](https://img.shields.io/github/issues/onlypeng/FileCodeBox?style=flat-square&logo=github)](https://github.com/onlypeng/FileCodeBox/issues)
[![GitHub license](https://img.shields.io/github/license/onlypeng/FileCodeBox?style=flat-square)](https://github.com/onlypeng/FileCodeBox/blob/master/LICENSE)

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.128+-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Vue.js](https://img.shields.io/badge/Vue.js-3.x-4FC08D?style=flat-square&logo=vue.js&logoColor=white)](https://vuejs.org)
[![WebRTC](https://img.shields.io/badge/WebRTC-P2P-blue?style=flat-square)](https://webrtc.org)

[English](./readme_en.md) · [在线文档](./docs/index.md) · [参与贡献](./docs/contributing.md) · [安全政策](./SECURITY.md)

```bash
# 一键部署（单实例，零外部依赖）
docker run -d --restart always -p 12345:12345 \
  -v /opt/FileCodeBox:/app/data --name filecodebox \
  onlypeng/filecodebox:latest
```

</div>

---

## 功能一览

| 能力 | 说明 |
|---|---|
| **提取码分享** | 单文件 / 多文件 / 纯文本；有效期按时长、取件次数或永久设置 |
| **二次口令** | 提取码之外再设一道口令，走另一条信道告知；服务端只存哈希，猜错不扣取件次数 |
| **收件箱** | 投件码收、取件码取，提交者只能上传、看不到别人的文件 |
| **直连快传** | WebRTC P2P 优先，服务器中转兜底且不落盘；支持房间缓存、屏幕/摄像头共享 |
| **断点续传** | 分片上传与 Range 下载，中断后从断点继续 |
| **秒传去重** | 服务端落盘时流式计算哈希，同内容只存一份 |
| **多存储后端** | 本地 / S3 兼容 / OneDrive / WebDAV / OpenDAL，后台切换 |
| **管理后台** | 仪表盘、文件与房间管理、在线配置热生效、审计日志、TURN 连通性自检 |
| **安全** | PBKDF2 管理员口令、JWT、IP 失败限流、敏感配置加密、强制首次改密、病毒扫描钩子 |
| **界面** | 中文简/繁/英三语、深浅色主题、移动端适配 |

---

## 功能截图

### 取件 · 输入提取码即可

<img src=".github/images/home.png" width="760" alt="首页取件">

### 发件 · 上传文件或直接发文本

<img src=".github/images/send.png" width="760" alt="发件页">

### 提取码 · 二维码与链接一键复制

<img src=".github/images/share-code.png" width="700" alt="分享成功">

### 收件箱 · 投件码与取件码分离

<img src=".github/images/collection.png" width="760" alt="创建收件箱">

### 直连快传 · P2P 互传与房间聊天

<img src=".github/images/direct.png" width="760" alt="直连快传房间">

### 管理后台 · 仪表盘与在线配置

<img src=".github/images/admin.png" width="760" alt="管理后台仪表盘">

<img src=".github/images/admin-settings.png" width="760" alt="系统设置">

### 移动端

<img src=".github/images/mobile-home.png" width="300" alt="移动端首页">

---

## 快速开始

### Docker（推荐）

```bash
docker run -d --restart always -p 12345:12345 \
  -v /opt/FileCodeBox:/app/data \
  -e JWT_SECRET="$(openssl rand -hex 32)" \
  -e ENCRYPTION_KEY="$(openssl rand -hex 32)" \
  --name filecodebox onlypeng/filecodebox:latest
```

国内拉取缓慢时可换镜像：`docker.cnb.cool/aixk/filecodebox`。

打开 `http://<服务器IP>:12345`。管理后台在 `/#/admin`，默认口令 `FileCodeBox2023`，**首次登录会强制修改**。

### docker-compose

```yaml
services:
  filecodebox:
    image: onlypeng/filecodebox:latest
    ports: ["12345:12345"]
    volumes: ["./data:/app/data"]
    environment:
      JWT_SECRET: ${JWT_SECRET}
      ENCRYPTION_KEY: ${ENCRYPTION_KEY}
```

### 手动运行

```bash
git clone https://github.com/onlypeng/FileCodeBox.git
cd FileCodeBox
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Linux/macOS: source .venv/bin/activate && pip install -r requirements.txt
.venv/Scripts/python.exe main.py
```

后端单端口同时提供 API 与前端页面，仓库已内置前端构建产物 `frontend_dist`，无需额外构建。

---

## 文档

| 文档 | 内容 |
|---|---|
| [快速开始](./docs/guide/getting-started.md) | 部署方式、环境变量、反向代理、多实例前置条件 |
| [配置说明](./docs/guide/configuration.md) | 全部后台配置项与取值 |
| [文件分享](./docs/guide/share.md) / [文件上传](./docs/guide/upload.md) | 分享、过期、分片、预签名 |
| [收件箱](./docs/guide/collection.md) | 三码机制与投递流程 |
| [聊天与直连](./docs/guide/realtime.md) | WebRTC、房间缓存、部署注意事项 |
| [存储](./docs/guide/storage.md) | S3 / OneDrive / WebDAV / OpenDAL |
| [安全](./docs/guide/security.md) | 口令、限流、白名单、病毒扫描 |
| [管理面板](./docs/guide/management.md) | 后台各模块操作 |
| [API 参考](./docs/api/index.md) | 完整接口说明 |
| [变更日志](./docs/changelog.md) | 版本变更 |

技术栈：Python 3.12 + FastAPI + Tortoise ORM，Vue 3 + Vite + TypeScript，SQLite 开箱即用（可切 PostgreSQL / MySQL，配 Redis 可水平扩展）。

> 本项目基于 [vastsa/FileCodeBox](https://github.com/vastsa/FileCodeBox) 继续开发，文档与代码以**当前仓库实现**为准。

---

## 免责声明

本项目仅供合法合规的文件分享使用。使用者应遵守所在地区法律法规，不得用于传播违法、侵权内容。作者不对使用者的行为及由此产生的后果承担责任。

部署到公网时请务必：修改默认管理员口令、配置 `JWT_SECRET` 与 `ENCRYPTION_KEY`、启用 HTTPS、按需配置文件类型白名单与病毒扫描。

---

<div align="center">

基于 [vastsa/FileCodeBox](https://github.com/vastsa/FileCodeBox) · License: LGPL-3.0 · 详见 [LICENSE](./LICENSE)

</div>
