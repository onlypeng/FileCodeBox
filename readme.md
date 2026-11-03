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
  lanol/filecodebox:latest
```

</div>

---

## 目录

- [这是什么](#这是什么)
- [当前能力速览](#当前能力速览)
- [快速开始](#快速开始)
- [环境变量](#环境变量)
- [多实例部署](#多实例部署)
- [反向代理](#反向代理)
- [使用指南](#使用指南)
- [命令行示例（curl）](#命令行示例curl)
- [API 概览](#api-概览)
- [管理后台](#管理后台)
- [系统配置](#系统配置)
- [开发指南](#开发指南)
- [常见问题](#常见问题)
- [免责声明](#免责声明)

---

## 这是什么

FileCodeBox 是一个**自托管的轻量文件快递柜**：发件人拿到一串 6 位提取码，把它给收件人，对方输入码即可取件。没有账号体系，没有注册流程。

后端是 FastAPI + Tortoise ORM（默认 SQLite，可切 PostgreSQL / MySQL），前端是 Vue 3 + Vite。**后端单端口同时提供 API 与前端静态产物**，所以最小部署只需要一个进程、一个端口、一个数据目录。

除了基础的文本/文件分享，当前版本内置了收件箱（文件收集）、跨设备聊天、直连快传（WebRTC P2P + 服务器中转）、房间文件缓存、审计日志与可插拔病毒扫描钩子。

> 本项目基于 [vastsa/FileCodeBox](https://github.com/vastsa/FileCodeBox) 继续开发。文档与代码以**当前仓库实现**为准；若与其它分支的说明不一致，以代码为准并欢迎提 issue。

### 典型场景

| 场景 | 对应能力 |
|------|---------|
| 临时传个文件给同事 | 提取码分享，按时间/次数自动过期 |
| 发一段配置、日志、代码片段 | 纯文本分享（不上传文件，只提交备注内容） |
| 收作业 / 收材料 | 收件箱：别人凭投件码上传，你用管理码统一查看下载 |
| 两台设备互传大文件 | 直连快传：WebRTC P2P，服务器中转兜底且不落盘 |
| 对方不在线也要能拿到 | 房间缓存：文件暂存服务端，对方上线后拉取 |
| 担心提取码被转发 | 二次口令（提取口令），走另一条信道告知 |
| 企业内网私有部署 | 全功能自托管，数据留在自己的磁盘/对象存储里 |

---

## 当前能力速览

| 领域 | 已具备 |
|------|--------|
| 分享 | 单文件 / 多文件 / 纯文本、时间型与次数型过期、有效期续期、撤销分享 |
| 取件 | 提取码查询、只读详情刷新、二次口令、下载签名链接、断点续传（Range/206）、SHA-256 下载校验 |
| 上传 | 整包上传、分片上传（断点续传）、S3 预签名直传、服务端权威哈希秒传 |
| 收件箱 | 创建/编辑/关闭、投件码与取件码分离、分片投递、打包下载、后台管理 |
| 实时 | 跨设备聊天、直连快传房间、房间消息持久化、WebRTC 信令、房间文件缓存与配额 |
| 管理 | 仪表盘、文件/房间/收件箱管理、在线配置、审计日志、TURN 连通性自检 |
| 安全 | PBKDF2 管理员口令、JWT、IP 失败限流、敏感配置加密、可插拔病毒扫描、强制首次改密 |
| 存储 | 本地 / S3 兼容 / OneDrive / WebDAV / OpenDAL（可插拔） |
| 部署 | 单端口、Docker、SQLite 开箱即用；PostgreSQL/MySQL + Redis 可水平扩展 |
| 界面 | 中文简/繁/英三语、深浅色主题、移动端适配 |

---

## 快速开始

### Docker 部署（推荐）

```bash
docker run -d --restart always -p 12345:12345 \
  -v /opt/FileCodeBox:/app/data \
  -e JWT_SECRET="$(openssl rand -hex 32)" \
  -e ENCRYPTION_KEY="$(openssl rand -hex 32)" \
  --name filecodebox lanol/filecodebox:latest
