"""Migration 029：清理重复的房间缓存记录并建立唯一索引，杜绝并发写入产生重复条目。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.db_compat import index_exists
from core.logger import logger

# 唯一索引：同一房间内同一 transfer_id 只允许一条缓存记录
_IDX_NAME = "idx_room_cached_file_room_tid"


async def migrate():
    """迁移 029：房间缓存文件 (room_code, transfer_id) 唯一约束。

    上传接口是「先 filter().first() 查、不存在再 create()」，并发下会产生同一
    transfer_id 的多条重复记录（列表重复展示、配额统计重复计入）。
    建唯一索引前先清理历史重复数据：每组只保留 id 最大的一条（最新的落盘信息）。
    """
    conn = connections.get("default")
    try:
        # 1) 清理历史重复（保留每组最新一条）
        dup = await conn.execute_query(
            "SELECT COUNT(*) AS c FROM ("
            "  SELECT room_code, transfer_id FROM room_cached_file"
            "  GROUP BY room_code, transfer_id HAVING COUNT(*) > 1"
            ")"
        )
        rows = dup[1] if len(dup) > 1 else dup[0]
        dup_groups = (rows[0]["c"] if rows else 0)
        if dup_groups:
            await conn.execute_query(
                "DELETE FROM room_cached_file WHERE id NOT IN ("
                "  SELECT MAX(id) FROM room_cached_file GROUP BY room_code, transfer_id"
                ")"
            )
            logger.info(f"Migration 029: 清理重复缓存记录 {dup_groups} 组")

        # 2) 建唯一索引（幂等）
        if not await index_exists(_IDX_NAME):
            await conn.execute_query(
                f"CREATE UNIQUE INDEX {_IDX_NAME} ON room_cached_file (room_code, transfer_id)"
            )
            logger.info(f"Migration 029: room_cached_file 建立 {_IDX_NAME} 唯一索引")

        logger.info("Migration 029: 房间缓存文件唯一约束就绪")
    except Exception as e:
        logger.error(f"Migration 029 failed: {e}")
        raise
