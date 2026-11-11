# 安全设置

FileCodeBox 提供了多层安全机制来保护您的文件分享服务。本文档介绍如何正确配置安全选项，确保系统安全运行。

## 管理员密码

### 修改默认密码

::: danger 重要安全警告
FileCodeBox 的默认管理员密码是 `FileCodeBox2023`。**在生产环境中必须立即修改此密码！**使用默认密码会导致任何人都可以访问您的管理面板。
:::

修改管理员密码有两种方式：

**方式一：通过管理面板修改（推荐）**

1. 访问 `/admin` 进入管理面板
2. 使用当前密码登录
3. 进入「系统设置」页面
4. 找到 `admin_token` 配置项
5. 输入新的安全密码并保存

**方式二：通过数据库修改**

配置存储在 `data/filecodebox.db` 数据库的 `keyvalue` 表中，可以直接修改 `admin_token` 的值。

### 密码安全建议

- 使用至少 16 个字符的强密码
- 包含大小写字母、数字和特殊字符
- 避免使用常见词汇或个人信息
- 定期更换密码

```python
# 推荐的密码格式示例
"admin_token": "Xk9#mP2$vL5@nQ8&wR3"
```

### 隐藏管理入口

默认情况下，管理面板入口是隐藏的。您可以通过 `showAdminAddr` 配置控制是否在首页显示管理入口：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `showAdminAddr` | int | `0` | 是否显示管理入口（1=显示，0=隐藏） |

::: tip 建议
在公开服务中，建议保持 `showAdminAddr` 为 `0`，通过直接访问 `/admin` 路径进入管理面板。
:::

### 管理 Token 存储（localStorage）

管理后台登录 Token 存储于浏览器 `localStorage`（`src/utils/auth-storage.ts`），
**任何 XSS 都可读取该 Token**。这是自托管/局域网工具的现状权衡，不迁移到 httpOnly Cookie 的原因：

- 本项目前后端分离，API 通过 `Authorization: Bearer` 头认证，Cookie 方案需改动认证协议；
- 现有 XSS 纵深：聊天/备注/Markdown 渲染全部经 DOMPurify 白名单消毒；后台模板注入已转义；
  管理员密码不做本地持久化；Token 30 天过期且路由守卫经 `/admin/verify` 校验有效性。

若部署于不可信网络或多人共享浏览器，建议改造为 httpOnly Cookie。

### 口令存储与敏感配置加密

- 管理员口令以 **PBKDF2-SHA256 加盐哈希** 存储（非明文）。旧版明文口令会在启动时自动迁移为哈希。
- S3 / OneDrive / WebDAV 等敏感凭据保存时使用 `ENCRYPTION_KEY`（未设置则从管理员口令派生）加密，读取时自动解密，管理界面展示时脱敏为 `******`。
- 生产环境建议显式设置 `ENCRYPTION_KEY` 与 `JWT_SECRET` 环境变量。

## IP 速率限制

FileCodeBox 内置了基于 IP 的速率限制机制，可以有效防止滥用和攻击。

### 上传频率限制

限制单个 IP 在指定时间内的上传次数：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `uploadMinute` | int | `1` | 上传限制的时间窗口（分钟） |
| `uploadRateLimitCount` | int | `10` | 在时间窗口内允许的最大上传次数 |

**工作原理：**
- 系统按 IP 在上传接口上计数（计数落库，实例本身无状态）
- 当某 IP 在 `uploadMinute` 分钟内的上传次数达到 `uploadRateLimitCount` 时
- 该 IP 的后续上传请求将被拒绝（HTTP 423），等待窗口过期后计数重置
- 后台改这两项会就地生效，不需要重启

**配置示例：**

```python
# 宽松配置：5分钟内最多上传20次
{
    "uploadMinute": 5,
    "uploadRateLimitCount": 20
}

# 严格配置：1分钟内最多上传3次
{
    "uploadMinute": 1,
    "uploadRateLimitCount": 3
}
```

::: warning 多实例下阈值会放大
计数在进程内存与各自的表里独立维护，N 个实例的实际阈值约为「配置值 × N」。
需要严格限流请在前置 Nginx 或 WAF 上按 IP 计数。
:::

### 错误次数限制

限制单个 IP 的错误尝试次数，防止暴力破解提取码：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `errorMinute` | int | `1` | 错误限制的时间窗口（分钟） |
| `errorCount` | int | `10` | 在时间窗口内允许的最大错误次数 |