```

国内拉取缓慢时可换镜像：`docker.cnb.cool/aixk/filecodebox`。

打开 `http://<服务器IP>:12345` 即可使用。管理后台在 `/#/admin`，**默认管理员口令 `FileCodeBox2023`，首次部署请立刻修改**。

### docker-compose（带 PostgreSQL + Redis 的多实例形态）

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
      # 数据库类型为后台配置项（dbType），需要 postgres/mysql 才能多进程并发写
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

> **不要照抄 `WORKERS=2` 就直接用**：必须同时满足「数据库可并发写」与「集群已启用」两个条件，详见[多实例部署](#多实例部署)。条件不齐时启动会打 ERROR，且房间消息会出现"只有一半人收到"这种极难排查的现象。

### 手动部署（前后端一体）

```bash
# 1. 克隆后端仓库（已内置前端构建产物 frontend_dist）
git clone https://github.com/onlypeng/FileCodeBox.git
cd FileCodeBox

# 2. 创建虚拟环境并安装依赖
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # Linux/macOS

# 3. 启动（默认 0.0.0.0:12345）
.venv/Scripts/python.exe main.py
```

前端源码在**另一个仓库**（[FileCodeBoxFronted](https://github.com/onlypeng/FileCodeBoxFronted)）。只有需要改前端时才需要它；日常部署直接用后端仓库里的 `frontend_dist` 即可。

本地开发时可以前后端分离运行：前端 `npm run dev`（5173）通过 Vite 代理把 `/share`、`/admin` 等请求转发到后端 `12345`。

---

## 环境变量

| 变量 | 默认值 | 生效范围 | 说明 |
|------|--------|---------|------|
| `HOST` | `0.0.0.0` | 仅 Docker CMD | 传给 `uvicorn --host`；手动启动时用配置项 `serverHost` |
| `PORT` | `12345` | 仅 Docker CMD | 传给 `uvicorn --port`；手动启动时用配置项 `serverPort` |
| `LOG_LEVEL` | `info` | 仅 Docker CMD | uvicorn 日志级别 |
| `WORKERS` | `1` | Docker CMD + 运行时能力判定 | 工作进程数。**默认请保持 1**；调大前必须满足多实例的两个前置条件 |
| `JWT_SECRET` | 空 | 应用 | JWT 签名密钥。留空时从管理员口令派生，**改管理员密码会使已登录 Token 失效**；生产环境建议显式配置 |
| `ENCRYPTION_KEY` | 空 | 应用 | S3/WebDAV 等敏感配置的加密密钥。留空时从管理员口令派生，生产环境建议显式配置 |
| `REDIS_URL` | 空 | 应用 | 多实例协调的 Redis 连接串，如 `redis://127.0.0.1:6379/0`。留空 = 单实例，无需任何外部依赖 |

生成随机密钥：`openssl rand -hex 32`。

> `HOST` / `PORT` / `LOG_LEVEL` 是 Dockerfile 里 `CMD` 的 shell 变量，只在使用镜像时生效；直接跑 `python main.py` 时由 `core/settings.py` 的 `serverHost` / `serverPort` 决定。

---

## 多实例部署

默认形态是**单实例**：房间 WebSocket 连接表与房间配额锁在进程内存里，SQLite 也是单写者。这是刻意选的保守路线——零外部依赖、行为可预测。

把 `WORKERS` 调大，或起多个容器做负载均衡之前，**两个条件必须同时满足**：

| 条件 | 为什么 | 怎么确认 |
|------|--------|---------|
| 数据库改为 `postgres` / `mysql` | SQLite 单写者，多进程并发写会冲突 | 后台「系统配置」→ `dbType`，改完重启 |
| 配置 `REDIS_URL`（或后台 `redisUrl`） | 房间成员、文本信令、配额锁需要跨进程共享 | 启动日志 `集群协调: Redis 模式` |

启动日志会如实打印结论，不会制造"看起来支持"的假象：

```
集群协调: 本地模式（单实例），未配置 redisUrl
应用初始化完成
部署形态: 数据库=sqlite 多进程支持=False
```

条件满足时：

```
集群协调: Redis 模式 instance=8f3c1a2b9d4e channel=filecodebox:cluster
部署形态: 数据库=postgres 多进程支持=True
```

若 `WORKERS > 1` 但条件不齐，启动会打 `ERROR` 并要求修正配置。

### 三条必须知道的约束

1. **房间文件中转需要会话亲和**。文本信令（成员上下线、文件卡片、WebRTC 协商）跨实例同步，但**文件分片的二进制帧刻意不跨实例转发**：单帧上限 256KB，走 pub/sub 会因 base64 放大约 1.4 倍且延迟不可控。请在负载均衡上按房间码做一致性哈希/粘性会话。纯 P2P 直连不受此限制；未做亲和时发送方会收到明确的 `relay_failed` 提示，而不是静默卡住。
2. **IP 限流按实例计数**，实际阈值约为「配置值 × 实例数」，属近似放宽，不影响正确性。
3. **Redis 只是协调件，不是主链路**。运行抖动会丢部分文本信令（成员快照每 5 秒自愈）；加锁失败降级为进程内锁（宁可极小概率超配额，也不让上传整体不可用）；启动时连不上则记 error 并回落单实例。

多实例需要额外安装 `pip install redis`（默认单实例不需要，故未固定进 `requirements.txt`）。

---

## 反向代理

Nginx 示例（关键是转发头与 WebSocket 升级）：

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

容器默认只信任回环地址的转发头（`--forwarded-allow-ips "127.0.0.1"`）。部署在反代之后时，需把代理地址加入白名单，并在后台配置 `trustedProxies`，否则 IP 限流拿到的是网关地址。

---

## 使用指南

### 发件与取件

1. 首页选择文件（可多选）或直接填写文本备注，设置有效期，提交后得到 6 位提取码
2. 收件人输入提取码 → 查看文件信息 → 下载

有效期支持 `day` / `hour` / `minute` / `count`（下载次数）/ `forever`。

### 提取口令（二次口令）

提取码常在聊天、邮件里明文流转，泄露即等于文件泄露。发件时可额外设置一个 2–32 位**提取口令**，通过另一条信道（口头、电话）告知：

- 上传时随 `access_code` 一起提交；服务端只存 PBKDF2 哈希，不存明文
- 取件时未带口令 → `403 该分享需要提取口令`，前端弹窗补输后重试
- 口令错误 → `403 提取口令错误`，**不扣取件次数**（否则猜几次就能把别人额度烧光），但计入失败限流并写审计
- 只读详情接口对未通过口令者只返回「需要口令」与过期状态，不下发文件名、大小、哈希

分片上传、S3 预签名直传同样支持：口令在「完成上传」阶段提交，不需要在上传会话里传递。

### 收件箱（文件收集）

创建收件箱后得到两组码：**投件码**给提交者上传，**取件码 / 管理码**给收集者查看与打包下载。支持设置最大文件数、有效期，投件支持分片。

### 直连快传（WebRTC）

进入房间后，优先尝试 P2P 直连；不可用时走服务器中转（**二进制帧不落盘**）。房间内可发消息、传文件、共享摄像头画面。若对方离线，可用房间文件缓存把文件暂存到服务端（默认 72 小时，单文件与房间级配额均可配）。

### 撤销分享

发件人持发起者凭证（`owner_token`）可撤销自己的分享。服务端会先检查物理文件是否被其它记录引用（秒传/去重会共享文件），有引用则只删记录不删文件。

---

## 命令行示例（curl）

<details>
<summary><b>展开 curl 示例</b></summary>

**上传文件**（默认 1 天）

```bash
curl -X POST "http://localhost:12345/share/file/" -F "file=@./a.txt"
```

**指定有效期 / 次数 / 多文件 / 纯文本 / 提取口令**

```bash
curl -X POST "http://localhost:12345/share/file/" \
  -F "file=@./a.txt" -F "expire_value=1" -F "expire_style=hour"

curl -X POST "http://localhost:12345/share/file/" \
  -F "file=@./a.txt" -F "expire_value=10" -F "expire_style=count"

curl -X POST "http://localhost:12345/share/file/" \
  -F "files=@a.txt" -F "files=@b.txt" -F "remark=批量文件"

# 纯文本分享：不传文件，只传 remark
curl -X POST "http://localhost:12345/share/file/" -F "remark=要分享的文本内容"

# 带二次口令
curl -X POST "http://localhost:12345/share/file/" \
  -F "file=@./a.txt" -F "access_code=pass2026"
```

**取件**

```bash
# 只读详情（不消耗取件次数）
curl "http://localhost:12345/share/select/info/?code=ABCD12"

# 正式取件（消耗次数，返回带签名 key 的下载地址）
curl -X POST "http://localhost:12345/share/select/" \
  -H "Content-Type: application/json" -d '{"code":"ABCD12"}'

# 带口令
curl -X POST "http://localhost:12345/share/select/" \
  -H "Content-Type: application/json" \
  -d '{"code":"ABCD12","access_code":"pass2026"}'

# 下载（用 select 返回的 key）
curl -L "http://localhost:12345/share/download?code=ABCD12&key=<key>" -o a.txt
```

**其它**

```bash
# 识别码类型（文件码/管理码/投件码/取件码/房间码）
curl "http://localhost:12345/share/check_code/?code=ABCD12"

# 撤销（需发起者凭证）
curl -X POST "http://localhost:12345/share/revoke" \
  -H "Content-Type: application/json" \
  -d '{"code":"ABCD12","owner_token":"<owner_token>"}'
```

业务响应统一为：

```json
{ "code": 200, "message": "ok", "detail": { "code": "ABCD12", "name": "a.txt" } }
```

**管理员关闭游客上传后**（`openUpload=0`）：

```bash
TOKEN=$(curl -s -X POST "http://localhost:12345/admin/login" \
  -H "Content-Type: application/json" \
  -d '{"password":"FileCodeBox2023"}' | python -c "import sys,json;print(json.load(sys.stdin)['detail']['token'])")

curl -X POST "http://localhost:12345/share/file/" \
  -H "Authorization: Bearer $TOKEN" -F "file=@./a.txt"
```

| `expire_style` | 含义 |
|----------------|------|
| `day` / `hour` / `minute` | 按时间过期 |
| `count` | 下载指定次数后过期 |
| `forever` | 永不过期 |

</details>

---

## API 概览

> 完整接口说明见 [docs/api](./docs/api/index.md)。所有业务响应统一为 `{ "code": 200, "message": "ok", "detail": {...} }`；`410` 表示已过期。

| 前缀 | 主要端点 | 说明 |
|------|---------|------|
| `/share` | `POST /file/`、`GET /check_code/`、`GET /select/info/`、`POST /select/`、`POST /revoke`、`GET /download`、`GET /download/item/{id}`、`GET /zip/{code}` | 分享与取件（含二次口令、多文件、打包下载） |
| `/chunk` | `POST /upload/init/`、`POST /upload/`、`DELETE /upload/{id}`、`POST /complete/` | 分片上传（断点续传） |
| `/presign` | `POST /upload/init`、`PUT /upload/proxy/{id}`、`POST /upload/confirm/{id}`、`DELETE /upload/{id}` | S3 预签名直传 / 代理上传 |
| `/collection` | `POST /create/`、`PATCH /update/{code}`、`GET /status/{code}`、`GET /manage/{code}`、`GET /retrieve/{code}`、`GET /download/{id}`、`GET /zip/{code}`、`DELETE /delete/{id}` | 收件箱 |
| `/delivery` | `POST /upload/`、`POST /chunk/init|complete|cancel/`、`GET /{delivery_code}` | 收件箱投件 |
| `/room` | 房间管理端点 | 实时房间 |
| `/ws` | `/ws/collection/{code}`、`/ws/room/{room_code}`、`/ws/direct/{room_code}`、`/ws/transfer/{room_code}` | WebSocket 实时通道 |
| `/admin` | `POST /login`、`GET /verify`、`GET /dashboard`、`GET|PATCH /file/*`、`GET|PATCH /room/*`、`GET /collection/*`、`GET /audit`、`PATCH /config/update`、`GET /network/turn-check` | 管理后台（除 login 外全部需要 Bearer Token） |

---

## 管理后台

访问 `/#/admin`（或 `/#/login`），默认口令 `FileCodeBox2023`，**首次登录会被强制要求修改**。

| 模块 | 能力 |
|------|------|
| 仪表盘 | 文件数、存储占用、在线房间、过期趋势 |
| 文件管理 | 列表、详情、修改有效期、续期、删除 |
| 收件箱管理 | 收件箱列表、投件文件查看、删除、续期 |
| 房间管理 | 房间列表、缓存文件、踢人、删除、续期 |
| 系统配置 | 全量在线配置（数据库/Redis 等基础设施项除外） |
| 审计日志 | 登录、配置变更、删除、撤销等关键操作留痕 |
| 网络自检 | TURN 服务器连通性检查 |

**为什么数据库与 Redis 配置不进后台白名单**：这类基础设施配置改了不会在当前进程生效（连接池已经建立），在后台改只会让人误以为已切换。它们通过环境变量/配置文件设定并重启生效。

---

## 系统配置

后台「系统配置」可在线修改，改完保存即生效（结构性配置除外）。常用项：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `openUpload` | `1` | 是否允许游客上传；`0` 时上传需管理员 Token |
| `uploadSize` | `104857600` | 单文件大小上限（字节），默认 100MB |
| `expireStyle` | `day/hour/minute/forever/count` | 可选过期方式 |
| `enableChunk` / `uploadChunkSize` | `0` / `5` | 分片上传开关与分片大小（MB） |
| `codeLength` | `6` | 提取码长度 |
| `maxMultiFileCount` | `20` | 单次多文件分享上限 |
| `errorMinute` / `errorCount` | `1` / `10` | 失败限流窗口与次数 |
| `roomCacheEnabled` / `roomCacheDefaultHours` / `roomCacheMaxSize` / `roomCacheRoomQuota` | `1` / `72` / `100` / `500` | 房间缓存开关、默认时长（小时）、单文件上限（MB）、房间配额（MB） |
| `globalStorageQuotaMB` | `0` | 全局存储配额，0 = 不限制 |
| `auditLogRetentionDays` | `90` | 审计日志保留天数 |
| `virusScanEnabled` / `virusScanHook` / `virusScanTimeoutSec` / `virusScanFailOpen` | `false` / 空 / `30` / `true` | 可插拔病毒扫描钩子 |
| `directRelayEnabled` / `directRelayChunkSize` / `directMaxRelaySize` | `1` / `64` / `0` | 服务器中转开关、分片大小（KB）、单文件上限（0=不限） |
| `shareDomain` | 空 | 生成分享链接时使用的域名 |
| `trustedProxies` | 空 | 可信代理地址，反代部署时配置 |
| `cors_origins` | `http://localhost:5173,http://127.0.0.1:5173` | 允许的跨域来源 |
| `dbType` / `dbHost` / `dbPort` / `dbName` / `dbUser` / `dbPassword` / `dbSchema` | `sqlite` / … | 数据库连接（**改完需重启**） |
| `redisUrl` | 空 | 集群协调 Redis 连接串（**改完需重启**） |

### 病毒扫描钩子

不内置扫描引擎，但留出接口：上传落盘后回调外部扫描服务（本地命令或 HTTP），命中即拒绝。

| 项 | 说明 |
|----|------|
| `virusScanHook` | `cmd:<可执行文件路径> <参数...>` 或 `http://...` 的 multipart 接口 |
| 返回码约定 | 命令：0 = 干净，1 = 命中，其它 = 扫描失败；HTTP：JSON `{ "infected": true/false }` |
| `virusScanFailOpen` | 扫描器不可用时是否放行，默认 `true`（扫描是辅助能力，不该因扫描器挂掉让上传整体不可用） |

命令参数以列表方式执行、**不经 shell**，避免文件名被拼进命令行造成注入。

---

## 开发指南

### 两个仓库

```
FileCodeBox/          后端 FastAPI（master）       ← 本仓库
FileCodeBoxFronted/   前端 Vue 3 + Vite（main）    ← 独立仓库，无统一父仓库
```

后端 serve `FileCodeBox/frontend_dist`，**该目录必须入库**（Dockerfile 直接 COPY 它）。前端构建产物需要同步回后端仓库，否则镜像里会是旧界面。

### 本地开发

```bash
# 终端 1：后端（工作目录必须是 FileCodeBox/，迁移扫描依赖相对路径 apps/）
cd FileCodeBox
.venv/Scripts/python.exe main.py

# 终端 2：前端
cd FileCodeBoxFronted
npm install
npm run dev        # 5173，Vite 代理转发到 12345
```

### 构建与同步

```bash
cd FileCodeBoxFronted
npm run build      # clean-dist → 架构边界检查 → vue-tsc → vite build，约 40~100s

cd ..
python .workbuddy/sync_dist.py   # 覆盖式复制 dist → FileCodeBox/frontend_dist
git -C FileCodeBox add frontend_dist
```

`npm run build` 已内置构建前清理（`scripts/clean-dist.mjs`），解决 `emptyOutDir:false` 导致的产物累积。同步后请确认入口 chunk 已入库——Dockerfile 依赖它。

### 验证

```bash
cd FileCodeBox
.venv/Scripts/python.exe -m pytest -q          # 后端测试
```

端到端验证脚本在 `.workbuddy/`（`e2e_server.py` 起临时服务、`e2e_test.py` 断言），每次使用全新临时数据目录，不会碰真实 `data/`。

### 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、FastAPI、Tortoise ORM、SQLite / PostgreSQL / MySQL |
| 前端 | Vue 3、Vite 7、TypeScript、Pinia、vue-i18n、Tailwind CSS |
| 实时 | WebSocket、WebRTC（P2P + TURN） |
| 存储 | 本地 / S3 / OneDrive / WebDAV / OpenDAL |
| 集群 | Redis pub/sub（可选） |

注解与文档规范见 [docs/contributing.md](./docs/contributing.md)。核心一条：**注解写「为什么」，不写「做了什么」**；改了接口/配置，同一提交里必须同步文档。

---

## 常见问题

**Q：忘记管理员口令怎么办？**
在数据库 `keyvalue` 表的 `settings` 记录里重置 `admin_token`，或删除数据目录重新初始化（会丢失全部数据）。生产环境请务必配置 `JWT_SECRET` 与 `ENCRYPTION_KEY` 并备份。

**Q：上传大文件失败？**
检查 `uploadSize`（默认 100MB）、反向代理的 `client_max_body_size`、以及是否开启分片上传（`enableChunk=1`）。

**Q：下载链接过期 / 提示 410？**
取件返回的下载地址带签名 `key`，有时效。请重新取件获取新链接；若分享本身已过期（`count` 用尽或时间到），则无法恢复。

**Q：为什么改了管理员密码后所有人被登出？**
未配置 `JWT_SECRET` 时，签名密钥由管理员口令派生，改口令即等于换密钥。配置 `JWT_SECRET` 可避免。

**Q：多实例下文件传到一半卡住？**
房间文件分片不跨实例转发，需要在负载均衡上按房间码做会话亲和。详见[多实例部署](#多实例部署)。

**Q：秒传为什么没生效？**
文件哈希一律由服务端落盘时流式计算，不采信客户端声明值（防缓存投毒）。相同内容才会命中秒传/去重。

**Q：数据存在哪里？**
默认 `data/` 目录（Docker 部署时映射到 `/app/data`）。切换 S3/WebDAV 后在后台配置对应凭据。

---

## 免责声明

本项目仅供合法合规的文件分享使用。使用者应遵守所在地区法律法规，不得用于传播违法、侵权内容。作者不对使用者的行为及由此产生的后果承担责任。

部署到公网时请务必：修改默认管理员口令、配置 `JWT_SECRET` 与 `ENCRYPTION_KEY`、启用 HTTPS、按需配置文件类型白名单与病毒扫描。

---

<div align="center">

基于 [vastsa/FileCodeBox](https://github.com/vastsa/FileCodeBox) · License: LGPL-3.0 · 详见 [LICENSE](./LICENSE)

</div>
