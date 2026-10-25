from core.storage import FileStorageInterface, storages
from core.settings import settings
from core.config import _sync_ip_limits
from core.file_dedup import is_file_referenced_elsewhere
from core.audit import record
from core.logger import logger
from apps.base.models import FileCodes, KeyValue
from fastapi import HTTPException
from core.utils import hash_password, is_password_hashed


class FileService:
    def __init__(self):
        self.file_storage: FileStorageInterface = storages[settings.file_storage]()

    async def delete_file(self, file_id: int):
        file_code = await FileCodes.filter(id=file_id).first()
        if not file_code:
            raise HTTPException(status_code=404, detail="文件不存在")
        # 秒传会让多条记录共享同一份物理文件：仍有其它记录引用时只删记录，
        # 否则会把别人的文件一起删掉（表现为下载 404）
        if not await is_file_referenced_elsewhere(
            file_code.file_path, file_code.uuid_file_name, file_code.id
        ):
            await self.file_storage.delete_file(file_code)
        else:
            logger.info(f"文件仍被其它记录引用，仅删除记录: code={file_code.code}")
        await record("delete", target_type="filecode", target_id=str(file_code.code),
                     result="success", detail="后台删除文件", actor="admin")
        await file_code.delete()

    async def list_files(self, page: int, size: int, keyword: str = "", status: str = ""):
        from core.utils import get_now, pagination
        from tortoise.expressions import Q

        page, offset = pagination(page, size)
        now = await get_now()
        # 排除收件箱文件：用反向关联 JOIN 完成（collection_files 关联存在即排除），
        # 避免每次分页请求全表加载 CollectionFile 的 file_code_id 列表到内存
        query = FileCodes.exclude(collection_files__id__isnull=False).filter(prefix__icontains=keyword)

        # 状态筛选（多选，逗号分隔：active=有效 / expired=已过期；两者都选=全部）
        # 过期条件：时间过期（expired_at < now 且 非次数模式）或 次数用尽（次数模式且剩余 <= 0）
        time_expired = Q(expired_at__lt=now, expired_count__lt=0)
        count_expired = Q(expired_count__lte=0, expired_count__gte=0)
        expired_q = time_expired | count_expired
        statuses = [s for s in status.split(",") if s in ("active", "expired")]
        if statuses and "active" not in statuses:
            query = query.filter(expired_q)          # 仅 expired
        elif statuses and "expired" not in statuses:
            query = query.exclude(expired_q)         # 仅 active
        # active+expired 或空 → 不过滤（全部）

        total = await query.count()
        # prefetch items 消除多文件子项列表的 N+1 查询
        files = await query.limit(size).offset(offset).prefetch_related("items")

        result = []
        for f in files:
            item = {
                "id": f.id,
                "code": f.code,
                "prefix": f.prefix,
                "suffix": f.suffix,
                "size": f.size,
                "remark": f.remark or None,  # 文本分享内容（备注字段）
                "used_count": f.used_count,
                "expired_at": f.expired_at.isoformat() if f.expired_at else None,
                "expired_count": f.expired_count,
                "expire_style": f.expire_style,
                "expire_value": f.expire_value,
                "created_at": f.created_at.isoformat() if f.created_at else None,
                "is_multi_file": f.is_multi_file,
                "file_count": 0,
                "file_items": [],
            }
            # 多文件分享，获取子文件列表（已预取，无需逐条查询）
            if f.is_multi_file:
                items = f.items or []
                item["file_count"] = len(items)
                item["file_items"] = [
                    {
                        "id": i.id,
                        "file_name": i.file_name,
                        "file_size": i.file_size,
                    }
                    for i in items
                ]
            result.append(item)

        return result, total


class ConfigService:
    def get_config(self):
        from core.utils import SENSITIVE_KEYS
        config = dict(settings.items())
        # 脱敏敏感字段
        for key in SENSITIVE_KEYS:
            if key in config and config[key]:
                config[key] = "******"
        return config

    async def update_config(self, data: dict):
        from core.utils import hash_password, is_password_hashed, encrypt_value, decrypt_value, is_encrypted, SENSITIVE_KEYS

        # 读取现有配置，用于继承"脱敏占位符"对应的真实凭据
        config_record = await KeyValue.filter(key="settings").first()
        existing_config = config_record.value if config_record and config_record.value else {}

        # 管理员密码：留空或占位符表示不修改（防止误操作改坏密码）
        admin_token = data.get("admin_token")
        if admin_token is None or str(admin_token).strip() == "" or admin_token == "******":
            data.pop("admin_token", None)
        elif not is_password_hashed(admin_token):
            data["admin_token"] = hash_password(admin_token)

        for key, value in data.items():
            if key not in settings.default_config:
                continue
            # 跳过脱敏占位符/空值：继承数据库中的真实凭据（空值表示不修改）
            if key in SENSITIVE_KEYS and (value == "******" or value == ""):
                if key in existing_config:
                    data[key] = existing_config[key]
                else:
                    data.pop(key, None)
                continue
            # 加密敏感字段（跳过已加密值）
            if key in SENSITIVE_KEYS and isinstance(value, str) and value and not is_encrypted(value):
                data[key] = encrypt_value(value)
            if key in [
                "errorCount",
                "errorMinute",
                "maxCollectionFiles",
                "maxSendFiles",
                "maxTextLength",
                "onedrive_proxy",
                "openUpload",
                "port",
                "s3_proxy",
                "uploadRateLimitCount",
                "uploadMinute",
                "uploadSize",
                "directRelayEnabled",
                "directRelaySpeedLimit",
                "expiredRetentionValue",
            ]:
                try:
                    data[key] = int(value)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"配置项 {key} 必须是整数")
                # 限速/配额/时长类配置不允许负数
                if data[key] < 0 and key in {
                    "errorCount", "errorMinute", "maxCollectionFiles",
                    "maxSendFiles", "maxTextLength", "uploadRateLimitCount", "uploadMinute",
                    "uploadSize", "directRelaySpeedLimit", "expiredRetentionValue",
                }:
                    raise HTTPException(status_code=400, detail=f"配置项 {key} 不能为负数")
            elif key in ["opacity"]:
                try:
                    data[key] = float(value)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"配置项 {key} 必须是数字")

        await KeyValue.filter(key="settings").update(
            value={**existing_config, **{k: v for k, v in data.items() if k in settings.default_config}}
        )
        for k, v in data.items():
            # 敏感字段运行时使用解密后的明文，避免写入密文到内存配置
            if k in SENSITIVE_KEYS and is_encrypted(v):
                settings.__setattr__(k, decrypt_value(v))
            else:
                settings.__setattr__(k, v)
        _sync_ip_limits()
