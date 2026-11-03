"""分享侧通用工具：存储路径生成、过期策略换算、取件码生成、文件哈希、IP 限流实例。

## 问题
"文件存到哪""什么时候过期""取件码怎么发"这三件事在上传、分片合并、直传确认、
收件箱投递等多条链路上都要用到。早期各写一份，结果是**过期策略出现了多套口径**
（有的地方把 count 模式当成"一天后过期"），且路径拼接处多次出现过
文件名未净化导致的路径穿越风险。

## 方案
- 路径统一由 `get_file_path_name` / `get_chunk_file_path_name` 生成，
  文件名**必须先经 `sanitize_filename` 净化**再参与拼接（防御 `../` 穿越）。
- 过期策略统一由 `get_expire_info` 换算，返回值与 `FileCodes.is_expired` 的
  判定规则严格对应：**次数模式 `expired_at=None` 且 `expired_count>=0`**。
  改这里必须同步改模型方法。

## 刻意不做
**不做取件码的业务校验**（存在性、口令、过期）——那需要查库且有副作用，
应由视图层完成；本模块的 `get_random_code` 只负责"生成一个不与现存的重复的码"。
"""
import datetime
import hashlib
import os
import uuid
from urllib.parse import unquote

from fastapi import UploadFile, HTTPException
from typing import Optional, Tuple

from apps.base.dependencies import IPRateLimit
from apps.base.models import FileCodes
from core.settings import settings
from core.utils import (
    get_random_string,
    sanitize_filename,
    get_now,
)


async def get_file_path_name(file: UploadFile) -> Tuple[str, str, str, str, str]:
    """生成普通上传的存储路径。返回 (目录, 后缀, 主名, 净化后文件名, 完整路径)。

    副作用：无（不落盘、不查库）。调用方负责建目录与实际写入。
    注意 `file.filename` 可能为空串或含 URL 编码，故先 unquote 再净化。
    """
    today = await get_now()
    storage_path = settings.storage_path.strip("/")
    file_uuid = uuid.uuid4().hex
    filename = await sanitize_filename(unquote(file.filename or ""))
    base_path = f"share/data/{today.strftime('%Y/%m/%d')}/{file_uuid}"
    path = f"{storage_path}/{base_path}" if storage_path else base_path
    prefix, suffix = os.path.splitext(filename)
    save_path = f"{path}/{filename}"
    return path, suffix, prefix, filename, save_path


async def get_chunk_file_path_name(
    file_name: str, upload_id: str
) -> Tuple[str, str, str, str, str]:
    """生成分片上传的存储路径，返回元组含义同 get_file_path_name。

    目录以 `upload_id` 而非 uuid 命名：同一上传的所有分片必须落在同一目录下，
    合并阶段才能按目录整体处理。
    """
    today = await get_now()
    storage_path = settings.storage_path.strip("/")
    # 与 get_file_path_name 一致：文件名必须先净化，防止 ../ 等路径穿越
    # （分片接口的 save_path 会参与本地存储合并/删除的文件路径拼接）
    file_name = await sanitize_filename(unquote(file_name))
    base_path = f"share/data/{today.strftime('%Y/%m/%d')}/{upload_id}"
    path = f"{storage_path}/{base_path}" if storage_path else base_path
    prefix, suffix = os.path.splitext(file_name)
    save_path = f"{path}/{prefix}{suffix}"
    return path, suffix, prefix, file_name, save_path


async def get_expire_info(
    expire_value: int, expire_style: str
) -> Tuple[Optional[datetime.datetime], int, int, str]:
    """把 (过期数值, 过期类型) 换算成落库所需的四元组。

    返回 `(expired_at, expired_count, used_count, code)`，其中 `code` 是顺带生成的
    取件码（生成策略与过期无关，只是复用一次调用省掉一轮数据库往返）。

    边界条件：
    - `expire_style` 不在已知集合内时**不报错**，退化为"1 天后过期"——
      配置可能来自旧版本数据，拒绝写入会让整个上传失败，代价不成比例。
    - `count` / `forever` 返回的 `expired_at` 为 None，对应
      `FileCodes.is_expired` 中"次数型不看时间"的判定。
    """
    expired_count, used_count = -1, 0
    now = await get_now()
    code = None

    expire_styles = {
        "day": lambda: now + datetime.timedelta(days=expire_value),
        "hour": lambda: now + datetime.timedelta(hours=expire_value),
        "minute": lambda: now + datetime.timedelta(minutes=expire_value),
        # 按次模式：无时间上限（次数用尽前永久有效），is_expired 仅按剩余次数判断
        "count": lambda: (None, expire_value),
        "forever": lambda: (None, None),
    }

    if expire_style in expire_styles:
        result = expire_styles[expire_style]()
        if isinstance(result, tuple):
            expired_at, extra = result
            if expire_style == "count":
                expired_count = extra
            elif expire_style == "forever":
                code = await get_random_code()
        else:
            expired_at = result
    else:
        expired_at = now + datetime.timedelta(days=1)

    if not code:
        code = await get_random_code()

    return expired_at, expired_count, used_count, code


async def get_random_code(style: str = "string") -> str:
    """生成一个全新的、库内不重复的取件码。

    用"随机 + 查重重试"而不是预生成码池：码空间足够大，冲突概率极低，
    且码池本身会成为需要保护的高价值目标。重试 20 次仍冲突则 500——
    这是数据库异常的信号，不该静默返回一个可能重复的码。
    """
    for _ in range(20):
        code = await get_random_string()
        if not await FileCodes.filter(code=code).exists():
            return str(code)
    raise HTTPException(500, "生成取件码失败，请重试")


async def calculate_file_hash(file: UploadFile, chunk_size=1024 * 1024) -> str:
    """流式计算 SHA-256（1MB 分块，避免整文件进内存）。

    副作用：会把文件指针 seek 回 0（前后各一次），调用方无需自行复位，
    但**不要**在并发读取同一个 file 对象时调用它。
    """
    sha = hashlib.sha256()
    await file.seek(0)
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        sha.update(chunk)
    await file.seek(0)
    return sha.hexdigest()


# 模块级单例：限流计数在数据库里，实例本身无状态，共享一份即可。
# 注意这里读的是**启动时刻**的配置值——后台改了 errorCount/uploadRateLimitCount 后，
# 由 core.config._sync_ip_limits() 就地更新这些实例的 count/minutes，不要换成新的实例。
ip_limit = {
    "error": IPRateLimit(count=settings.errorCount, minutes=settings.errorMinute, key="error"),
    "upload": IPRateLimit(count=settings.uploadRateLimitCount, minutes=settings.uploadMinute, key="upload"),
}
