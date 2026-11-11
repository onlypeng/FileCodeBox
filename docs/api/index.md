# API 参考

> 本页按**当前仓库实际注册的路由**整理（后端 `apps/base/views.py`、`apps/admin/views.py`）。
> 若与其它资料不一致，以代码为准。路由前缀在 `main.py` 中挂载。

## 约定

### 响应格式

所有业务响应统一为：

```json
{ "code": 200, "message": "ok", "detail": { } }
```

`detail` 的内容随接口变化。文件下载类接口直接返回二进制流，不套这层结构。

### 状态码与业务码

| 值 | 含义 |
|----|------|
| `200` | 成功 |
| `400` | 参数错误（过期类型非法、备注超限等） |
| `403` | 鉴权失败（取件码不匹配、提取口令缺失/错误、无管理员权限） |
| `404` | 资源不存在 |
| `410` | 分享已过期（时间到或次数用尽）——与 404 语义不同，不要合并处理 |
| `423` | 触发失败限流 |

### 鉴权

| 方式 | 适用范围 |
|------|---------|
| 无需鉴权 | 取件、投件、房间只读信息（受 `openUpload` 影响） |
| `Authorization: Bearer <JWT>` | 所有 `/admin/*`（`POST /admin/login` 除外）；`openUpload=0` 时的上传接口 |
| 取件码 `code` | 取件、下载 |
| 提取口令 `access_code` | 设置了二次口令的分享 |
| `owner_token` | 撤销分享、发起者查看自己的分享详情 |
| 成员令牌 | 房间写接口（要求「当前在线 **或** 持有成员令牌」） |

### 有效期

`expire_style` 可选：`day` / `hour` / `minute` / `count`（下载次数）/ `forever`。
`expire_value` 为对应数值；`count` 模式下为次数。

---

## 分享与取件 `/share`

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/share/file/` | 上传文件 / 多文件 / 纯文本分享 |
| `GET` | `/share/check_code/` | 识别码类型（文件码/管理码/投件码/取件码/房间码） |
| `GET` | `/share/select/` | 查询分享信息（不消耗取件次数） |
| `GET` | `/share/select/info/` | 只读详情 + 实时剩余次数，供记录刷新 |
| `POST` | `/share/select/` | 正式取件（**消耗取件次数**），返回带签名 `key` 的下载地址 |
| `POST` | `/share/revoke` | 撤销分享（需 `owner_token`） |
| `GET` | `/share/download` | 下载单文件（`code` + `key`） |
| `GET` | `/share/download/item/{item_id}` | 下载多文件分享中的单个文件 |
| `GET` | `/share/zip/{code}` | 打包下载（单文件分享同样支持） |

### `POST /share/file/`

一次接口同时承载三种分享方式，靠「有没有文件」区分：

| 场景 | 参数 |
|------|------|
| 单文件 | `file` + 可选 `remark` |
| 多文件 | 多个 `files` + 可选 `remark`（上限 `maxMultiFileCount`，默认 20） |
| **纯文本** | **只传 `remark`，不传任何文件** |

> 历史文档里出现过的 `/share/text/` **并不存在**。纯文本分享走的就是 `/share/file/`，
> 只是没有文件字段；内容由 `remark` 承载，长度受 `maxTextLength`（默认 200000 字符）约束。

通用表单字段：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `expire_value` | int | `1` | 有效期数值 |
| `expire_style` | str | `day` | `day` / `hour` / `minute` / `count` / `forever` |
| `remark` | str | `""` | 备注；无文件时即为分享内容本身 |
| `access_code` | str | 空 | 提取口令（2–32 位），服务端只存 PBKDF2 哈希 |

```bash
curl -X POST "http://localhost:12345/share/file/" \
  -F "file=@./a.txt" \
  -F "expire_value=1" -F "expire_style=day" \
  -F "access_code=pass2026"

