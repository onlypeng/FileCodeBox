from tortoise import connections
from core.logger import logger


async def migrate():
    """房间表新增 expired_count 列（次数模式的剩余会话次数，-1 表示非次数模式不限制）"""
    try:
        conn = connections.get("default")
        cols = await conn.execute_query("PRAGMA table_info(room)")
        names = {row[1] for row in cols[1]}
        if "expired_count" not in names:
            await conn.execute_script(
                "ALTER TABLE room ADD COLUMN expired_count INT DEFAULT -1 NOT NULL"
            )
            logger.info("Added expired_count column to room table")
        logger.info("Migration 018 completed successfully")
    except Exception as e:
        logger.error(f"Migration 018 failed: {e}")
        raise
