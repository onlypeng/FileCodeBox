from tortoise import connections
from core.logger import logger


async def migrate():
    """迁移 026：房间缓存文件记录持有者昵称。

    - room_cached_file 表新增 holder_names 列（与 cache_holder_ids 对应的昵称列表），
      供房间页"房间文件缓存"消息框展示共享文件的用户名。
    """
    conn = connections.get("default")
    try:
        exists = await conn.execute_query(
            "SELECT COUNT(*) AS c FROM pragma_table_info('room_cached_file') WHERE name = ?",
            ["holder_names"],
        )
        rows = exists[1] if len(exists) > 1 else exists[0]
        count = rows[0]["c"] if rows else 0
        if count == 0:
            await conn.execute_query(
                "ALTER TABLE room_cached_file ADD COLUMN holder_names TEXT DEFAULT ''"
            )
            logger.info("Migration 026: room_cached_file 新增字段 holder_names")
        logger.info("Migration 026: 缓存文件持有者昵称字段就绪")
    except Exception as e:
        logger.error(f"Migration 026 failed: {e}")
        raise