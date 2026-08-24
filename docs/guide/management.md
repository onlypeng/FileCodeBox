# 管理面板

FileCodeBox 提供了功能完善的管理面板，让管理员可以方便地管理文件、收件箱、直连房间和修改系统配置。本文档介绍管理面板的各项功能和使用方法。

## 访问管理面板

### 登录方式

1. 在浏览器中访问 `http://your-domain.com/#/admin`（或 `/#/login`）
2. 输入管理员口令（`admin_token` 配置项的值）
3. 点击登录按钮

::: tip 提示
默认管理员口令是 `FileCodeBox2023`。请务必在生产环境中修改此口令，详见 [安全设置](/guide/security)。
:::

### 显示管理入口

默认情况下，首页不显示管理面板入口。您可以通过配置控制是否显示：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `showAdminAddr` | int | `0` | 是否在首页显示管理入口（1=显示，0=隐藏） |

::: warning 安全建议
在公开服务中，建议保持 `showAdminAddr` 为 `0`，通过直接访问 `/#/admin` 路径进入管理面板，减少被恶意扫描的风险。
:::

### 认证机制

管理面板使用 JWT（JSON Web Token）进行身份认证：

1. 登录成功后，服务器返回一个包含管理员身份的 Token
2. 后续请求通过 `Authorization: Bearer <token>` 头部携带 Token
3. Token 默认有效期为 30 天

管理员口令以 PBKDF2 哈希存储，非明文。可通过设置 `JWT_SECRET` 环境变量固定 Token 签名密钥（留空则从管理员口令派生）。

## 仪表盘

登录后首先看到的是仪表盘页面，展示系统的整体运行状态。

### 统计指标

仪表盘显示以下关键指标：

| 指标 | 说明 |
|------|------|
| **总文件数** (`totalFiles`) | 系统中已分享的文件总数量 |
| **存储空间** (`storageUsed`) | 所有文件占用的总存储空间（字节） |
| **运行时间** (`sysUptime`) | 系统启动时间戳 |
| **昨日/今日分享** | 前一天/当天分享的数量与容量 |
| **收件箱统计** | 收件箱总数与有效数 |
| **投递统计** | 累计投递、今日投递及容量 |

### 文件健康与存储策略

- **文件健康度**：基于真实文件记录统计有效、过期和类型分布，可在文件管理中清理过期文件。
- **存储与上传策略**：展示当前存储后端、单文件上限、游客上传开关、最长保存时间等配置对上传链路的影响。

## 文件管理

### 文件列表

文件管理页面展示系统中所有已分享的文件（不包含收件箱中的投递文件），支持分页浏览和搜索。

**列表信息包括：**
- 提取码（code）、文件名、文件大小
- 创建时间、过期时间、剩余下载次数
- 多文件分享的子文件明细（`file_items`）

### 搜索文件

1. 在搜索框中输入关键词
2. 系统根据文件名进行模糊匹配（匹配 `prefix` 字段）
3. 搜索结果实时更新

### 删除文件

管理员可以删除任意文件：

1. 在文件列表中找到要删除的文件
2. 点击删除按钮
3. 确认删除操作

::: danger 警告
删除操作不可恢复！文件将从存储后端永久删除，同时删除数据库中的记录。
:::

**删除流程：**
1. 系统首先从存储后端（本地/S3/OneDrive 等）删除实际文件
2. 然后从数据库中删除文件记录
3. 删除后，对应的提取码将失效

## 收件箱管理

收件箱（文件收集）管理页面展示所有收件箱：

- 标题、管理码/投件码/取件码
- 最大文件数与已投递文件数
- 收件箱、投件码、取件码各自的过期时间与过期状态
- 创建时间

支持的操作：

| 操作 | 说明 |
|------|------|
| **查看文件列表** | 查看收件箱中已投递的文件（文件名、大小、投递者、状态、时间） |
| **删除收件箱** | 删除收件箱及其关联的所有文件（不可恢复） |

::: danger 警告
删除收件箱会同时删除其中所有已投递的文件，操作不可恢复！
:::

## 直连快传房间管理

直连快传房间管理页面展示所有房间：

- 房间码、标题、最大成员数
- 当前在线人数、消息数
- 过期时间与过期状态

支持的操作：

| 操作 | 说明 |
|------|------|
| **删除房间** | 断开该房间所有在线 WebSocket 连接并删除房间及消息记录 |

## 系统设置

### 查看配置

在系统设置页面可以查看当前所有配置项的值。配置项按类别分组显示：

- 基础设置（站点名称、描述、通知等）
- 上传设置（文件大小限制、频率限制、分片开关等）
- 存储设置（存储类型、路径及各存储后端凭据）
- 主题设置（主题选择、透明度、背景图）
- 安全设置（管理员口令、错误限制、可信代理、CORS）
- 实时通信设置（直连中转开关、限速、TURN 服务器）

### 修改配置

