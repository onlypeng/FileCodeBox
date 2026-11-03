"""Migration 010：再次校准 expire_style 列，兼容不同历史版本建出的表。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.logger import logger


async def add_expire_fields_to_filecodes():
    """为 filecodes 表添加 expire_style 和 expire_value 字段"""
    conn = connections.get("default")

    try:
        await conn.execute_script(
            "ALTER TABLE filecodes ADD COLUMN expire_style VARCHAR(20) DEFAULT 'day' NOT NULL"
        )
    except Exception:
        logger.info("Column expire_style already exists, skipping")

    try:
        await conn.execute_script(
            "ALTER TABLE filecodes ADD COLUMN expire_value INTEGER DEFAULT 1 NOT NULL"
        )
    except Exception:
        logger.info("Column expire_value already exists, skipping")

    logger.info("Added expire_style and expire_value columns to filecodes")


async def migrate():
    """迁移入口"""
    try:
        await add_expire_fields_to_filecodes()
        logger.info("Migration 010 completed successfully")
    except Exception as e:
        logger.error(f"Migration 010 failed: {e}")
        raise
