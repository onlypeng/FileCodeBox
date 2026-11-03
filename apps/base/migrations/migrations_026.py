"""Migration 026：room_cached_file 增加 holder_names，缓存文件可记录持有者昵称。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.db_compat import column_exists
from core.logger import logger


async def migrate():
    """迁移 026：房间缓存文件记录持有者昵称。

    - room_cached_file 表新增 holder_names 列（与 cache_holder_ids 对应的昵称列表），
      供房间页"房间文件缓存"消息框展示共享文件的用户名。
    """
    conn = connections.get("default")
    try:
        if not await column_exists('room_cached_file', "holder_names"):
            await conn.execute_query(
                "ALTER TABLE room_cached_file ADD COLUMN holder_names TEXT DEFAULT ''"
            )
            logger.info("Migration 026: room_cached_file 新增字段 holder_names")
        logger.info("Migration 026: 缓存文件持有者昵称字段就绪")
    except Exception as e:
        logger.error(f"Migration 026 failed: {e}")
        raise