# 纯文本分享
curl -X POST "http://localhost:12345/share/file/" -F "remark=要分享的文本内容"
```

响应 `detail`：`{ "code": "ABCD12", "owner_token": "..." }`。
`owner_token` 是发起者凭证，请自行保存，撤销分享与查看自己的分享详情需要它。

### `POST /share/select/`

请求体（JSON）：

```json
{ "code": "ABCD12", "access_code": "pass2026" }
```

行为要点：

- 未通过提取口令时返回 `403`，**不消耗取件次数**（避免猜错即成拒绝服务），但计入失败限流并写审计
- 口令保护的分享，响应里会带 `access_key`；后续 `/share/download/item` 与 `/share/zip` 必须带上它
  才能下载（这两个入口原本只认取件码，会被用来绕开口令）
- 次数型分享在通过校验后才扣次数

### `GET /share/select/info/`

只读查询，**不消耗取件次数**，返回结构与 `select` 一致并附 `expired_count`。
对未通过提取口令的调用方**只返回「需要口令」与过期状态**，不下发文件名、大小、哈希；
发起者带 `owner_token` 可看全量。

### `GET /share/download`

`?code=...&key=<select 返回的签名 key>`。支持 HTTP Range（206 断点续传，由 Starlette `FileResponse` 提供）。
签名 `key` 有时效，过期请重新取件。

---

## 分片上传 `/chunk`

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/chunk/upload/init/` | 创建上传会话，返回 `upload_id` 与分片参数 |
| `POST` | `/chunk/upload/chunk/{upload_id}/{chunk_index}` | 上传单个分片 |
| `GET` | `/chunk/upload/status/{upload_id}` | 查询已上传分片，用于断点续传 |
| `POST` | `/chunk/upload/complete/{upload_id}` | 合并分片并生成分享码 |
| `DELETE` | `/chunk/upload/{upload_id}` | 放弃会话，清理临时分片 |

分片的**提取口令、有效期、备注在 `complete` 阶段提交**，不需要在上传会话里传递。
合并时服务端流式计算权威 SHA-256，不采信客户端声明值（防缓存投毒）。

## 预签名上传 `/presign`

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/presign/upload/init` | 创建直传会话 |
| `PUT` | `/presign/upload/proxy/{upload_id}` | 代理上传（服务端转存到存储后端） |
| `POST` | `/presign/upload/confirm/{upload_id}` | 客户端完成 S3 直传后确认，换取分享码 |
| `GET` | `/presign/upload/status/{upload_id}` | 查询会话状态 |
| `DELETE` | `/presign/upload/{upload_id}` | 取消会话 |

同样支持 `access_code` / `expire_value` / `expire_style` / `remark`（在 proxy 或 confirm 阶段）。

---

## 收件箱 `/collection` 与投递 `/delivery`

### 收件箱

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/collection/create/` | 创建收件箱，返回投件码 / 取件码 / 管理码 |
| `PATCH` | `/collection/update/{collection_code}` | 修改设置（最大文件数、有效期等） |
| `GET` | `/collection/status/{collection_code}` | 状态查询 |
| `GET` | `/collection/manage/{collection_code}` | 管理视角列表（管理码） |
| `GET` | `/collection/manage-by-delivery/{delivery_code}` | 凭投件码查管理信息 |
| `GET` | `/collection/retrieve/{retrieve_code}` | 取件视角列表（取件码） |
| `GET` | `/collection/download/{file_id}` | 下载单个投件 |
| `GET` | `/collection/zip/{code}` | 打包下载 |
| `DELETE` | `/collection/delete/{file_id}` | 删除投件 |

### 投递

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/delivery/{delivery_code}` | 获取投件页信息 |
| `POST` | `/delivery/upload/` | 整包投递 |
| `POST` | `/delivery/chunk/init/` | 分片投递初始化 |
| `POST` | `/delivery/chunk/complete/` | 分片投递完成 |
| `POST` | `/delivery/chunk/cancel/` | 取消分片投递 |

投件码与取件码分离：提交者拿投件码只能上传，收集者拿管理码/取件码才能查看和打包。

---

## 实时房间 `/room` 与 `/ws`

### HTTP

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/room/create/` | 创建房间 |
| `GET` | `/room/info/{room_code}` | 房间信息 |
| `GET` | `/room/history/{room_code}` | 历史消息（断线重连补拉） |
| `GET` | `/room/{room_code}/files` | 房间缓存文件列表 |
| `POST` | `/room/{room_code}/file/upload` | 上传缓存文件（受配额约束） |
| `GET` | `/room/{room_code}/file/{transfer_id}/link` | 换签名下载链接 |
| `GET` | `/room/{room_code}/file/{transfer_id}/download` | 下载缓存文件 |
| `DELETE` | `/room/{room_code}/file/{transfer_id}` | 删除缓存文件 |

### WebSocket

| 路径 | 用途 |
|------|------|
| `/ws/collection/{code}` | 收件箱实时推送 |
| `/ws/room/{room_code}` | 跨设备聊天 |
| `/ws/direct/{room_code}` | 直连快传 |
| `/ws/transfer/{room_code}` | 文件传输 |

