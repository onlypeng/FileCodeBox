"""文件柜秒传（同内容只存一份）与共享文件的引用检查。

为什么需要引用检查：FileCodes 的物理文件由 (file_path, uuid_file_name) 定位，
而删除逻辑是"记录过期/被删 → 直接删物理文件"。秒传会让多条记录指向同一份文件，
此时删掉其中任意一条都会把别人的文件一起删掉（表现为下载 404）。
因此：
- 建立秒传时要让新记录复用已有文件的路径
- 删除前必须确认没有别的记录还在引用同一份文件

哈希来源：合并分片时由 storage.merge_chunks 服务端算出（权威值），
不采信客户端声明的哈希——与房间缓存去重保持同一原则。
"""
from __future__ import annotations

from tortoise.expressions import Q

from apps.base.models import FileCodes
from core.logger import logger
from core.utils import get_now


async def find_duplicate_file(file_hash: str, size: int) -> FileCodes | None:
    """按服务端算出的哈希查找可复用的已有文件。

    只匹配未过期、单文件、且确实落盘的记录；命中即返回，未命中返回 None。
    """
    if not file_hash or not size:
        return None
    now = await get_now()
    return await FileCodes.filter(
        file_hash=file_hash,
        size=size,
        is_multi_file=False,
    ).filter(
        Q(expired_count=0)
        | Q(expired_count__lt=0, expired_at__isnull=True)
        | Q(expired_count__lt=0, expired_at__gt=now)
    ).first()


async def is_file_referenced_elsewhere(file_path: str, uuid_file_name: str, exclude_id: int) -> bool:
    """是否还有其它记录引用同一份物理文件（用于删除前判断是否真的能删）。"""
    if not uuid_file_name:
        return False
    return await FileCodes.filter(
        file_path=file_path, uuid_file_name=uuid_file_name
    ).exclude(id=exclude_id).exists()
