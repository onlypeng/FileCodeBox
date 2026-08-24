# FileCodeBox API 文档

## API 版本: 2.1.0

## 目录
- [通用说明](#通用说明)
- [认证](#认证)
- [分享接口](#分享接口)
- [分片上传接口](#分片上传接口)
- [预签名上传接口](#预签名上传接口)
- [收件箱与投递接口](#收件箱与投递接口)
- [跨设备聊天接口](#跨设备聊天接口)
- [直连传输与直连快传接口](#直连传输与直连快传接口)
- [管理接口](#管理接口)
- [错误响应](#错误响应)
- [状态码说明](#状态码说明)

## 通用说明

所有业务接口的响应统一为以下结构：

```json
{
  "code": 200,
  "message": "ok",
  "detail": {}
}
```

- `code`：业务状态码（`200` 表示成功）
- `message`：状态描述
- `detail`：具体返回数据

部分接口（分享/上传类）在后台关闭游客上传（`openUpload=0`）时，需要携带管理员 Token 访问。

## 认证

部分接口需要在请求头中携带 `Authorization` 进行认证：

```
Authorization: Bearer <token>
```

### 获取 Token

```bash
curl -X POST "http://localhost:12345/admin/login" \
  -H "Content-Type: application/json" \
  -d '{"password": "FileCodeBox2023"}'
```

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "token": "xxx.xxx.xxx",
    "token_type": "Bearer"
  }
}
```

---

## 分享接口

### 分享文本

**POST** `/share/text/`

分享文本内容，获取分享码（单次不超过 222KB）。

**请求参数：**

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|-------|------|------|--------|------|
| text | string | 是 | - | 要分享的文本内容 |
| expire_value | integer | 否 | 1 | 过期时间值 |
| expire_style | string | 否 | "day" | 过期时间单位(day/hour/minute/count/forever) |

**cURL 示例：**

```bash
# 游客上传（openUpload=1 时）
curl -X POST "http://localhost:12345/share/text/" \
  -F "text=这是要分享的文本内容" \
  -F "expire_value=1" \
  -F "expire_style=day"

# 需要认证时（openUpload=0 时）
curl -X POST "http://localhost:12345/share/text/" \
  -H "Authorization: Bearer xxx.xxx.xxx" \
  -F "text=这是要分享的文本内容"
```

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "code": "ABC123"
  }
}
```

### 分享文件

**POST** `/share/file/`

上传并分享文件（支持单文件与多文件批量），获取分享码。

**请求参数：**

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|-------|------|------|--------|------|
| file | file | 否* | - | 单文件模式上传的文件 |
| files | file | 否* | - | 多文件模式，可重复提交多个 `files` |
| expire_value | integer | 否 | 1 | 过期时间值 |
| expire_style | string | 否 | "day" | 过期时间单位(day/hour/minute/count/forever) |
| remark | string | 否 | "" | 备注说明 |

> `file` 与 `files` 至少提供一种；多文件数量受后台 `maxSendFiles` 限制（默认 20）。

**cURL 示例：**

```bash
# 上传单个文件（默认1天有效期）
curl -X POST "http://localhost:12345/share/file/" \
  -F "file=@/path/to/file.txt"

# 指定 7 天有效期
curl -X POST "http://localhost:12345/share/file/" \
  -F "file=@/path/to/file.txt" \
  -F "expire_value=7" \
  -F "expire_style=day"

# 指定下载 10 次后过期
curl -X POST "http://localhost:12345/share/file/" \
  -F "file=@/path/to/file.txt" \
  -F "expire_value=10" \
  -F "expire_style=count"

# 多文件批量上传
curl -X POST "http://localhost:12345/share/file/" \
  -F "files=@a.txt" \
  -F "files=@b.txt" \
  -F "remark=批量文件"

# 需要认证时
curl -X POST "http://localhost:12345/share/file/" \
  -H "Authorization: Bearer xxx.xxx.xxx" \
  -F "file=@/path/to/file.txt"
```

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "code": "ABC123",
    "name": "example.txt",
    "is_multi_file": false
  }
}
```

### 检查码类型

**GET** `/share/check_code/`

自动识别码的类型：`file` / `collection` / `retrieve` / `delivery` / `chat` / `transfer` / `direct` / `unknown`。

**请求参数：**

| 参数名 | 类型 | 必填 | 描述 |
|-------|------|------|------|
| code | string | 是 | 任意 6 位码 |

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "type": "collection",
    "code": "ABC123",
    "title": "项目文件收集"
  }
}
```

### 取件（查询文件信息 / 下载）

**GET** `/share/select/`

单文件/文本码：直接返回文件内容或文本；多文件码：返回文件列表。

**请求参数：**

| 参数名 | 类型 | 必填 | 描述 |
|-------|------|------|------|
| code | string | 是 | 文件分享码 |

**cURL 示例：**

```bash
# 通过取件码下载文件
curl -L "http://localhost:12345/share/select/?code=ABC123" -o downloaded_file
```

**POST** `/share/select/`

以 JSON 返回文件/文本信息与下载地址（多文件返回子文件列表）。

**请求体：**

```json
{ "code": "ABC123" }
```

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "code": "ABC123",
    "name": "example.txt",
    "size": 1024,
    "text": "/share/download?key=xxx&code=ABC123",
    "created_at": "2026-08-11T10:00:00+08:00",
    "expired_at": "2026-08-18T10:00:00+08:00",
    "expire_style": "day",
    "expire_value": 7
  }
}
```

### 下载文件

**GET** `/share/download`

下载分享的文件（需要取件接口返回的签名 `key`）。

**请求参数：**

| 参数名 | 类型 | 必填 | 描述 |
|-------|------|------|------|
| key | string | 是 | 下载签名密钥 |
| code | string | 是 | 文件分享码 |

### 下载多文件中的单个文件

**GET** `/share/download/item/{item_id}`

**请求参数：**

| 参数名 | 类型 | 必填 | 描述 |
|-------|------|------|------|
| item_id | integer | 是 | 路径参数，文件项 ID |
| code | string | 是 | 多文件分享码 |

### 打包下载

**GET** `/share/zip/{code}`

将分享的文件（含多文件）打包为 ZIP 流式下载。

**请求参数：**

| 参数名 | 类型 | 必填 | 描述 |
|-------|------|------|------|
| code | string | 是 | 文件分享码 |

---

## 分片上传接口

分片上传适用于大文件，支持断点续传（通过文件哈希自动检测未完成会话）。

### 初始化上传

**POST** `/chunk/upload/init/`

**请求体：**

```json
{
  "file_name": "big.zip",
  "chunk_size": 5242880,
  "file_size": 104857600,
  "file_hash": "sha256 完整文件哈希"
}
```

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "existed": false,
    "upload_id": "xxx",
    "chunk_size": 5242880,
    "total_chunks": 20,
    "uploaded_chunks": []
  }
}
```

> `existed=true` 表示存在断点续传会话，`uploaded_chunks` 列出已上传的分片索引。

### 上传分片

**POST** `/chunk/upload/chunk/{upload_id}/{chunk_index}`

multipart 表单上传单个分片（`chunk` 字段）。重复上传已存在的分片会返回 `skipped: true`。

### 查询进度

**GET** `/chunk/upload/status/{upload_id}`

返回已上传分片索引与进度百分比。

### 合并完成

**POST** `/chunk/upload/complete/{upload_id}`

**请求体：**

```json
{
  "expire_value": 1,
  "expire_style": "day",
  "remark": ""
}
```

合并所有分片并生成分享码。**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "code": "ABC123",
    "name": "big.zip"
  }
}
```

### 取消上传

**DELETE** `/chunk/upload/{upload_id}`

取消上传并清理已保存的临时分片。

---

## 预签名上传接口

S3 存储下生成预签名直传 URL（浏览器直接上传到对象存储）；其他存储自动降级为服务器代理上传。会话有效期 15 分钟。

### 初始化上传

**POST** `/presign/upload/init`

**请求体：**

```json
{
  "file_name": "photo.jpg",
  "file_size": 10485760,
  "expire_value": 1,
  "expire_style": "day",
  "remark": ""
}
```

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "upload_id": "xxx",
    "upload_url": "https://bucket.s3...（直传）或 /presign/upload/proxy/xxx（代理）",
    "mode": "direct",
    "expires_in": 900
  }
}
```

### 代理模式上传

**PUT** `/presign/upload/proxy/{upload_id}`

multipart 表单上传文件（`file` 字段）。文件大小须与初始化声明一致（误差 ≤ 1KB）。

### 直传模式确认

**POST** `/presign/upload/confirm/{upload_id}`

客户端完成 S3 直传后调用，校验文件存在与大小后生成分享码。

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "code": "ABC123",
    "name": "photo.jpg"
  }
}
```

### 查询 / 取消会话

- `GET /presign/upload/status/{upload_id}`：查询会话状态
- `DELETE /presign/upload/{upload_id}`：取消会话（直传模式会删除已上传对象）

---

## 收件箱与投递接口

收件箱（文件收集）包含三个独立口令：**管理码**（collection_code）、**投件码**（delivery_code）、**取件码**（retrieve_code）。所有码均为 6 位大写字母+数字。

### 创建收件箱

**POST** `/collection/create/`

**请求体：**

```json
{
  "title": "项目文件收集",
  "description": "请上传项目相关文件",
  "password": "",
  "max_files": 20,
  "expire_style": "day",
  "expire_value": 7,
  "delivery_expire_style": "day",
  "delivery_expire_value": 7,
  "retrieve_expire_style": "day",
  "retrieve_expire_value": 7,
  "delivery_count": -1
}
```

- `delivery_count`：最大投递次数，`-1` 表示不限
- 投件码/取件码过期时间不能超过收件箱过期时间

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "collection_code": "ABC123",
    "delivery_code": "DEF456",
    "retrieve_code": "GHI789",
    "title": "项目文件收集",
    "max_files": 20,
    "expired_at": "...",
    "delivery_expired_at": "...",
    "retrieve_expired_at": "..."
  }
}
```

### 管理收件箱

**GET** `/collection/manage/{collection_code}`

管理码视角，返回收件箱信息、三码及全部文件列表。

### 取件

**GET** `/collection/retrieve/{retrieve_code}`

取件码视角，只读返回文件列表（不暴露管理码与投件码）。

### 投件码查看

**GET** `/collection/manage-by-delivery/{delivery_code}`

投件码视角，查看已投递的文件列表。

### 更新收件箱配置

**PATCH** `/collection/update/{collection_code}`

仅可更新投件码/取件码有效期与最大文件数（收件箱过期时间创建后不可更改）。

**请求体（可部分提交）：**

```json
{
  "delivery_expire_style": "day",
  "delivery_expire_value": 3,
  "retrieve_expire_style": "day",
  "retrieve_expire_value": 3,
  "max_files": 50
}
```

### 下载 / 打包 / 删除文件

- **GET** `/collection/download/{file_id}?code=管理码或取件码`：下载单个文件
- **GET** `/collection/zip/{code}`：打包下载收件箱全部文件（管理码或取件码）
- **DELETE** `/collection/delete/{file_id}?code=管理码`：删除文件（仅管理码可删除）

### 投递

**GET** `/delivery/{delivery_code}`：获取投递页信息（标题、容量、已投递数等，不暴露文件列表）。

**POST** `/delivery/upload/`

投递文件到收件箱。

**请求参数（multipart 表单）：**

| 参数名 | 类型 | 必填 | 描述 |
|-------|------|------|------|
| delivery_code | string | 是 | 投件码 |
| file | file | 是 | 要投递的文件 |
| uploader_name | string | 否 | 投递者署名 |

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "id": 1,
    "filename": "report.pdf",
    "file_size": 2048,
    "status": "completed"
  }
}
```

### 收件箱实时推送（WebSocket）

**WS** `/ws/collection/{code}`

使用管理码或投件码连接，第一条消息发送 `{"type":"join","nickname":"昵称"}` 注册身份。服务端推送的事件类型：`user_joined` / `user_left` / `file_uploading` / `file_progress` / `file_completed` / `file_delivery_failed` / `file_deleted`。

---

## 跨设备聊天接口

### 创建房间

**POST** `/chat/create/`

**请求体：**

```json
{
  "title": "闲聊",
  "expire_style": "day",
  "expire_value": 1
}
```

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "room_code": "ABC123",
    "title": "闲聊",
    "expired_at": "..."
  }
}
```