**工作原理：**
- 当用户输入错误的提取码时，系统记录该 IP 的错误次数
- 当错误次数达到 `errorCount` 时，该 IP 将被暂时锁定
- 锁定时间为 `errorMinute` 分钟
- 锁定期间，该 IP 的所有提取请求都将被拒绝

**配置示例：**

```python
# 防暴力破解配置：5分钟内最多允许3次错误
{
    "errorMinute": 5,
    "errorCount": 3
}
```

::: warning 注意
默认配置 `errorMinute=1, errorCount=10`，即每 1 分钟最多允许 10 次错误尝试。可根据实际需求调整此配置。
:::

## 上传限制

### 文件大小限制

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `uploadSize` | int | `104857600` | 单文件最大上传大小（字节），默认 100MB |
| `openUpload` | int | `1` | 是否开启上传功能（1=开启，0=关闭） |

**常用大小换算：**
- 10MB = 10 * 1024 * 1024 = `10485760`
- 50MB = 50 * 1024 * 1024 = `52428800`
- 100MB = 100 * 1024 * 1024 = `104857600`
- 1GB = 1024 * 1024 * 1024 = `1073741824`

### 文件类型白名单

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `fileTypeWhitelist` | str | `""` | 允许上传的扩展名白名单（逗号分隔，如 `jpg,png,pdf`）；留空不限制 |

- 校验基于文件扩展名（大小写不敏感），适用于普通分享、投递与分片上传全部入口；
- 上传文件以 `attachment` 响应头强制下载、不落 Web 静态目录，即使白名单放开也不提供直接执行路径；
- 如需屏蔽可执行/可内联文件（`.html`、`.svg`、`.js` 等），在后台「系统设置 → 上传设置」配置白名单。

### 文件过期设置

通过文件过期机制，可以自动清理过期文件，减少存储占用和安全风险：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `expireStyle` | list | `["day","hour","minute","forever","count"]` | 允许上传者选择的过期方式 |
| `expiredRetentionStyle` | str | `day` | 记录过期后的保留单位：`day`/`hour`/`minute`/`forever` |
| `expiredRetentionValue` | int | `1` | 保留时长；`forever` = 不自动清理 |

**过期方式说明：**
- `day` - 按天数过期
- `hour` - 按小时过期
- `minute` - 按分钟过期
- `forever` - 永不过期（需要字符串提取码）
- `count` - 按下载次数过期

::: tip count（按次）模式的语义
`count` 模式为**次数用尽前永久有效**（`expired_at` 为空，无时间上限），
仅按剩余取件次数判断过期；次数用尽（`expired_count = 0`）即过期并被后台任务清理。
最后一次取件的下载链接仍有 1 小时放行窗口，避免下载到一半被掐断。
::: 

**安全建议：**

系统没有「全局最长保存时间」这个开关，单个文件的期限完全由上传时选择的方式决定。
公开服务建议：

1. 从 `expireStyle` 里**去掉 `forever`**，强制每次上传都带期限
2. 把 `expiredRetentionStyle` 保持为有限值（如 `day`/`1`），过期记录连同物理文件一起清掉
3. 优先保留 `count`，取完即删

```python
{
    "expireStyle": ["hour", "minute", "count"],
    "expiredRetentionStyle": "day",
    "expiredRetentionValue": 1
}
```

### 关闭上传功能

在某些情况下，您可能需要临时关闭上传功能：

```python
{
    "openUpload": 0  # 关闭上传功能
}
```

### 病毒扫描钩子

匿名上传站点的最大风险是被人当恶意文件的中转站。系统内置一个**可选**的扫描钩子，
默认关闭（未配置钩子时零开销），启用后每个上传在落盘后回调外部扫描服务。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `virusScanEnabled` | bool | `false` | 总开关 |
| `virusScanHook` | str | `""` | 扫描钩子，命令或 HTTP 地址 |
| `virusScanTimeoutSec` | int | `30` | 单次扫描超时（1~300 秒） |
| `virusScanFailOpen` | bool | `true` | 扫描服务不可用时：`true`=放行并告警，`false`=拒绝上传 |

两种钩子形态：

