"""把 KeyValue 里的配置加载到运行时 Settings。

## 问题

管理员在后台改的配置存在数据库 KeyValue(key="settings") 里，而 Python 侧的
默认配置在 `core/settings.py`。两者需要合并，且涉及敏感字段（对象存储密钥、
网盘密码）——它们入库前已加密，读取时必须还原成明文，否则业务侧拿着密文去连
S3/WebDAV 必然失败。

## 方案

`refresh_settings()` 一次性重灌 `settings.user_config`：先清空再逐条写入，
避免删掉的键残留在内存里（管理员清掉某项后应当回落到默认表）。
写完后调用 `_sync_ip_limits()`：`apps.base.utils.ip_limit` 里的限流窗口与阈值
是从 settings 拷进去的，不同步的话后台改了限流但实际仍是旧值。

## 刻意不做

不做"配置变更事件通知"与热重载：有些配置（数据库类型、Redis 地址）天然无法
运行时切换，与其让一半的配置能热更新、另一半不能（最容易误导人的状态），
不如统一要求"改完在后台保存、结构性配置需重启"，这一点在后台 UI 里写明。
"""
from apps.base.models import KeyValue
from apps.base.utils import ip_limit
from core.settings import DEFAULT_CONFIG, settings


async def ensure_settings_row() -> None:
    """保证 KeyValue 里存在 settings 行。

    get_or_create 的默认值是**全量 DEFAULT_CONFIG**：首次启动即把出厂配置固化入库，
    之后的新增配置项不会被自动写进去（仍靠 default_config 回落），
    这样"升级后新增了配置键"不会把正在运行的值覆盖回默认。
    """
    await KeyValue.get_or_create(key="settings", defaults={"value": DEFAULT_CONFIG})


def _sync_ip_limits() -> None:
    ip_limit["error"].minutes = settings.errorMinute
    ip_limit["error"].count = settings.errorCount
    ip_limit["upload"].minutes = settings.uploadMinute
    ip_limit["upload"].count = settings.uploadRateLimitCount


async def refresh_settings() -> None:
    """从数据库读取最新配置并应用到运行时。

    副作用：整体替换 `settings.user_config`（因此所选密钥类配置项会有一次短暂的
    明文驻留窗口），并重置 `apps.base.utils.ip_limit` 的窗口与阈值。
    敏感值只在形式符合（SENSITIVE_KEYS 且带 enc:/fe: 前缀）时才解密，
    解密失败会保留原值并告警 —— 宁可配置不生效，也不要让登录/存储整体不可用。
    """
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
