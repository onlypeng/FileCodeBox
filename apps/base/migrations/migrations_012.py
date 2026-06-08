from tortoise import connections
from core.logger import logger


async def remove_is_active_from_collectionbox():
    """从 collectionbox 表删除 is_active 字段（过期判断已改为直接检查 expired_at）"""
    conn = connections.get("default")

    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox DROP COLUMN is_active"
        )
    except Exception:
        logger.info("Column is_active does not exist or already dropped, skipping")

    logger.info("Removed is_active column from collectionbox")


async def migrate():
    """迁移入口"""
    try:
        await remove_is_active_from_collectionbox()
        logger.info("Migration 012 completed successfully")
    except Exception as e:
        logger.error(f"Migration 012 failed: {e}")
        raise
