# @Time    : 2023/8/15 09:51
# @Author  : Lan
# @File    : settings.py
# @Software: PyCharm
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
data_root = BASE_DIR / "data"

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
    # 临时房间默认人员上限（创建时可在 1~该值 之间选择；0 表示不限制）
    "defaultMaxMembers": 10,
    # 共享视频流默认质量档位（low=流畅 / sd=标清 / hd=高清 / auto=按带宽自适应）
    "mediaDefaultQuality": "auto",
    # WebRTC TURN 中继服务器列表（对称 NAT 下 P2P 失败时兜底直连）
    # 格式: [{"urls": "turn:host:3478?transport=udp", "username": "xxx", "credential": "yyy"}]
    "directTurnServers": [],
}


class Settings:
    def __init__(self, defaults=None):
        self.default_config = defaults or {}
        self.user_config = {}

    def __getattr__(self, attr):
        if attr in self.user_config:
            return self.user_config[attr]
        if attr in self.default_config:
            return self.default_config[attr]
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{attr}'"
        )

    def __setattr__(self, key, value):
        if key in ["default_config", "user_config"]:
            super().__setattr__(key, value)
        else:
            self.user_config[key] = value

    def items(self):
        return {**self.default_config, **self.user_config}.items()


settings = Settings(DEFAULT_CONFIG)