```bash
# 命令形态：{file} 替换为落盘后的临时路径，返回码 0=干净，1=感染
clamscan --no-summary {file}

# HTTP 形态：POST 文件二进制，需返回 {"infected": true|false, "reason": "..."}
https://scan.example.com/check
```

::: warning fail-open 是取舍，不是疏漏
扫描是辅助能力，不该因为扫描服务抖动就让整个上传不可用，所以默认放行。
对安全要求更高的场景改成 `false`，代价是扫描服务不可用期间上传全部失败。
:::

## 反向代理安全配置

在生产环境中，通常会使用 Nginx 或其他反向代理服务器。以下是安全配置建议：

### Nginx 配置示例

```nginx
server {
    listen 80;
    server_name your-domain.com;
    
    # 强制 HTTPS 重定向
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;
    
    # SSL 证书配置
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;
    ssl_prefer_server_ciphers on;
    
    # 安全头部
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    
    # 限制请求体大小（与 uploadSize 配置一致）
    client_max_body_size 100M;
    
    # 传递真实 IP
    location / {
        proxy_pass http://127.0.0.1:12345;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket 升级转发（实时房间 /ws/room、收件箱实时推送 /ws/collection 必需，
        # 缺失时房间页显示"已断开"、实时进度不更新）
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
    
    # 静态资源缓存
    location /assets {
        proxy_pass http://127.0.0.1:12345;
        proxy_cache_valid 200 7d;
        add_header Cache-Control "public, max-age=604800";
    }
}
```

### 关键安全配置说明

**1. 传递真实 IP**

FileCodeBox 的 IP 限制功能依赖于获取客户端真实 IP。系统会按以下顺序获取 IP：
1. `X-Real-IP` 请求头
2. `X-Forwarded-For` 请求头
3. 直接连接的客户端 IP

确保反向代理正确设置这些头部：

```nginx
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

**2. 请求体大小限制**

Nginx 的 `client_max_body_size` 应该与 FileCodeBox 的 `uploadSize` 配置一致或略大：

```nginx
client_max_body_size 100M;  # 允许上传最大 100MB
```

**3. HTTPS 加密**

强烈建议在生产环境中启用 HTTPS：
- 保护用户上传的文件内容
- 保护管理员登录凭据
- 防止中间人攻击

### Caddy 配置示例

```nginx
your-domain.com {
    reverse_proxy localhost:12345
    
    header {
        X-Frame-Options "SAMEORIGIN"
        X-Content-Type-Options "nosniff"
        X-XSS-Protection "1; mode=block"
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
    }
}
```

## 安全检查清单

部署 FileCodeBox 前，请确认以下安全配置：

- [ ] 已修改默认管理员密码 `admin_token`
- [ ] 已隐藏管理入口 `showAdminAddr: 0`
- [ ] 已配置合适的上传频率限制
- [ ] 已配置错误次数限制防止暴力破解
- [ ] 已设置合理的文件大小限制
- [ ] 已配置文件过期策略
- [ ] 已启用 HTTPS 加密
- [ ] 反向代理已正确传递真实 IP
- [ ] 已设置安全响应头部

## 推荐安全配置

### 公开服务配置

```python
{
    "admin_token": "your-very-secure-password",
    "showAdminAddr": 0,
    "uploadSize": 10485760,           # 10MB
    "uploadMinute": 1,
    "uploadRateLimitCount": 5,
    "errorMinute": 5,
    "errorCount": 3,
    "expireStyle": ["hour", "minute", "count"],
    "expiredRetentionStyle": "day",   # 过期后 1 天清理
    "expiredRetentionValue": 1,
    "fileTypeWhitelist": "jpg,png,pdf,zip,docx,xlsx,pptx",
    "openUpload": 1
}
```

### 内部服务配置

```python
{
    "admin_token": "internal-secure-password",
    "showAdminAddr": 1,
    "uploadSize": 104857600,          # 100MB
    "uploadMinute": 5,
    "uploadRateLimitCount": 50,
    "errorMinute": 1,
    "errorCount": 5,
    "expireStyle": ["day", "hour", "forever"],
    "expiredRetentionStyle": "day",   # 过期后保留 7 天再清理
    "expiredRetentionValue": 7,
    "openUpload": 1
}
```

## 下一步

- [配置说明](/guide/configuration) - 了解所有配置选项
- [存储配置](/guide/storage) - 配置安全的存储后端
- [文件分享](/guide/share) - 了解文件分享功能
