"""Migration 011：补齐 retrieve_expire_style 列，收件箱可独立设置取件有效期。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.logger import logger


async def add_retrieve_expire_fields_to_collectionbox():
    """为 collectionbox 表添加取件码过期字段"""
    conn = connections.get("default")

    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox ADD COLUMN retrieve_expire_style VARCHAR(20) DEFAULT 'day' NOT NULL"
        )
    except Exception:
        logger.info("Column retrieve_expire_style already exists, skipping")

    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox ADD COLUMN retrieve_expire_value INTEGER DEFAULT 7 NOT NULL"
        )
    except Exception:
        logger.info("Column retrieve_expire_value already exists, skipping")

    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox ADD COLUMN retrieve_expired_at TIMESTAMP NULL"
        )
    except Exception:
        logger.info("Column retrieve_expired_at already exists, skipping")

    logger.info("Added retrieve_expire_style, retrieve_expire_value, retrieve_expired_at columns to collectionbox")


async def migrate():
    """迁移入口"""
    try:
        await add_retrieve_expire_fields_to_collectionbox()
        logger.info("Migration 011 completed successfully")
    except Exception as e:
        logger.error(f"Migration 011 failed: {e}")
        raise