### 房间信息 / 历史消息

- **GET** `/chat/info/{room_code}`：房间信息（加入页展示）
- **GET** `/chat/history/{room_code}?before_id=0`：最近消息（新加入/断线重连同步）

### 实时聊天（WebSocket）

**WS** `/ws/chat/{room_code}`

第一条消息发送 `{"type":"join","nickname":"昵称"}`。文本消息帧：`{"type":"chat_message","content":"..."}`（单条 ≤ 64KB）；打字状态帧：`{"type":"typing","is_typing":true}`。服务端广播：`chat_message` / `typing` / `user_joined` / `user_left`。

---

## 直连传输与直连快传接口

两个模块均基于 WebSocket + WebRTC，服务器中转兜底且**不落盘**（单帧二进制 ≤ 4MB）。

### 直连传输（纯文件）

- **POST** `/transfer/create/`：创建房间，请求体 `{"title":"","expire_style":"day","expire_value":1}`
- **GET** `/transfer/info/{room_code}`：房间信息
- **WS** `/ws/transfer/{room_code}`：首个连接者为主机（`role: host`），其余为访客（`role: guest`）。信令帧：`rtc_offer` / `rtc_answer` / `rtc_ice`；控制帧：`file_start` / `file_cancel`；二进制帧直接中继给房间内其他连接。

