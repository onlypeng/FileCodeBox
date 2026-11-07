"""面向匿名用户的请求模型（分享取件、上传、收件箱、房间）。

## 问题
这些接口的调用方是浏览器里的匿名用户，请求体完全不可信；同时它们又是最容易被
暴力枚举/刷量的入口。校验散落在视图里就会出现"某个接口忘了限长"这类漏洞。

## 方案
统一用 Pydantic 模型收口入参：类型与取值范围（`Field(gt=0)`、`Field(ge=0)`）
在边界处一次校验完，视图里不再重复判断。

## 刻意不做
**不在这里做业务校验**（取件码是否存在、口令是否正确、是否过期）。
那些判断需要查库，放在模型里会让校验变成有副作用的操作，
也拿不到请求上下文（IP、Header）。本模块只保证"字段形态合法"。
"""
from pydantic import BaseModel, Field
from typing import Optional

from core.settings import settings


class SelectFileModel(BaseModel):
    """取件请求：取件码 + 可选的二次口令。"""
    code: str
    # 提取口令（二次口令）：分享创建时可选设置，未设置时忽略该字段
    access_code: str = ""


class RevokeFileModel(BaseModel):
    """提前作废一条分享：需要发起者凭证，仅知道取件码不足以撤销。"""
    code: str
    owner_token: str = ""


class InitChunkUploadModel(BaseModel):
    """分片上传初始化请求。

    `file_hash` 由客户端声明，仅用于**秒传判定**，服务端不信任它：
    命中秒传后真实内容以服务端既有文件为准，落盘时服务端会自行重算哈希。
    """
    file_name: str
    chunk_size: int = Field(default=5 * 1024 * 1024, gt=0)
    file_size: int = Field(ge=0)
    file_hash: str


class CompleteUploadModel(BaseModel):
    """分片合并请求：合并时才确定过期策略与二次口令。

    刻意让过期策略在**合并阶段**才提交，而不是初始化时：初始化到合并之间可能
    间隔很久（用户中途关页面就永远不合并），过早写入会产生大量"有文件无分享"的垃圾。
    """
    expire_value: int
    expire_style: str
    remark: str = ""
    # 提取口令（二次口令），可选；为空表示不需要
    access_code: str = ""


# 预签名上传相关模型
class PresignUploadInitRequest(BaseModel):
    """预签名上传初始化请求"""
    file_name: str
    file_size: int
    expire_value: int = 1
    expire_style: str = "day"
    remark: str = ""


class PresignConfirmRequest(BaseModel):
    """直传确认请求：只有二次口令需要客户端在确认时补上（其余取会话里的值）。"""
    access_code: str = ""


# 收件箱相关模型
class CreateCollectionRequest(BaseModel):
    """创建收件箱请求 - expire_style 模式

    注意：password 字段已废弃（从未被任何流程校验，前端也无入口设置），
    创建时不再接收，避免给用户"已加密"的虚假安全感。存量 DB 中的值被忽略。
    """
    title: str = ""
    description: Optional[str] = ""
    # 默认值从运行时配置读取（default_factory 每次实例化都求值，后台改了立即生效），
    # 而不是硬编码 20/7 —— 否则后台的 collectionDefault* 对不带参数的调用方永远无效。
    max_files: int = Field(default_factory=lambda: int(getattr(settings, "collectionDefaultMaxFiles", 20) or 20))
    # 收件箱过期配置（创建后不可更改）
    expire_style: str = "day"
    expire_value: int = Field(default_factory=lambda: int(getattr(settings, "collectionDefaultExpireDays", 7) or 7))
    # 投件码过期配置
    delivery_expire_style: str = "day"
    # 子码默认有效期必须跟随收件箱默认有效期：后台把 collectionDefaultExpireDays
    # 调小后，子码若仍固定 7 天会触发「投件码过期时间不能超过收件箱过期时间」
    # 的校验，让不带参数的创建请求被自己的默认值拒掉（配置改了反而功能不可用）。
    delivery_expire_value: int = Field(default_factory=lambda: int(getattr(settings, "collectionDefaultExpireDays", 7) or 7))
    # 取件码过期配置
    retrieve_expire_style: str = "day"
    retrieve_expire_value: int = Field(default_factory=lambda: int(getattr(settings, "collectionDefaultExpireDays", 7) or 7))
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
    """创建房间请求。

    `save_messages` / `cache_enabled` 一旦开启就会在服务端留下数据副本，
    因此必须由创建者显式勾选，默认关闭（不落库、不占磁盘）。
    """
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
    """投递分片上传初始化请求（收件箱投件侧）。

    与分享侧的分片上传刻意分开建模：投件需要 `delivery_code` 与 `uploader_name`，
    且**没有过期策略参数**——投递出的文件继承收件箱自身的过期配置，
    不让投递者决定文件能活多久。
    """
    delivery_code: str
    uploader_name: str = ""
    file_name: str
    file_size: int = Field(ge=0)
    chunk_size: int = Field(default=5 * 1024 * 1024, gt=0)
    file_hash: str
