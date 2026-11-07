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

from apps.base.models import FileCodes, FileItem
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
        Q(expired_count__gt=0)
        | Q(expired_count__lt=0, expired_at__isnull=True)
        | Q(expired_count__lt=0, expired_at__gt=now)
    ).first()


async def is_file_referenced_elsewhere(file_path: str, uuid_file_name: str, exclude_id: int) -> bool:
    """是否还有其它记录引用同一份物理文件（用于删除前判断是否真的能删）。

    同时查 FileCodes（单文件秒传）与 FileItem（多文件子项去重）两个来源：
    只查前者会让多文件分享的子文件漏检。空路径/空文件名无法定位物理文件，
    直接返回 False（纯文本分享本来就没有物理文件）。
    """
    if not file_path or not uuid_file_name:
        return False
    if await FileCodes.filter(
        file_path=file_path, uuid_file_name=uuid_file_name
    ).exclude(id=exclude_id).exists():
        return True
    return await FileItem.filter(
        storage_path=file_path, uuid_file_name=uuid_file_name
    ).exclude(file_code_id=exclude_id).exists()


async def physical_targets(file_code: FileCodes) -> list[tuple[str, str]]:
    """该记录实际占用的物理文件 (目录, 文件名) 列表。

    单文件由 FileCodes 自身定位；**多文件分享的 FileCodes.uuid_file_name 恒为 None**，
    物理文件由 FileItem 逐项管理。旧逻辑只调 delete_file(file_code) 会拼出
    `path/None` 这种无效路径，导致多文件分享撤销/删除/过期后物理文件永远残留。
    """
    if file_code.is_multi_file:
        items = await FileItem.filter(file_code_id=file_code.id).all()
        return [(it.storage_path, it.uuid_file_name) for it in items]
    return [(file_code.file_path, file_code.uuid_file_name)]


async def delete_physical_files(file_storage, file_code: FileCodes, logger) -> None:
    """删除一条分享记录关联的物理文件（单文件 / 多文件统一收口）。

    每个物理文件删除前都做引用检查：秒传会让多条记录共享同一份文件。
    用临时 FileCodes 对象复用各存储后端已实现的 delete_file，避免为多文件
    引入各后端实现程度不一的 delete_file_by_path 依赖。
    吞异常与既有清理任务口径一致：删文件失败不阻断记录删除与下一轮清理。
    """
    for path, name in await physical_targets(file_code):
        if not path or not name:
            continue
        try:
            if await is_file_referenced_elsewhere(path, name, file_code.id):
                logger.info(f"文件仍被其它记录引用，仅删除记录: code={file_code.code}")
                continue
            await file_storage.delete_file(
                FileCodes(file_path=path, uuid_file_name=name)
            )
        except Exception as e:
            logger.error(f"删除物理文件失败 code={file_code.code}: {e}")
