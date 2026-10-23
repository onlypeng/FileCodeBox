"""全局存储容量统计与配额告警。

文件柜侧此前只有"单文件大小上限"和"房间缓存配额"，缺少**全局**容量视角：
匿名开放上传（openUpload 默认开启）时，磁盘可能被慢慢写满而没有任何提示。

口径说明：FileCodes.size 对单文件是文件本身大小、对多文件是各 FileItem 之和，
因此统计只用 FileCodes.size，不能再叠加 FileItem（会重复计数）。
房间缓存落在 data/room_cache，不进 FileCodes，需单独统计目录大小。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from tortoise.functions import Sum

from apps.base.models import FileCodes
from core.logger import logger
from core.room_cache import CACHE_ROOT
from core.settings import settings


def _dir_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


async def current_storage_usage() -> dict:
    """统计当前已用存储并给出配额占用情况。

    返回 used_bytes / quota_bytes / quota_mb / percent / exceeded / breakdown。
    quota_mb = 0 表示不限制（quota_bytes = 0，percent 与 exceeded 均为 0/False）。
    """
    codes = await FileCodes.all().annotate(total_size=Sum("size")).first()
    code_size = int(getattr(codes, "total_size", 0) or 0) if codes else 0
    # 目录遍历是同步 I/O，放进线程避免阻塞事件循环
    cache_size = await asyncio.to_thread(_dir_size, CACHE_ROOT)

    used = code_size + cache_size
    quota_mb = int(getattr(settings, "globalStorageQuotaMB", 0) or 0)
    quota_bytes = quota_mb * 1024 * 1024 if quota_mb > 0 else 0

    return {
        "used_bytes": used,
        "quota_bytes": quota_bytes,
        "quota_mb": quota_mb,
        "percent": round(used / quota_bytes * 100, 2) if quota_bytes else 0.0,
        "exceeded": bool(quota_bytes and used > quota_bytes),
        "breakdown": {"filecodes": code_size, "room_cache": cache_size},
    }


async def check_global_storage_quota():
    """定期检查全局存储占用，超过阈值时告警。

    - 超过 90% 记 warning，超过 100% 记 error
    - 未配置配额（0）时只周期性记录占用，便于运维观察增长趋势
    """
    while True:
        try:
            usage = await current_storage_usage()
            quota_mb = usage["quota_mb"]
            used_mb = usage["used_bytes"] / 1024 / 1024
            if quota_mb <= 0:
                logger.info(f"存储占用 {used_mb:.1f}MB（未配置全局配额）")
            elif usage["exceeded"]:
                logger.error(
                    f"全局存储已超配额: {used_mb:.1f}MB / {quota_mb}MB，"
                    f"请清理文件或调高配额（后台：全局存储配额）"
                )
            elif usage["percent"] >= 90:
                logger.warning(
                    f"全局存储接近配额: {used_mb:.1f}MB / {quota_mb}MB "
                    f"({usage['percent']}%)"
                )
        except Exception as e:
            logger.error(f"检查全局存储配额异常: {e}")
        finally:
            await asyncio.sleep(1800)
