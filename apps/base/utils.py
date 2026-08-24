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
    for _ in range(20):
        code = await get_random_string()
        if not await FileCodes.filter(code=code).exists():
            return str(code)
    raise HTTPException(500, "生成取件码失败，请重试")


async def calculate_file_hash(file: UploadFile, chunk_size=1024 * 1024) -> str:
    sha = hashlib.sha256()
    await file.seek(0)
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        sha.update(chunk)
    await file.seek(0)
    return sha.hexdigest()


ip_limit = {
    "error": IPRateLimit(count=settings.errorCount, minutes=settings.errorMinute, key="error"),
    "upload": IPRateLimit(count=settings.uploadRateLimitCount, minutes=settings.uploadMinute, key="upload"),
}
