from typing import List, Optional

from pydantic import BaseModel


class IDData(BaseModel):
    id: int


class LoginData(BaseModel):
    password: str


class UpdateConfigRequest(BaseModel):
    """配置更新请求 - 白名单校验"""
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


class ExtendExpireRequest(BaseModel):
    """延长过期：目标对象 id + expire_style ∈ day/hour/minute/count/forever；count 时 expire_value 为剩余次数"""
    id: int = 0
    """延长过期：expire_style ∈ day/hour/minute/count/forever；count 时 expire_value 为剩余次数"""
    expire_style: str = "day"
    expire_value: int = 1
    # 收件箱三码延长目标：manage / deliver / retrieve
    target: Optional[str] = None


class UpdateFileRequest(BaseModel):
    """文件管理：修改备注（文本内容）"""
    remark: Optional[str] = None


class UpdateRoomRequest(BaseModel):
    """房间管理：修改人员上限（1~后台 defaultMaxMembers）与功能开关（保存消息/文件缓存）"""
    max_members: Optional[int] = None
    # 保存聊天消息/文件元信息到服务器（0/1）
    save_messages: Optional[int] = None
    # 启用房间文件缓存（已接收成员作为多源转发，0/1）
    cache_enabled: Optional[int] = None
