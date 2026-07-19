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
    "name": "文件快递柜 - FileCodeBox",
    "description": "开箱即用的文件快传系统",
    "notify_title": "系统通知",
    "notify_content": '欢迎使用 FileCodeBox，本程序开源于 <a href="https://github.com/vastsa/FileCodeBox" target="_blank">Github</a> ，欢迎Star和Fork。',
    "page_explain": "请勿上传或分享违法内容。根据《中华人民共和国网络安全法》、《中华人民共和国刑法》、《中华人民共和国治安管理处罚法》等相关规定。 传播或存储违法、违规内容，会受到相关处罚，严重者将承担刑事责任。本站坚决配合相关部门，确保网络内容的安全，和谐，打造绿色网络环境。",
    "keywords": "FileCodeBox, 文件快递柜, 口令传送箱, 匿名口令分享文本, 文件",
    "s3_access_key_id": "",
    "s3_secret_access_key": "",
    "s3_bucket_name": "",
    "s3_endpoint_url": "",
    "s3_region_name": "auto",
    "s3_signature_version": "s3v2",
    "s3_hostname": "",
    "s3_proxy": 0,
    "max_save_seconds": 0,
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
    "uploadSize": 1024 * 1024 * 10,
    "expireStyle": ["day", "hour", "minute", "forever", "count"],
    "uploadMinute": 1,
    "enableChunk": 0,
    "webdav_url": "",
    "webdav_password": "",
    "webdav_username": "",
    "opacity": 0.9,
    "background": "",
    "uploadCount": 10,
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
    "collectionDefaultMaxFiles": 20,
    "collectionDefaultExpireDays": 7,
    "trustedProxies": "",
    "codeDigitCount": 8,
    "code_generate_type": "number",
}

# 码位数范围限制
CODE_LENGTH_MIN = 5
CODE_LENGTH_MAX = 15

ADMIN_SESSION_EXPIRE_MIN = 30
ADMIN_SESSION_EXPIRE_MAX = 720

# 数值配置项范围限制：field -> (min, max, 描述)
# min/max 均为闭区间；若 min 允许 0，需在描述中说明 0 的含义
CONFIG_NUMERIC_LIMITS = {
    # 访问保护 - 不允许 0 或负数（0 分钟窗口或 0 次错误会立即封禁所有用户）
    "errorMinute": (1, 1440, "检测时间窗口（分钟）必须在 1-1440 之间"),
    "errorCount": (1, 1000, "允许错误次数必须在 1-1000 之间"),
    # 上传限流 - 不允许 0 或负数（0 次上传或 0 分钟窗口会阻止所有上传）
    "uploadMinute": (1, 1440, "上传限流时间窗口（分钟）必须在 1-1440 之间"),
    "uploadCount": (1, 1000, "允许上传文件数必须在 1-1000 之间"),
    # 文件数量上限 - 不允许 0 或负数
    "maxSendFiles": (1, 100, "发送文件数量上限必须在 1-100 之间"),
    "maxCollectionFiles": (1, 1000, "收件箱最大文件数上限必须在 1-1000 之间"),
    "maxMultiFileCount": (1, 100, "多文件分享数量上限必须在 1-100 之间"),
    "collectionDefaultMaxFiles": (1, 1000, "收件箱默认文件数必须在 1-1000 之间"),
    # 码位数 - 已有定义
    "codeDigitCount": (CODE_LENGTH_MIN, CODE_LENGTH_MAX, f"码位数必须在 {CODE_LENGTH_MIN}-{CODE_LENGTH_MAX} 之间"),
    # 存储大小 - 0 表示不限，否则至少 1KB
    "uploadSize": (1024, 1024 * 1024 * 1024 * 10, "单文件大小上限必须在 1KB-10GB 之间"),
    # 最长保存时间 - 0 表示不限，否则至少 60 秒（1分钟）
    "max_save_seconds": (0, 365 * 24 * 3600, "最长保存时间必须在 0-365天 之间（0 表示不限制）"),
    # 透明度 - 浮点数
    "opacity": (0.1, 1.0, "透明度必须在 0.1-1.0 之间"),
}

# 布尔型配置项（0 或 1）
CONFIG_BOOLEAN_KEYS = {
    "openUpload",
    "enableChunk",
    "s3_proxy",
    "onedrive_proxy",
    "webdav_proxy",
    "showAdminAddr",
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
