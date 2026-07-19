# @Time    : 2023/8/13 20:43
# @Author  : Lan
# @File    : models.py
# @Software: PyCharm
from typing import Optional

from tortoise.models import Model
from tortoise.contrib.pydantic import pydantic_model_creator

from tortoise import fields, models
from datetime import datetime
from core.utils import get_now


def _make_naive(dt):
    """将 datetime 转为 naive（去除时区信息），确保与 get_now() 可比较"""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


class FileCodes(models.Model):
    id = fields.IntField(pk=True)
    code = fields.CharField(max_length=255, unique=True, index=True)
    prefix = fields.CharField(max_length=255, default="")
    suffix = fields.CharField(max_length=255, default="")
    uuid_file_name = fields.CharField(max_length=255, null=True)
    file_path = fields.CharField(max_length=255, null=True)
    size = fields.BigIntField(default=0)
    text = fields.TextField(null=True)
    expired_at = fields.DatetimeField(null=True)
    expired_count = fields.IntField(default=0)
    used_count = fields.IntField(default=0)
    expire_style = fields.CharField(max_length=20, default="day")
    expire_value = fields.IntField(default=1)
    created_at = fields.DatetimeField(auto_now_add=True)
    file_hash = fields.CharField(max_length=64, null=True)
    is_chunked = fields.BooleanField(default=False)
    upload_id = fields.CharField(max_length=36, null=True)
    is_multi_file = fields.BooleanField(default=False)

    async def is_expired(self):
        if self.expired_at is None:
            return False
        if self.expired_at and self.expired_count < 0:
            return _make_naive(self.expired_at) < await get_now()
        return self.expired_count <= 0

    async def get_file_path(self):
        return f"{self.file_path}/{self.uuid_file_name}"


class UploadChunk(models.Model):
    id = fields.IntField(pk=True)
    upload_id = fields.CharField(max_length=36, index=True)
    chunk_index = fields.IntField()
    chunk_hash = fields.CharField(max_length=64)
    total_chunks = fields.IntField()
    file_size = fields.BigIntField()
    chunk_size = fields.IntField()
    file_name = fields.CharField(max_length=255)
    save_path = fields.CharField(max_length=512, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    completed = fields.BooleanField(default=False)


class KeyValue(Model):
    id: Optional[int] = fields.IntField(pk=True)
    key: Optional[str] = fields.CharField(
        max_length=255, description="键", index=True, unique=True
    )
    value: Optional[str] = fields.JSONField(description="值", null=True)
    created_at: Optional[datetime] = fields.DatetimeField(
        auto_now_add=True, description="创建时间"
    )


class PresignUploadSession(models.Model):
    """预签名上传会话模型"""

    id = fields.IntField(pk=True)
    upload_id = fields.CharField(max_length=36, unique=True, index=True)
    file_name = fields.CharField(max_length=255)
    file_size = fields.BigIntField()
    save_path = fields.CharField(max_length=512)
    mode = fields.CharField(max_length=10)  # "direct" 或 "proxy"
    expire_value = fields.IntField(default=1)
    expire_style = fields.CharField(max_length=20, default="day")
    created_at = fields.DatetimeField(auto_now_add=True)
    expires_at = fields.DatetimeField()  # 会话过期时间

    async def is_expired(self):
        """检查会话是否已过期"""
        return _make_naive(self.expires_at) < await get_now()


class FileItem(models.Model):
    """多文件分享中的单个文件项"""
    id = fields.IntField(pk=True)
    file_code = fields.ForeignKeyField("models.FileCodes", related_name="items", on_delete=fields.CASCADE)
    file_name = fields.CharField(max_length=255)
    file_size = fields.BigIntField(default=0)
    file_hash = fields.CharField(max_length=64, null=True)
    storage_path = fields.CharField(max_length=512)
    uuid_file_name = fields.CharField(max_length=255, null=True)
    sort_order = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)

    async def get_full_path(self):
        """获取完整的存储路径"""
        return f"{self.storage_path}/{self.uuid_file_name}"


