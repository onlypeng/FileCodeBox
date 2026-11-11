# 快速开始

## 简介

FileCodeBox 是单端口应用：一个进程同时提供 API 与预构建的前端页面，
`docker run` 一条命令就能跑起来，SQLite 开箱即用、零外部依赖。

## 部署方式

### Docker 部署（推荐）

#### 快速启动

```bash
docker run -d --restart=always -p 12345:12345 -v /opt/FileCodeBox/:/app/data --name filecodebox onlypeng/filecodebox:latest
```

#### Docker Compose

```yml
version: "3"
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

#### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOST` | `0.0.0.0` | 监听地址（仅 Docker 镜像 CMD 生效） |
| `PORT` | `12345` | 服务端口（仅 Docker 镜像 CMD 生效） |
| `WORKERS` | `1` | 工作进程数。**默认请保持 1**；调大前必须满足多实例的两个前置条件 |
| `LOG_LEVEL` | `info` | 日志级别：debug/info/warning/error |
| `JWT_SECRET` | 空 | JWT 签名密钥；留空时从管理员口令派生 |
| `ENCRYPTION_KEY` | 空 | 敏感配置加密密钥；留空时从管理员口令派生 |
| `REDIS_URL` | 空 | 多实例协调的 Redis 连接串；留空 = 单实例 |

::: tip 生产环境请显式设置 `JWT_SECRET` 与 `ENCRYPTION_KEY`
两者留空时由管理员口令派生，省事但有两个副作用：**修改管理员口令会让所有已登录 Token 立即失效**
（因为签名密钥跟着变了），且口令泄漏等于签名密钥泄漏。生成方式：`openssl rand -hex 32`。
:::

::: warning 多实例前置条件
`WORKERS > 1` 必须**同时**满足两条：数据库改为 `postgres`/`mysql`（SQLite 只允许单写者），
且配置 `REDIS_URL`（房间状态、配额锁需跨进程共享）。条件不齐时启动会打 ERROR，
房间消息则会出现"只有一半人收到"这类极难排查的现象。

即使条件齐备，仍有两条约束：

1. **二进制分片刻意不跨实例转发**。单帧 ≤256KB，走 pub/sub 会放大约 1.4 倍且延迟不可控，
   所以文件直传要求收发双方落在同一实例 → 接入层必须按房间码做**会话亲和**，
   否则发送方会收到 `relay_failed`。
2. **限流按实例计数**。`uploadRateLimitCount`、登录失败锁定等计数都在进程内存里，
   多实例下实际阈值是“配置值 × 实例数”。需要严格限流请前置 Nginx/WAF 按 IP 计数。
:::

### 配置反向代理（Nginx）

```nginx
location / {
    # WebSocket 升级转发（实时房间 /ws/room、收件箱实时推送 /ws/collection 必需，
    # 缺失时房间页会显示"已断开"、收件箱实时进度不更新）
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";

    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_pass http://localhost:12345;

    # WebSocket 长连接需要较长的读/写超时（默认 60s 会中断空闲房间连接）
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

::: tip CDN / 边缘加速不转发 WebSocket 时
部分 CDN（含 EdgeOne、ESA 一类默认配置）只代理 HTTP，`Upgrade` 请求会被吞掉。
此时房间页显示「已断开」而普通页面一切正常。不必换 CDN，去
**管理后台 → 系统设置 → WebSocket 独立接入地址** 填一个直连源站的域名即可：

```
wss://direct.example.com     # 允许 wss:// https:// 或裸域名 example.com
```

前端只在建立 WebSocket 时改用它，HTTP 请求仍走 CDN；值随公共配置运行时下发，
**改完刷新页面即生效，无需重新构建镜像**。留空或非法值自动回退同源。

排障顺序：先确认镜像里装上了 WS 协议实现（`docker exec <容器> python -c "import uvicorn, websockets"`），
再查反向代理是否转发 `Upgrade`，最后才怀疑 CDN。
:::

### 手动部署

```bash
git clone https://github.com/onlypeng/FileCodeBox.git
cd FileCodeBox
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Linux/macOS: source .venv/bin/activate && pip install -r requirements.txt
.venv/Scripts/python.exe main.py
```

仓库已内置前端构建产物 `frontend_dist`，后端单端口同时提供 API 与页面，无需额外构建。
改前端源码才需要 `npm run build` 后同步产物。


## 使用方法

1. 访问系统
   打开浏览器访问 `http://localhost:12345`

2. 上传文件
   - 点击上传按钮或拖拽文件到上传区域
   - 设置文件有效期和下载次数限制
   - 获取分享链接和提取码

3. 下载文件
   - 访问分享链接
   - 输入提取码
   - 下载文件

4. 后台管理
   - 访问 `http://localhost:12345/#/admin`
   - 输入管理员口令：`FileCodeBox2023`
   - 进入后台管理页面
   - 查看仪表盘、管理文件/收件箱/直连房间、修改系统配置

5. 更多功能
   - **收件箱**：创建收件箱，他人凭投件码上传文件，详见 [收件箱（文件收集）](/guide/collection)
   - **聊天与直连**：创建房间实时聊天或点对点传文件，详见 [聊天与直连传输](/guide/realtime)

## 下一步

- [文件上传](/guide/upload) - 普通/分片/预签名上传方式
- [收件箱（文件收集）](/guide/collection) - 文件收集与三码管理
- [聊天与直连传输](/guide/realtime) - 实时聊天与直连传输
- [存储配置](/guide/storage) - 了解如何配置不同的存储方式
- [安全设置](/guide/security) - 了解如何增强系统安全性
- [API 文档](/api/) - 了解如何通过 API 集成