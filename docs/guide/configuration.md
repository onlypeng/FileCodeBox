# 配置说明

配置有两处来源：`core/settings.py` 的 `DEFAULT_CONFIG`（出厂默认值）与数据库中的覆盖值。
**推荐在管理后台「系统设置」修改**——保存即写库并热更新到运行中的进程，不用重启。
可直接改库的场景只有部署初始化。

::: warning 需重启才生效的配置
`serverHost` / `serverPort` / `serverWorkers` 是启动参数，改完必须重启；
`redisUrl` 与 `db*` 系列改动会在保存时被明确拒绝（值为真的改变时返回 400）——
订阅连接与连接池无法热切换，请改 `.env` 或环境变量后重启。
:::

## 站点与界面

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | string | `驿码 - FileCodeBox` | 站点名称，页面标题与导航栏 |
| `description` | string | `开箱即用的文件快传系统` | 站点描述（SEO） |
| `keywords` | string | `FileCodeBox, 驿码, ...` | 站点关键词（SEO） |
| `notify_title` / `notify_content` | string | `系统通知` / 欢迎信息 | 顶部公告标题与内容，支持 HTML |
| `page_explain` | string | 法律声明 | 页面底部说明文字 |
| `robotsText` | string | `User-agent: *\nDisallow: /` | robots.txt 内容 |
| `showAdminAddr` | int | `0` | 首页是否显示管理入口（1=显示） |
| `opacity` | float | `0.9` | 界面透明度（0~1） |
| `background` | string | `""` | 自定义背景图 URL，空 = 默认背景 |
| `themesSelect` / `themesChoices` | string / list | `themes/2024` / 两个内置主题 | 旧版主题选择；**dist 部署不参与**，仅作兼容保留 |
| `spaTheme` | string | `light` | 前端默认主题：`light` / `dark` / `enterprise`，由主题管理模块写入 |

## 上传

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `openUpload` | int | `1` | 是否开放上传（0=关闭，仅管理员可传） |
| `uploadSize` | int | `104857600` | 单文件上限（**字节**），默认 100MB |
| `fileTypeWhitelist` | string | `""` | 扩展名白名单，逗号分隔（`jpg,png,pdf`）；空 = 不限制 |
| `expireStyle` | list | `day,hour,minute,forever,count` | 允许选择的过期方式 |
| `maxTextLength` | int | `200000` | 纯文本分享/备注的最大**字符**数 |
| `uploadMinute` | int | `1` | 上传限流时间窗口（分钟） |
| `uploadRateLimitCount` | int | `10` | 窗口内单 IP 允许的上传次数 |
| `maxSendFiles` | int | `20` | 单次发件的文件数量上限 |
| `maxMultiFileCount` | int | `20` | 多文件分享记录的数量上限 |
| `codeLength` | int | `6` | 提取码位数（4~12），文件码/收件箱三码/房间码统一长度 |

### 分片上传

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enableChunk` | int | `0` | 是否启用分片上传（1=启用） |
| `uploadChunkSize` | int | `5` | 分片大小（**MB**），仅分片开启时生效 |
| `chunkExpireHours` | int | `24` | 未完成分片会话的回收时长（小时） |

分片上传支持断点续传：进度保存在当前页面内存里（`uploadId` + 已传片号 + `File` 对象），
刷新后 `File` 无法恢复，只能重传；主动「放弃」或离开页面才回滚已扣的投递次数。

## 过期与清理

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `expiredRetentionStyle` | string | `day` | 记录过期后的保留单位：`day` / `hour` / `minute` / `forever` |
| `expiredRetentionValue` | int | `1` | 保留时长数值；`forever` = 不自动清理，文件长期占盘 |

保留期覆盖文件分享、收件箱、直连房间三类记录，到期由后台任务连同物理文件一起清理。
`forever` 是逃生阀：开启后清理循环直接退出，磁盘只增不减，需自行巡检。

## 安全管理

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `admin_token` | string | `FileCodeBox2023` | 管理员口令，**首次登录强制修改** |
| `errorMinute` | int | `1` | 取件码猜错限流窗口（分钟） |
| `errorCount` | int | `10` | 窗口内允许的猜错次数 |
| `auditLogRetentionDays` | int | `90` | 审计日志保留天数（0=不清理） |
| `trustedProxies` | string | `""` | 可信代理 IP，逗号分隔；部署在 Nginx 之后时填代理地址，否则取到的是代理 IP |
| `cors_origins` | string | `http://localhost:5173,...` | CORS 白名单；生产同源部署可留空 |

口令与密钥的存储细节（PBKDF2、敏感项加密、占位符回写）见[安全](/guide/security)。

### 病毒扫描（可选）

默认关闭，未配置钩子时零开销；启用后每个上传在落盘后回调外部扫描服务。

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `virusScanEnabled` | bool | `false` | 总开关 |
| `virusScanHook` | string | `""` | 扫描钩子，两种形态见下 |
| `virusScanTimeoutSec` | int | `30` | 单次扫描超时（1~300 秒） |
| `virusScanFailOpen` | bool | `true` | 扫描服务不可用时的策略：`true`=放行并告警，`false`=拒绝上传 |

```bash
# 命令形态：{file} 会被替换为落盘后的临时路径，返回码 0=干净 / 1=感染
clamscan --no-summary {file}

# HTTP 形态：POST 文件二进制，响应 {"infected": true, "reason": "..."}
https://scan.example.com/check
```