### 直连快传（消息 + 文件一体）

- **POST** `/direct/create/`：创建房间
- **GET** `/direct/info/{room_code}`：房间信息
- **GET** `/direct/history/{room_code}?before_id=0`：最近聊天消息
- **WS** `/ws/direct/{room_code}`：多人房间。消息帧：`chat_message` / `typing`；文件流程：`file_offer`（广播）→ `file_response`（接受/拒绝）→ `file_start`（定向中继）→ 二进制数据定向转发给目标接收者；另有 `rtc_offer` / `rtc_answer` / `rtc_ice` 信令。服务器中转受后台 `directRelayEnabled` 与 `directRelaySpeedLimit` 控制。

---

## 管理接口

以下接口除 `/admin/login` 外均需携带管理员 Token（`Authorization: Bearer <token>`）。

### 管理员登录

**POST** `/admin/login`

**请求参数：**

| 参数名 | 类型 | 必填 | 描述 |
|-------|------|------|------|
| password | string | 是 | 管理员口令 |

### 仪表盘数据

**GET** `/admin/dashboard`

**响应示例：**

```json
{
  "code": 200,
  "message": "ok",
  "detail": {
    "totalFiles": 100,
    "storageUsed": "1572864000",
    "sysUptime": "1723353600000",
    "yesterdayCount": 50,
    "yesterdaySize": "524288000",
    "todayCount": 30,
    "todaySize": "314572800",
    "totalCollections": 5,
    "activeCollections": 3,
    "totalDeliveries": 12,
    "todayDeliveries": 4,
    "todayDeliveriesSize": "1048576"
  }
}
```