管理员可以通过管理面板修改大部分配置：

1. 进入系统设置页面
2. 找到要修改的配置项
3. 输入新的值
4. 点击保存按钮

**可修改的配置项：**

| 类别 | 配置项示例 |
|------|------------|
| 基础设置 | `name`, `description`, `keywords`, `notify_title`, `notify_content` |
| 上传设置 | `uploadSize`, `uploadMinute`, `uploadCount`, `openUpload`, `enableChunk` |
| 过期设置 | `expireStyle`, `max_save_seconds` |
| 多文件/收件箱 | `maxSendFiles`, `maxMultiFileCount`, `maxCollectionFiles` |
| 主题设置 | `themesSelect`, `opacity`, `background` |
| 安全设置 | `admin_token`, `showAdminAddr`, `errorMinute`, `errorCount` |
| 存储设置 | `file_storage`, `storage_path` 及各存储后端的配置 |
| 网络设置 | `trustedProxies`, `cors_origins` |
| 实时通信 | `directRelayEnabled`, `directRelaySpeedLimit`, `directTurnServers` |

::: warning 注意
- `admin_token`（管理员口令）留空或保持脱敏占位符 `******` 表示不修改
- 敏感凭据（S3/OneDrive/WebDAV 等）在界面中脱敏展示，留空表示不修改，保存时会加密存储
- `themesChoices`（主题列表）不可通过管理面板修改
- 修改存储设置后，已有文件不会自动迁移
:::

### 配置生效

配置修改后立即生效（最多 5 秒缓存延迟），无需重启服务。配置保存在数据库中，重启后仍然有效。

**配置存储位置：**
- 数据库：`data/filecodebox.db`
- 表名：`keyvalue`
- 键名：`settings`

## API 接口

管理面板的所有功能都通过 REST API 实现，以下是主要接口：

### 认证接口

**登录**
```
POST /admin/login
Content-Type: application/json

{
    "password": "your-admin-password"
}
```

响应：
```json
{
    "code": 200,
    "message": "ok",
    "detail": {
        "token": "eyJhbGciOiJIUzI1NiIs...",
        "token_type": "Bearer"
    }
}
```

### 仪表盘接口

**获取统计数据**
```
GET /admin/dashboard
Authorization: Bearer <token>
```

### 文件管理接口

**获取文件列表**
```
GET /admin/file/list?page=1&size=10&keyword=
Authorization: Bearer <token>
```

**删除文件**
```
DELETE /admin/file/delete
Authorization: Bearer <token>
Content-Type: application/json

{
    "id": 123
}
```

### 收件箱管理接口

**获取收件箱列表**
```
GET /admin/collection/list?page=1&size=10&keyword=
Authorization: Bearer <token>
```

**获取收件箱文件列表**
```
GET /admin/collection/{collection_id}/files
Authorization: Bearer <token>
```

**删除收件箱**
```
DELETE /admin/collection/{collection_id}
Authorization: Bearer <token>
```

### 直连快传房间接口

**获取房间列表**
```
GET /admin/direct/room/list?page=1&size=10&keyword=
Authorization: Bearer <token>
```

**删除房间**
```
DELETE /admin/direct/room/{room_id}
Authorization: Bearer <token>
```

### 配置接口

**获取配置**
```
GET /admin/config/get
Authorization: Bearer <token>
```

**更新配置**
```
PATCH /admin/config/update
Authorization: Bearer <token>
Content-Type: application/json

{
    "uploadSize": 52428800
}
```

## 常见问题

### 忘记管理员口令

如果忘记了管理员口令，可以通过以下方式重置：

1. 停止 FileCodeBox 服务
2. 使用 SQLite 工具打开 `data/filecodebox.db`
3. 查询 `keyvalue` 表中 `key='settings'` 的记录
4. 修改 JSON 中的 `admin_token` 值（需为 PBKDF2 哈希格式，或直接修改为明文后由系统在下次启动时自动迁移）
5. 重启服务

```sql
-- 查看当前配置
SELECT * FROM keyvalue WHERE key = 'settings';

-- 或者删除配置，恢复默认口令
DELETE FROM keyvalue WHERE key = 'settings';
```

### 文件删除失败

如果删除文件时出现错误，可能的原因：

1. **存储后端连接失败**：检查存储配置是否正确
2. **文件已不存在**：文件可能已被手动删除
3. **权限不足**：检查存储目录的写入权限

### 配置修改不生效

如果修改配置后没有生效：

1. 检查是否点击了保存按钮
2. 稍等几秒（配置有 5 秒缓存）后刷新页面
3. 检查浏览器控制台是否有错误信息
4. 确认配置值的格式是否正确（如数字类型不要输入字符串）

## 下一步

- [配置说明](/guide/configuration) - 了解所有配置选项的详细说明
- [安全设置](/guide/security) - 了解如何增强系统安全性
- [存储配置](/guide/storage) - 配置不同的存储后端
