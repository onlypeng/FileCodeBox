from apps.base.models import KeyValue
from apps.base.utils import ip_limit
from core.settings import DEFAULT_CONFIG, settings


async def ensure_settings_row() -> None:
    await KeyValue.get_or_create(key="settings", defaults={"value": DEFAULT_CONFIG})


def _sync_ip_limits() -> None:
    ip_limit["error"].minutes = settings.errorMinute
    ip_limit["error"].count = settings.errorCount
    ip_limit["upload"].minutes = settings.uploadMinute
    ip_limit["upload"].count = settings.uploadRateLimitCount


async def refresh_settings() -> None:
    """从数据库读取最新配置并应用到运行时。"""
    from core.utils import decrypt_value, SENSITIVE_KEYS
    config_record = await KeyValue.filter(key="settings").first()
    raw_config = config_record.value if config_record and config_record.value else {}
    # 解密敏感字段
    settings.user_config = {}
    for k, v in raw_config.items():
        if k in SENSITIVE_KEYS and isinstance(v, str) and v.startswith("enc:"):
            settings.user_config[k] = decrypt_value(v)
        else:
            settings.user_config[k] = v
    _sync_ip_limits()