房间写接口要求「当前在线 **或** 持有成员令牌」，令牌由 WS join 时服务端签发。

### 兼容端点

`POST /transfer/create/`、`POST /direct/create/`、`GET /transfer/info/{room_code}`、
`GET /direct/info/{room_code}` 保留供旧客户端使用。
**`/chat/*` 系列端点已下线**，聊天统一走 `/room` 与 `/ws/room/{room_code}`。

### 多实例下的限制

文本信令跨实例同步；**二进制文件分片刻意不跨实例转发**（单帧 ≤256KB，pub/sub 会放大约 1.4 倍且延迟不可控）。
接入层需按房间码做会话亲和，否则发送方会收到 `relay_failed`。
限流计数（取件码猜错、上传次数）都在进程内存里，多实例下实际阈值是「配置值 × 实例数」。
完整前置条件见[快速开始](/guide/getting-started)的多实例一节。

---

## 管理后台 `/admin`

> 除 `POST /admin/login` 外，全部需要 `Authorization: Bearer <JWT>`。
> `admin_api` 在 router 级别挂了 `admin_required` 依赖——**漏挂一处就是未授权访问漏洞，且功能测试不会暴露**。

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/admin/login` | 登录，返回 JWT（唯一免鉴权接口） |
| `GET` | `/admin/verify` | 校验 Token 有效性（只读，无副作用） |
| `POST` | `/admin/logout` | 退出（JWT 无状态，仅客户端丢弃 Token） |
| `GET` | `/admin/dashboard` | 仪表盘统计 |
| `GET` | `/admin/audit` | 审计日志 |
| `GET` | `/admin/network/turn-check` | TURN 连通性自检 |
| `GET` | `/admin/config/get` | 读取全量配置 |
| `PATCH` | `/admin/config/update` | 更新配置（白名单制） |
| `GET` | `/admin/file/list` | 文件列表 |
| `GET` | `/admin/file/{file_id}` | 文件详情 |
| `PATCH` | `/admin/file/{file_id}` | 修改文件 |
| `PATCH` | `/admin/file/{file_id}/expire` | 修改有效期 |
| `POST` | `/admin/file/extend` | 续期 |
| `DELETE` | `/admin/file/delete` | 删除文件 |
| `GET` | `/admin/collection/list` | 收件箱列表 |
| `GET` | `/admin/collection/{id}/files` | 收件箱投件列表 |
| `DELETE` | `/admin/collection/{id}` | 删除收件箱 |
| `PATCH` | `/admin/collection/{id}/expire` | 修改收件箱有效期 |
| `POST` | `/admin/collection/{id}/extend` | 收件箱续期 |
| `GET` | `/admin/room/list` | 房间列表 |
| `GET` | `/admin/room/{id}/files` | 房间缓存文件 |
| `DELETE` | `/admin/room/{id}` | 删除房间（跨实例踢人） |
| `DELETE` | `/admin/room/cache-file/{file_id}` | 删除房间缓存文件 |
| `PATCH` | `/admin/room/{id}` | 修改房间 |
| `PATCH` | `/admin/room/{id}/expire` | 修改房间有效期 |
| `POST` | `/admin/room/{id}/extend` | 房间续期 |

### 配置白名单

`PATCH /admin/config/update` 只接受白名单内的键。`dbType` / `dbHost` / `dbPort` /
`dbName` / `dbUser` / `dbPassword` / `dbSchema` / `redisUrl` **故意不进白名单**：
这类基础设施配置在进程运行中改不生效（连接池已建立），必须通过环境变量/配置文件
设定并重启。若提交的值与当前值不同，接口会明确返回 `400` 并列出需改的键；整份表单
原样回传（含脱敏占位符 `******`）不视为修改，不会阻断正常保存。

---

## 其它端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | 前端 SPA 页面（静态产物） |
| `POST` | `/` | 公共配置（前端启动时拉取） |
| `GET` | `/robots.txt` | 由 `robotsText` 配置生成 |

---

## 迁移说明

- 数据库迁移在每次启动时自动执行，位于 `apps/base/migrations/`，必须**幂等**。
- 新增迁移的函数名**必须是 `migrate()`**：执行器用 `hasattr(module, "migrate")` 判断，
  写成 `upgrade()` 会被**静默跳过**（日志照样打印"执行迁移"，但不做任何事），
  直到运行时报 `no such column` 才暴露。
- 当前最新迁移为 `migrations_033.py`（`FileCodes.access_code_hash`）。
