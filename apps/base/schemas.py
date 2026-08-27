from pydantic import BaseModel, Field
from typing import Optional


class SelectFileModel(BaseModel):
    code: str


class InitChunkUploadModel(BaseModel):
    file_name: str
    chunk_size: int = Field(default=5 * 1024 * 1024, gt=0)
    file_size: int = Field(ge=0)
    file_hash: str


class CompleteUploadModel(BaseModel):
    expire_value: int
    expire_style: str
    remark: str = ""


# 预签名上传相关模型
class PresignUploadInitRequest(BaseModel):
    """预签名上传初始化请求"""
    file_name: str
    file_size: int
    expire_value: int = 1
    expire_style: str = "day"
    remark: str = ""


# 收件箱相关模型
class CreateCollectionRequest(BaseModel):
    """创建收件箱请求 - expire_style 模式

    注意：password 字段已废弃（从未被任何流程校验，前端也无入口设置），
    创建时不再接收，避免给用户"已加密"的虚假安全感。存量 DB 中的值被忽略。
    """
    title: str = ""
    description: Optional[str] = ""
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


class UpdateCollectionConfigRequest(BaseModel):
    """更新收件箱配置请求 - 收件箱过期时间不可更改"""
    delivery_expire_style: Optional[str] = None
    delivery_expire_value: Optional[int] = None
    retrieve_expire_style: Optional[str] = None
    retrieve_expire_value: Optional[int] = None
    max_files: Optional[int] = None


# 跨设备实时房间相关请求（统一为直连快传：聊天 + 文件 + P2P 媒体）
class CreateRoomRequest(BaseModel):
    """创建房间请求"""
    title: str = ""
    expire_style: str = "day"
    expire_value: int = 1
    # 人员上限（1~后台配置的 defaultMaxMembers；0/缺省用后台默认）
    max_members: int = 0
    # 是否把聊天消息/文件元信息保存到服务器（随房间过期删除）
    save_messages: bool = False
    # 是否启用房间文件缓存（已接收成员可作为多源转发，其他成员/新成员可收取）
    cache_enabled: bool = False


# 投递（delivery）分片上传
class InitDeliveryChunkUploadRequest(BaseModel):
    """投递分片上传初始化请求"""
    delivery_code: str
    uploader_name: str = ""
    file_name: str
    file_size: int = Field(ge=0)
    chunk_size: int = Field(default=5 * 1024 * 1024, gt=0)
    file_hash: str
