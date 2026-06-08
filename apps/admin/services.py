import os
import time

from core.storage import FileStorageInterface, storages
from core.settings import settings
from core.config import _sync_ip_limits
from apps.base.models import FileCodes, KeyValue, CollectionFile, file_codes_pydantic
from fastapi import HTTPException
from core.utils import hash_password, is_password_hashed


class FileService:
    def __init__(self):
        self.file_storage: FileStorageInterface = storages[settings.file_storage]()

    async def delete_file(self, file_id: int):
        file_code = await FileCodes.get(id=file_id)
        await self.file_storage.delete_file(file_code)
        await file_code.delete()

    async def list_files(self, page: int, size: int, keyword: str = ""):
        offset = (page - 1) * size
        # 获取所有属于收件箱的 FileCode ID
        collection_file_ids = await CollectionFile.filter(
            file_code_id__isnull=False
        ).values_list("file_code_id", flat=True)
        # 过滤掉 None 值并转为列表
        exclude_ids = [fid for fid in collection_file_ids if fid is not None]

        # 查询文件时排除收件箱文件
        query = FileCodes.filter(prefix__icontains=keyword)
        if exclude_ids:
            query = query.exclude(id__in=exclude_ids)

        total = await query.count()
        files = await query.limit(size).offset(offset)

        result = []
        for f in files:
            item = {
                "id": f.id,
                "code": f.code,
                "prefix": f.prefix,
                "suffix": f.suffix,
                "size": f.size,
                "text": f.text,
                "expired_at": f.expired_at.isoformat() if f.expired_at else None,
                "expired_count": f.expired_count,
                "created_at": f.created_at.isoformat() if f.created_at else None,
                "is_multi_file": f.is_multi_file,
                "file_count": 0,
                "file_items": [],
            }
            # 多文件分享，获取子文件列表
            if f.is_multi_file:
                items = await f.items.all().order_by("sort_order")
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
        from core.utils import hash_password, is_password_hashed, encrypt_value, SENSITIVE_KEYS
        admin_token = data.get("admin_token")
        if admin_token is None or admin_token == "":
            raise HTTPException(status_code=400, detail="管理员密码不能为空")

        if not is_password_hashed(admin_token):
            data["admin_token"] = hash_password(admin_token)

        for key, value in data.items():
            if key not in settings.default_config:
                continue
            # 加密敏感字段
            if key in SENSITIVE_KEYS and isinstance(value, str) and value and not value.startswith("enc:"):
                data[key] = encrypt_value(value)
            if key in [
                "errorCount",
                "errorMinute",
                "max_save_seconds",
                "maxCollectionFiles",
                "maxSendFiles",
                "onedrive_proxy",
                "openUpload",
                "port",
                "s3_proxy",
                "uploadCount",
                "uploadMinute",
                "uploadSize",
            ]:
                try:
                    data[key] = int(value)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"配置项 {key} 必须是整数")
            elif key in ["opacity"]:
                try:
                    data[key] = float(value)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"配置项 {key} 必须是数字")

        await KeyValue.filter(key="settings").update(value=data)
        for k, v in data.items():
            settings.__setattr__(k, v)
        _sync_ip_limits()
