"""后台请求模型。

## 问题
后台接口直接改全局配置、删文件、改房间，请求体的**字段名本身就是权限边界**：
`UpdateConfigRequest` 里出现什么键，管理员就能改什么。
早期用 `dict` 接收再由服务层过滤，漏一项就是一个"改了不该改的"事故。

## 方案
把白名单前移到 Pydantic 模型：未声明的字段直接被丢弃，
服务层拿到的必然是合法键集合（服务层因此不再重复做白名单判断）。

## 刻意不做
**所有字段都是 Optional 且默认 None**。后台表单是"局部保存"，
None 表示"本次不修改"，与"改成空值"语义不同——
后者由具体服务逻辑（空串/占位符）另行处理。
"""
from typing import List, Optional

from pydantic import BaseModel


class IDData(BaseModel):
    """按主键定位单个对象（删除/详情等）"""
    id: int


class LoginData(BaseModel):
    """管理员登录：仅口令一项。

    刻意**不做账号体系**——本项目是单管理员自建服务，用户名只会增加
    被暴力枚举的维度而不增加安全性；登录失败计数走 IP 限流（error 桶）。
    """
    password: str


class UpdateConfigRequest(BaseModel):
    """配置更新请求 —— **本身就是白名单**。

    只有在这里声明过的字段才会被 `model_dump()` 带出并写入配置；
    未在模型中声明的键，即使前端提交也会被 Pydantic 静默丢弃。
    因此新增可后台修改的配置项，**必须同时**在这里加字段，否则改了不生效。

    ## 为什么 db* 与 redisUrl 故意不在白名单里
    `dbType` / `dbHost` / `dbPort` / `dbName` / `dbUser` / `dbPassword` /
    `dbSchema` / `redisUrl` 属于**基础设施配置**，与业务配置有本质区别：

    - 连接在启动时建立，改内存里的值**不会生效**——表单显示"已保存"，
      实际仍连着旧库/仍是单实例模式，这种"假生效"比不能改更危险，
      排查时往往要花很久才发现配置其实没切换。
    - 一旦真做成热切换，写错一个 host 就是**在线上把服务打成不可用**，
      且管理员此时已无法从后台改回来（后台自己连不上库了）。
    - 它们只在 `DEFAULT_CONFIG` / 环境变量里有意义，本就是部署期决策，
      不是运行期调优项。

    正确做法：通过环境变量（`REDIS_URL` 等，容器部署推荐）或配置文件设定并重启。
    `redisUrl` 支持 `REDIS_URL` 环境变量覆盖，多实例部署请用环境变量。

    ## 仍在白名单里但同样需要重启的项
    `serverWorkers` / `serverHost` / `serverPort` 也是启动期参数，
    保留在白名单里是为了让后台能展示并持久化它们（重启后沿用），
    但管理员需要知道改完不会立刻生效。
    """
    # 仅允许更新的配置项
    file_storage: Optional[str] = None
    storage_path: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    notify_title: Optional[str] = None
    notify_content: Optional[str] = None
    page_explain: Optional[str] = None
    keywords: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_bucket_name: Optional[str] = None
    s3_endpoint_url: Optional[str] = None
    s3_region_name: Optional[str] = None
    s3_signature_version: Optional[str] = None
    s3_hostname: Optional[str] = None
    s3_proxy: Optional[int] = None
    aws_session_token: Optional[str] = None
    onedrive_domain: Optional[str] = None
    onedrive_client_id: Optional[str] = None
    onedrive_username: Optional[str] = None
    onedrive_password: Optional[str] = None
    onedrive_root_path: Optional[str] = None
    onedrive_proxy: Optional[int] = None
    webdav_root_path: Optional[str] = None
    webdav_proxy: Optional[int] = None
    admin_token: Optional[str] = None
    openUpload: Optional[int] = None
    uploadSize: Optional[int] = None
    # 上传文件类型白名单（逗号分隔扩展名，空 = 不限制）
    fileTypeWhitelist: Optional[str] = None
    expireStyle: Optional[list] = None
    uploadMinute: Optional[int] = None
    enableChunk: Optional[int] = None
    # 上传分片大小（MB，默认 5；仅 enableChunk=1 时生效）
    uploadChunkSize: Optional[int] = None
    webdav_url: Optional[str] = None
    webdav_password: Optional[str] = None
    webdav_username: Optional[str] = None
    opacity: Optional[float] = None
    background: Optional[str] = None
    uploadRateLimitCount: Optional[int] = None
    themesSelect: Optional[str] = None
    # 前端 SPA 站点默认主题（light / dark / enterprise）
    spaTheme: Optional[str] = None
    errorMinute: Optional[int] = None
    errorCount: Optional[int] = None
    serverWorkers: Optional[int] = None
    serverHost: Optional[str] = None
    serverPort: Optional[int] = None
    showAdminAddr: Optional[int] = None
    robotsText: Optional[str] = None
    maxMultiFileCount: Optional[int] = None
    maxCollectionFiles: Optional[int] = None
    maxSendFiles: Optional[int] = None
    maxTextLength: Optional[int] = None
    collectionDefaultMaxFiles: Optional[int] = None
    collectionDefaultExpireDays: Optional[int] = None
    # 口令/码位数（4~12）
    codeLength: Optional[int] = None
    trustedProxies: Optional[str] = None
    cors_origins: Optional[str] = None
    # 共享域名：对外分享链接使用的站点地址（留空 = 使用当前访问地址）
    shareDomain: Optional[str] = None
    mediaDefaultQuality: Optional[str] = None
    directRelayEnabled: Optional[int] = None
    directRelaySpeedLimit: Optional[int] = None
    # 房间文件中转分片大小（KB，16~256；默认 64）
    directRelayChunkSize: Optional[int] = None
    # 房间文件中转单文件大小上限（MB；0=不限制）
    directMaxRelaySize: Optional[int] = None
    defaultMaxMembers: Optional[int] = None
    # 过期保存时间（清理宽限）：style ∈ day/hour/minute/forever
    expiredRetentionStyle: Optional[str] = None
    expiredRetentionValue: Optional[int] = None
    directTurnServers: Optional[List[dict]] = None
    # ===== 房间文件缓存 =====
    # 启用房间文件缓存（0/1）
    roomCacheEnabled: Optional[int] = None
    # 单文件大小上限（MB）
    roomCacheMaxSize: Optional[int] = None
    # 单房间总容量配额（MB）
    roomCacheRoomQuota: Optional[int] = None
    # 缓存文件默认保留时长（小时）
    roomCacheDefaultHours: Optional[int] = None
    # ===== 全局容量配额（F2）：0=不限制，达到阈值时后台告警 =====
    globalStorageQuotaMB: Optional[int] = None
    # ===== 审计日志保留天数（0=不清理） =====
    auditLogRetentionDays: Optional[int] = None
    # ===== 病毒扫描钩子 =====
    virusScanEnabled: Optional[int] = None
    virusScanHook: Optional[str] = None
    virusScanTimeoutSec: Optional[int] = None
    # 扫描服务不可用时是否放行（1=放行并告警，0=拒绝上传）
    virusScanFailOpen: Optional[int] = None
    # 可选主题列表（与 directTurnServers 同为结构化列表）
    themesChoices: Optional[List[dict]] = None

    # 白名单到此为止。dbType / dbHost / dbPort / dbName / dbUser / dbPassword /
    # dbSchema / redisUrl **故意不在此列**（基础设施配置，后台改了不生效且会误导运维），
    # 完整理由见本类 docstring；新增字段前请先读那段。


