"""Migration 008：补齐 expire_style 列（时间/次数等过期模式），已存在则跳过。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.logger import logger


async def add_expire_style_columns():
    """为 collectionbox 表添加 expire_style 模式字段"""
    conn = connections.get("default")

    # 添加收件箱过期配置字段
    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox ADD COLUMN expire_style VARCHAR(20) DEFAULT 'day' NOT NULL"
        )
    except Exception:
        logger.info("Column expire_style already exists, skipping")

    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox ADD COLUMN expire_value INT DEFAULT 7 NOT NULL"
        )
    except Exception:
        logger.info("Column expire_value already exists, skipping")

    # 添加投件码过期配置字段
    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox ADD COLUMN delivery_expire_style VARCHAR(20) DEFAULT 'day' NOT NULL"
        )
    except Exception:
        logger.info("Column delivery_expire_style already exists, skipping")

    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox ADD COLUMN delivery_expire_value INT DEFAULT 7 NOT NULL"
        )
    except Exception:
        logger.info("Column delivery_expire_value already exists, skipping")

    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox ADD COLUMN delivery_expired_at TIMESTAMP"
        )
    except Exception:
        logger.info("Column delivery_expired_at already exists, skipping")

    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox ADD COLUMN delivery_count INT DEFAULT -1 NOT NULL"
        )
    except Exception:
        logger.info("Column delivery_count already exists, skipping")

    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox ADD COLUMN delivery_used_count INT DEFAULT 0 NOT NULL"
        )
    except Exception:
        logger.info("Column delivery_used_count already exists, skipping")

    logger.info("Added expire_style columns to collectionbox")


async def migrate_old_data():
    """迁移旧数据：将旧的 expire_count 转换为 delivery_count"""
    conn = connections.get("default")

    # 将旧的 expire_count（如果>0）迁移到 delivery_count
    try:
        await conn.execute_script(
            "UPDATE collectionbox SET delivery_count = expire_count WHERE expire_count > 0"
        )
    except Exception:
        logger.info("expire_count migration skipped (column may not exist)")

    # 将旧的 expired_at 同步到 delivery_expired_at（如果投件码过期时间未设置）
    try:
        await conn.execute_script(
            "UPDATE collectionbox SET delivery_expired_at = expired_at WHERE delivery_expired_at IS NULL AND expired_at IS NOT NULL"
        )
    except Exception:
        logger.info("delivery_expired_at sync skipped")

    logger.info("Migrated old collectionbox data")


async def migrate():
    """迁移入口"""
    try:
        await add_expire_style_columns()
        await migrate_old_data()
        logger.info("Migration 008 completed successfully")
    except Exception as e:
        logger.error(f"Migration 008 failed: {e}")
        raise