::: tip 为什么默认 fail-open
扫描是辅助能力，不该因为扫描服务挂了就让整个上传不可用。
对安全要求更高的场景改成 `false`，代价是扫描服务抖动期间上传全部失败。
:::

## 存储

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `file_storage` | string | `local` | 存储后端：`local` / `s3` / `onedrive` / `webdav` / `opendal` |
| `storage_path` | string | `""` | 本地存储目录，空 = `data/storage` |

各后端的连接参数（`s3_*`、`onedrive_*`、`webdav_*`、`opendal_*`）见[存储配置](/guide/storage)。

## 收件箱

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `maxCollectionFiles` | int | `100` | 单个收件箱的文件数硬上限 |
| `collectionDefaultMaxFiles` | int | `20` | 创建时的默认文件数 |
| `collectionDefaultExpireDays` | int | `7` | 创建时的默认有效期（天） |

## 直连快传与房间

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `directRelayEnabled` | int | `1` | 服务器中转开关（1=开启）；关闭后只能 P2P |
| `directRelaySpeedLimit` | int | `0` | 中转限速（KB/s，0=不限） |
| `directRelayChunkSize` | int | `64` | 中转分片大小（KB，16~256）。分片加密后单帧 ≈ 分片+40B，网关常见上限 256KB，取值别顶满 |
| `directMaxRelaySize` | int | `0` | 单个中转文件的体积上限（MB，0=不限）；P2P 直连不受此限 |
| `directTurnServers` | list | `[]` | WebRTC TURN 列表，对称 NAT 下兜底直连 |
| `defaultMaxMembers` | int | `10` | 房间默认人数上限（0=不限），创建时可选 1~该值 |
| `mediaDefaultQuality` | string | `auto` | 共享视频流默认画质：`low` / `sd` / `hd` / `auto` |

```json
// directTurnServers 单项格式
[{ "urls": "turn:host:3478?transport=udp", "username": "xxx", "credential": "yyy" }]
```

### 房间文件缓存

房间的「服务端缓存」把文件落到本机磁盘，后进房间的人可直接从服务器取，不必等发送方在线。

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `roomCacheEnabled` | int | `1` | 是否允许房间文件缓存到服务器；关闭后只能 P2P 多源转发 |
| `roomCacheDefaultHours` | int | `72` | 默认缓存时长（小时，0=跟随房间生命周期） |
| `roomCacheMaxSize` | int | `100` | 单个缓存文件的体积上限（MB，0=不限） |
| `roomCacheRoomQuota` | int | `500` | 单个房间的缓存总配额（MB，0=不限）；超配额拒绝新上传，文件过期后额度自动释放 |

取源优先级固定为：**在线且已下载的持有者（P2P）> 服务端缓存 > 无源提示**。
已有缓存的用户不会被再问一次「是否提供下载」，避免重复弹窗。

## 全局配额

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `globalStorageQuotaMB` | int | `0` | 全局存储配额（MB，0=不限）。口径 = 文件柜累计大小 + 房间缓存目录占用 |

超配额**不阻止上传**，只在后台持续告警并显示占比。开放匿名上传时务必设一个值，
否则磁盘会被慢慢写满而没有任何提示。

## 对外链接

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `shareDomain` | string | `""` | 生成分享/取件链接（含下载直链、二维码、wget 命令）时使用的站点地址，如 `https://share.example.com`；空 = 用当前访问地址 |
| `wsBaseUrl` | string | `""` | WebSocket 独立接入地址。CDN/边缘加速不转发 `Upgrade` 时填直连源站域名；空 = 与页面同源 |

`shareDomain` 只影响对外分享链接，API 请求仍走实际访问地址；
`wsBaseUrl` 只影响 WebSocket，HTTP 仍走原站点。两者都随公共配置运行时下发，
改完刷新页面即生效。填法与排障见[快速开始](/guide/getting-started)的反向代理一节。

## 服务端与数据库

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `serverHost` | string | `0.0.0.0` | 监听地址（需重启） |
| `serverPort` | int | `12345` | 监听端口（需重启） |
| `serverWorkers` | int | `1` | 工作进程数（需重启）。**默认保持 1**，多进程条件见[快速开始](/guide/getting-started) |
| `redisUrl` | string | `""` | 多实例协调的 Redis 连接串，空 = 单实例；改动需重启 |
| `dbType` | string | `sqlite` | `sqlite` / `postgres` / `mysql`；SQLite 单写者，只支持单进程 |
| `dbHost` / `dbPort` | string / int | `127.0.0.1` / `0` | 端口 0 = 用类型默认端口（postgres 5432 / mysql 3306） |
| `dbUser` / `dbPassword` | string | `""` | 数据库账号口令 |
| `dbName` | string | `filecodebox` | 库名 |
| `dbSchema` | string | `public` | PostgreSQL schema，其它类型忽略 |

`redisUrl` / `db*` **故意不开放后台热改**：前端「保存设置」是整份配置回传，
后端只在值真的改变时返回 400，避免动了一个无关开关就阻断整个表单。
这些参数请通过环境变量 `REDIS_URL`、`DB_TYPE` 等注入后重启。

## 下一步

- [快速开始](/guide/getting-started) - 部署、环境变量、反向代理、多实例
- [存储配置](/guide/storage) - 各存储后端参数
- [安全设置](/guide/security) - 口令、限流、白名单、病毒扫描
- [管理面板](/guide/management) - 后台各模块操作