### 文件管理

- **GET** `/admin/file/list?page=1&size=10&keyword=`：分页列表（含多文件子项明细，不含收件箱文件）
- **DELETE** `/admin/file/delete`：删除文件，请求体 `{"id": 1}`

### 配置管理

- **GET** `/admin/config/get`：获取系统配置（敏感项脱敏为 `******`）
- **PATCH** `/admin/config/update`：更新配置，请求体为配置项键值对（只更新提交的字段；敏感项留空/`******` 表示不修改）

### 收件箱管理

- **GET** `/admin/collection/list?page=1&size=10&keyword=`：收件箱列表（含三码、容量、各码过期状态）
- **GET** `/admin/collection/{collection_id}/files`：收件箱文件列表
- **DELETE** `/admin/collection/{collection_id}`：删除收件箱及其全部文件

### 直连快传房间管理

- **GET** `/admin/direct/room/list?page=1&size=10&keyword=`：房间列表（含在线人数、消息数）
- **DELETE** `/admin/direct/room/{room_id}`：删除房间并断开在线连接

---

## 错误响应

当发生错误时，API 返回对应的错误信息（HTTPException）：

```json
{
  "detail": "文件不存在"
}
```

或（未授权）：

```json
{
  "detail": "密码错误"
}
```

## 状态码说明

- 200: 请求成功
- 400: 请求参数错误
- 401: 未授权
- 403: 禁止访问 / 权限不足
- 404: 资源不存在
- 410: 资源已过期
- 422: 请求参数验证错误（Pydantic）
- 429: 请求过于频繁 / 上传会话过多
- 500: 服务器内部错误
