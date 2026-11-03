"""Migration 012：移除废弃的 is_active 列；列不存在时安全跳过。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
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