class ExtendExpireRequest(BaseModel):
    """延长/重设过期：目标对象 id + expire_style ∈ day/hour/minute/count/forever。

    `expire_value` 的语义随 style 变化：时间型是"时长"，count 型是"剩余次数"。
    取值合法性由路由显式校验（不在这里用枚举约束），因为风格集合在多处复用，
    改动模型会波及所有引用方。

    `target` 仅收件箱三码延长使用：manage（整箱）/ deliver（投递）/ retrieve（取件）。
    """
    id: int = 0
    """延长过期：expire_style ∈ day/hour/minute/count/forever；count 时 expire_value 为剩余次数"""
    expire_style: str = "day"
    expire_value: int = 1
    # 收件箱三码延长目标：manage / deliver / retrieve
    target: Optional[str] = None


class UpdateFileRequest(BaseModel):
    """文件管理：修改备注（文本分享的内容本体就存在 remark 字段里）。

    只开放 remark：文件名/大小/哈希属于内容事实，允许后台改写会让
    "分享出去的文件"与"实际文件"不一致，且无法被用户察觉。
    """
    remark: Optional[str] = None


class UpdateRoomRequest(BaseModel):
    """房间管理：修改人员上限（1~后台 defaultMaxMembers）与功能开关（保存消息/文件缓存）。

    刻意**不允许改房间码与创建时间**：房间码是一次性协作凭证，
    后台可改等于可以随时劫持一个正在使用的房间。
    `save_messages` / `cache_enabled` 用 int(0/1) 而非 bool，与模型字段类型保持一致。
    """
    max_members: Optional[int] = None
    # 保存聊天消息/文件元信息到服务器（0/1）
    save_messages: Optional[int] = None
    # 启用房间文件缓存（已接收成员作为多源转发，0/1）
    cache_enabled: Optional[int] = None