class CollectionBox(models.Model):
    """收件箱（文件收集）- 三码分离 + expire_style 模式"""
    id = fields.IntField(pk=True)
    collection_code = fields.CharField(max_length=16, unique=True, index=True)
    delivery_code = fields.CharField(max_length=16, unique=True, index=True)
    retrieve_code = fields.CharField(max_length=16, unique=True, index=True, default="")
    title = fields.CharField(max_length=100, default="")
    description = fields.TextField(default="", null=True)
    creator_ip = fields.CharField(max_length=45, default="")
    creator_name = fields.CharField(max_length=50, default="匿名")
    password = fields.CharField(max_length=255, default="", null=True)
    max_files = fields.IntField(default=20)
    # 收件箱过期配置（创建后不可更改）
    expire_style = fields.CharField(max_length=20, default="day")
    expire_value = fields.IntField(default=7)
    expired_at = fields.DatetimeField(null=True)
    # 投件码过期配置
    delivery_expire_style = fields.CharField(max_length=20, default="day")
    delivery_expire_value = fields.IntField(default=7)
    delivery_expired_at = fields.DatetimeField(null=True)
    # 取件码过期配置
    retrieve_expire_style = fields.CharField(max_length=20, default="day")
    retrieve_expire_value = fields.IntField(default=7)
    retrieve_expired_at = fields.DatetimeField(null=True)
    delivery_count = fields.IntField(default=-1)  # 最大投递次数，-1 表示不限
    delivery_used_count = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    async def is_expired(self):
        """收件箱是否过期"""
        if self.expire_style == "forever":
            return False
        if self.expired_at is None:
            return False
        return _make_naive(self.expired_at) < await get_now()

    async def is_delivery_expired(self):
        """投件码是否过期（独立于收件箱过期）"""
        if self.delivery_expire_style == "forever":
            return False
        if self.delivery_expired_at is None:
            return False
        return _make_naive(self.delivery_expired_at) < await get_now()

    async def is_retrieve_expired(self):
        """取件码是否过期（独立于收件箱过期）"""
        if self.retrieve_expire_style == "forever":
            return False
        if self.retrieve_expired_at is None:
            return False
        return _make_naive(self.retrieve_expired_at) < await get_now()


class CollectionFile(models.Model):
    """收件箱中的投递文件 - 关联 FileCode"""
    id = fields.IntField(pk=True)
    collection = fields.ForeignKeyField("models.CollectionBox", related_name="files", on_delete=fields.CASCADE)
    file_code = fields.ForeignKeyField("models.FileCodes", related_name="collection_files", null=True, on_delete=fields.SET_NULL)
    uploader_name = fields.CharField(max_length=50, default="", null=True)
    uploader_ip = fields.CharField(max_length=45, default="")
    status = fields.CharField(max_length=20, default="uploading")
    created_at = fields.DatetimeField(auto_now_add=True)


class ActivityLog(models.Model):
    """活动日志模型"""
    id = fields.IntField(pk=True)
    action = fields.CharField(max_length=50, index=True)
    target_type = fields.CharField(max_length=50, null=True)
    target_id = fields.IntField(null=True)
    user_ip = fields.CharField(max_length=45, default="")
    user_agent = fields.TextField(null=True)
    detail = fields.JSONField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)


file_codes_pydantic = pydantic_model_creator(FileCodes, name="FileCodes")
upload_chunk_pydantic = pydantic_model_creator(UploadChunk, name="UploadChunk")
key_value_pydantic = pydantic_model_creator(KeyValue, name="KeyValue")
presign_upload_session_pydantic = pydantic_model_creator(
    PresignUploadSession, name="PresignUploadSession"
)
file_item_pydantic = pydantic_model_creator(FileItem, name="FileItem")
collection_box_pydantic = pydantic_model_creator(CollectionBox, name="CollectionBox")
collection_file_pydantic = pydantic_model_creator(CollectionFile, name="CollectionFile")
