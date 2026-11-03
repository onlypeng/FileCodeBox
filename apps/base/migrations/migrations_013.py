"""Migration 013：批量补齐若干历史版本遗漏的列，避免旧库升级后运行时报 no such column。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.logger import logger


async def add_remark_columns():
    """为 filecodes 和 presignuploadsession 表添加 remark 备注字段"""
    conn = connections.get("default")

    for table, column in (("filecodes", "remark"), ("presignuploadsession", "remark")):
        try:
            await conn.execute_script(
                f"ALTER TABLE {table} ADD COLUMN {column} TEXT"
            )
            logger.info(f"Added {column} column to {table}")
        except Exception:
            logger.info(f"Column {column} in {table} does not exist or already added, skipping")

    logger.info("Added remark columns to filecodes and presignuploadsession")


async def migrate():
    """迁移入口"""
    try:
        await add_remark_columns()
        logger.info("Migration 013 completed successfully")
    except Exception as e:
        logger.error(f"Migration 013 failed: {e}")
        raise
