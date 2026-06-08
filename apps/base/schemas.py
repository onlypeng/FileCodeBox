from pydantic import BaseModel
from typing import Optional


class SelectFileModel(BaseModel):
    code: str


class InitChunkUploadModel(BaseModel):
    file_name: str
    chunk_size: int = 5 * 1024 * 1024
    file_size: int
    file_hash: str


class CompleteUploadModel(BaseModel):
    expire_value: int
    expire_style: str


# 预签名上传相关模型
class PresignUploadInitRequest(BaseModel):
    """预签名上传初始化请求"""
    file_name: str
    file_size: int
    expire_value: int = 1
    expire_style: str = "day"


class PresignUploadInitResponse(BaseModel):
    """预签名上传初始化响应"""
    upload_id: str
    upload_url: str
    mode: str  # "direct" 或 "proxy"
    save_path: str
    expires_in: int  # URL过期时间（秒）


# 收件箱相关模型
class CreateCollectionRequest(BaseModel):
    """创建收件箱请求 - expire_style 模式"""
    title: str = ""
    description: Optional[str] = ""
    password: Optional[str] = ""
    max_files: int = 20
    # 收件箱过期配置（创建后不可更改）
    expire_style: str = "day"
    expire_value: int = 7
    # 投件码过期配置
    delivery_expire_style: str = "day"
    delivery_expire_value: int = 7
    # 取件码过期配置
    retrieve_expire_style: str = "day"
    retrieve_expire_value: int = 7
    delivery_count: int = -1  # 最大投递次数，-1 表示不限
    # 向后兼容：旧字段自动转换
    expire_days: Optional[int] = None


class CollectionUploadRequest(BaseModel):
    """收件箱投递请求（元数据部分通过Form）"""
    uploader_name: Optional[str] = ""


class UpdateCollectionConfigRequest(BaseModel):
    """更新收件箱配置请求 - 收件箱过期时间不可更改"""
    delivery_expire_style: Optional[str] = None
    delivery_expire_value: Optional[int] = None
    retrieve_expire_style: Optional[str] = None
    retrieve_expire_value: Optional[int] = None
    max_files: Optional[int] = None
