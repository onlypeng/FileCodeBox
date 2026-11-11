"""配置项定义：DEFAULT_CONFIG + Settings 读写壳。

## 问题

本项目的配置项来自两个地方——本文件的 `DEFAULT_CONFIG`（代码默认值）与数据库
KeyValue 表里 key="settings" 的 JSON（管理员在后台改过的部分）。历史上各处读取
配置时自己写 `getattr(settings, "x", 某个值)`，兜底值逐渐与实际默认值漂移：
同一个键在 A 处兜底 10、B 处兜底 20，改了默认值却忘记改兜底，排查时极难发现。
另外"能不能多 worker""B 上限是否生效"这类问题都取决于某个开关的值，
哪一处读错都会得出相反结论。

## 方案

- **默认值的唯一来源**：只在本文件登记。`Settings.__getattr__` 先查 user_config、
  再回落 default_config，因此调用方可以放心写 `settings.xxx` 或
  `getattr(settings, "xxx", None)`，不必复制一遍默认值。
- **运行时覆盖不落回默认表**：`__setattr__` 除 default_config/user_config 之外
  全部写进 user_config，`DEFAULT_CONFIG` 始终保持为"出厂值"，便于 diff 出厂默认值。
- **新增配置项必须先登记到这里**：`apps/admin/services.py:update_config` 会把
  不在 `settings.default_config` 里的键静默丢弃（不是报错），没登记的配置项
  表现为"后台保存成功但完全没生效"。
- **单位写在注释里**：MB / KB / 秒 / `0 = 不限制` 这类约定在这里定死，
  调用方的 0 判断要与之对齐（例如 `roomCacheMaxSize=0` 表示不限制而非禁止上传）。

## 刻意不做

不做类型声明与校验（不用 pydantic BaseSettings）。配置项是通过后台表单写到
KeyValue 的 JSON，库中既有 1/0 也有 True/False、`admin.schemas` 全是 Optional，
若在此处加 schema 校验，历史数据会在读取期直接崩。类型容错放在读取点
（`int(getattr(...))` + try / getattr 兜底），代价是重复，收益是升级不会炸。

同理，这里也不做"配置项变更通知"：开关类配置（如 `redisUrl`）需要重启才生效，
写进 `docs/` 与后台提示，而不是在这里引入订阅机制。
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
data_root = BASE_DIR / "data"

# 导入期就建目录（本模块有副作用）：数据库文件、房间缓存根目录、签名密钥持久化文件
# 都挂在它下面，等到首次写文件再 mkdir 会让这些路径在冷启动时随机失败。
if not data_root.exists():
    data_root.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "file_storage": "local",
    "storage_path": "",
    "name": "驿码 - FileCodeBox",
    "description": "开箱即用的文件快传系统",
    "notify_title": "系统通知",
    "notify_content": '欢迎使用 FileCodeBox，本程序开源于 <a href="https://github.com/vastsa/FileCodeBox" target="_blank">Github</a> ，欢迎Star和Fork。',
    "page_explain": "请勿上传或分享违法内容。根据《中华人民共和国网络安全法》、《中华人民共和国刑法》、《中华人民共和国治安管理处罚法》等相关规定。 传播或存储违法、违规内容，会受到相关处罚，严重者将承担刑事责任。本站坚决配合相关部门，确保网络内容的安全，和谐，打造绿色网络环境。",
    "keywords": "FileCodeBox, 驿码, 口令传送箱, 匿名口令分享文本, 文件",
    "s3_access_key_id": "",
    "s3_secret_access_key": "",
    "s3_bucket_name": "",
    "s3_endpoint_url": "",
    "s3_region_name": "auto",
    "s3_signature_version": "s3v2",
    "s3_hostname": "",
    "s3_proxy": 0,
    # 过期保存时间：记录/收件箱/房间过期后再保留该时长，到期由后台任务自动清理（含文件）；
    # style ∈ day/hour/minute/forever，forever 表示不自动清理
    "expiredRetentionStyle": "day",
    "expiredRetentionValue": 1,
    "aws_session_token": "",
    "onedrive_domain": "",
    "onedrive_client_id": "",
    "onedrive_username": "",
    "onedrive_password": "",
    "onedrive_root_path": "filebox_storage",
    "onedrive_proxy": 0,
    "webdav_root_path": "filebox_storage",
    "webdav_proxy": 0,
    "admin_token": "FileCodeBox2023",
    "openUpload": 1,
    "uploadSize": 1024 * 1024 * 100,
    # 上传文件类型白名单（逗号分隔的扩展名，如 "jpg,png,pdf"；空 = 不限制）
    "fileTypeWhitelist": "",
    "expireStyle": ["day", "hour", "minute", "forever", "count"],
    "uploadMinute": 1,
    "enableChunk": 0,
    # 上传分片大小（单位 MB，默认 5）。仅当 enableChunk=1（发送页/投递页分片上传开启）时生效；
    # 前端按此大小把文件切成若干片断点续传，后端按声明值校验并（S3 后端）流式 multipart 合并。
    "uploadChunkSize": 5,
    # 未完成分片会话的回收时长（小时）：会话哨兵 created_at 超过该时长即视为放弃，
    # 由 clean_incomplete_uploads 清理。曾硬编码 24，现登记为后台可配（>=1）。
    "chunkExpireHours": 24,
    "webdav_url": "",
    "webdav_password": "",
    "webdav_username": "",
    "opacity": 0.9,
    "background": "",
    "uploadRateLimitCount": 10,
    "themesChoices": [
        {
            "name": "2023",
            "key": "themes/2023",
            "author": "Lan",
            "version": "1.0",
        },
        {
            "name": "2024",
            "key": "themes/2024",
            "author": "Lan",
            "version": "1.0",
        },
    ],
    "themesSelect": "themes/2024",
    # 前端 SPA 站点默认主题（light / dark / enterprise），由后台主题管理模块写入
    "spaTheme": "light",
    "errorMinute": 1,
    "errorCount": 10,
    "serverWorkers": 1,
    "serverHost": "0.0.0.0",
    "serverPort": 12345,
    "showAdminAddr": 0,
    "robotsText": "User-agent: *\nDisallow: /",
    "maxMultiFileCount": 20,
    "maxCollectionFiles": 100,
    "maxSendFiles": 20,
    # 文本分享/备注的最大字符数（发送页 TextInputArea 上限，与后端校验一致）
    "maxTextLength": 200000,
    "collectionDefaultMaxFiles": 20,
    "collectionDefaultExpireDays": 7,
    # 口令/码位数（文件分享码、收件箱三码、房间码统一长度，范围 4~12）
    "codeLength": 6,
    # 共享域名：生成分享/取件链接（含下载直链、二维码、wget 命令）时使用的站点地址，
    # 形如 "https://share.example.com"，留空表示使用当前访问地址。
    # 适用于反向代理或内网部署时对外固定分享域名，避免链接带上内网地址。
    # 注意：仅影响对外分享链接；API 请求仍走实际访问地址，
    # WebSocket 若需绕过不转发 Upgrade 的 CDN/ESA，用下面的 wsBaseUrl 单独指定。
    "shareDomain": "",
    # WebSocket 独立接入地址：房间/收件箱实时通道可直连源站，HTTP 仍走原站点。
    # 支持 "wss://ws.example.com"、"https://..." 或裸域名；留空 = 与页面同源。
    # 该值会随公共配置下发给所有访客，只填公开域名，不要带路径或密钥。
    "wsBaseUrl": "",
    "trustedProxies": "",
    "cors_origins": "http://localhost:5173,http://127.0.0.1:5173",
    "directRelayEnabled": 1,
    "directRelaySpeedLimit": 0,
    # 房间文件中转分片大小（单位 KB）。房间 WS 通道把文件切成该大小分片加密传输；
    # 分片加密后单帧 ≈ 分片 + 帧开销(约 40B)。不同部署环境对单条 WebSocket 帧有不同上限
    # （常见 256KB），分片设成略小可避免被网关/代理拦截。默认 64。可设范围 16~256。
    "directRelayChunkSize": 64,
    # 房间文件中转单文件大小上限（单位 MB；0=不限制，走 P2P 直连时不受此限）。
    # 独立于全局 uploadSize（文件柜普通上传），便于单独控制房间文件共享的容量占用。
    "directMaxRelaySize": 0,
    # 是否允许房间文件缓存到服务器磁盘（1=允许）。关闭后房间文件只能走 P2P 多源转发。
    "roomCacheEnabled": 1,
    # 房间文件服务器缓存默认时长（单位小时；0 = 跟随房间生命周期，随房间过期一起清理）。
    # 默认 3 天（72 小时）；需要其他时长可在房间「文件管理」中上传时手动选择。
    "roomCacheDefaultHours": 72,
    # 单个房间缓存文件大小上限（单位 MB；0 = 不限制）。用于防止磁盘被大文件占满。
    "roomCacheMaxSize": 100,
    # 单个房间的缓存总容量配额（单位 MB；0 = 不限制）。
    # 房间内所有落盘文件累计超过后拒绝新上传，防止单个房间耗尽磁盘；
    # 文件过期被清理后额度自动释放。
    "roomCacheRoomQuota": 500,
    # 全局存储配额（单位 MB；0 = 不限制）。统计口径 = 文件柜 FileCodes 累计大小
    # + 房间缓存目录占用。超过后不阻止上传，但会持续告警并在后台显示占比；
    # 匿名开放上传（openUpload）时尤其需要设一个值，否则磁盘可能被慢慢写满而无提示。
    "globalStorageQuotaMB": 0,
    # 审计日志保留天数（0 = 不清理）。下载类操作会持续写入审计，
    # 长期运行需设一个保留期，否则 audit_log 表无限增长。
    "auditLogRetentionDays": 90,
    # ===== 病毒扫描（可选） =====
    # 默认关闭：未配置钩子时零开销。启用后上传落盘会回调外部扫描服务。
    "virusScanEnabled": False,
    # 扫描钩子，两种形态：
    #   命令（本地）："clamscan --no-summary {file}" —— 返回码 0 干净 / 1 感染
    #   HTTP：        "https://scan.example.com/check" —— POST 文件，返回
    #                 {"infected": true/false, "reason": "..."}
    "virusScanHook": "",
    # 单次扫描超时（秒，1~300）
    "virusScanTimeoutSec": 30,
    # 扫描服务不可用/超时时的策略：True = 放行并告警（扫描是辅助能力，不该让
    # 上传功能整体不可用）；False = 拒绝上传（安全优先于可用性）
    "virusScanFailOpen": True,
    # ===== 数据库 =====
    # 数据库类型：sqlite（默认，单进程）/ postgres / mysql。
    # 切换类型时连接参数（dbHost/dbPort/dbUser/dbPassword/dbName）需一并配置。
    # SQLite 为单写者且房间状态在进程内，仅支持单进程部署；
    # Postgres / MySQL 才允许多 worker（见 core/capabilities）。
    "dbType": "sqlite",
    "dbHost": "127.0.0.1",
    "dbPort": 0,  # 0 = 用各类型的默认端口（postgres 5432 / mysql 3306）
    "dbUser": "",
    "dbPassword": "",
    "dbName": "filecodebox",
    "dbSchema": "public",
    # ===== 多实例协调（可选） =====
    # Redis 连接串，用于多 worker 部署时同步房间成员与广播文本信令。
    # 留空 = 单实例模式（默认，无需任何外部依赖）；形如 "redis://127.0.0.1:6379/0"。
    # 注意：改这里需要重启生效（订阅连接不能热切换）；二进制文件中继仍要求
    # 同一房间的 WebSocket 连接粘在同一个实例上（接入层会话亲和），见 core/cluster。
    "redisUrl": "",
    # 临时房间默认人员上限（创建时可在 1~该值 之间选择；0 表示不限制）
    "defaultMaxMembers": 10,
    # 共享视频流默认质量档位（low=流畅 / sd=标清 / hd=高清 / auto=按带宽自适应）
    "mediaDefaultQuality": "auto",
    # WebRTC TURN 中继服务器列表（对称 NAT 下 P2P 失败时兜底直连）
    # 格式: [{"urls": "turn:host:3478?transport=udp", "username": "xxx", "credential": "yyy"}]
    "directTurnServers": [],
}


class Settings:
    """配置读写壳：user_config（数据库）覆盖 default_config（代码默认值）。

    刻意让未知属性抛 AttributeError 而不是返回 None：配置键拼错时，读到 None 往往
    表现为"功能静默失效"（例如配额变 0、开关变关），比直接崩更难排查。
    """

    def __init__(self, defaults=None):
        self.default_config = defaults or {}
        self.user_config = {}

    def __getattr__(self, attr):
        """仅在常规属性查找失败时被调用（Python 协议），故这里查的是两张配置表。

        注意 default_config/user_config 自身已在 __setattr__ 里落到实例字典，
        不会走到这里；若未先实例化就会递归，构造顺序不能改。
        """
        if attr in self.user_config:
            return self.user_config[attr]
        if attr in self.default_config:
            return self.default_config[attr]
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{attr}'"
        )

    def __setattr__(self, key, value):
        """写配置一律进 user_config，default_config 永不被改写。

        因此直接赋值可以写入 DEFAULT_CONFIG 中不存在的键（后续也能读到）。
        后台写入侧另有白名单过滤（见 apps/admin/services.py:update_config），
        这里不做校验，保留"代码路径可直接设置运行时值"的能力。
        """
        if key in ["default_config", "user_config"]:
            super().__setattr__(key, value)
        else:
            self.user_config[key] = value

    def items(self):
        """返回合并后的 dict_items（user 覆盖 default）。

        是**快照**而非视图：修改返回值不会影响 settings，也不会回写数据库。
        """
        return {**self.default_config, **self.user_config}.items()


settings = Settings(DEFAULT_CONFIG)
