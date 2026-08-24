from tortoise import connections
from core.logger import logger


async def create_iplimit_table():
    """创建 iplimit 表（IP 限流计数，多 worker 共享）"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS iplimit (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket    VARCHAR(20) NOT NULL,
            ip        VARCHAR(45) NOT NULL,
            count     INT DEFAULT 0 NOT NULL,
            last_time TIMESTAMP NOT NULL,
            CONSTRAINT uniq_iplimit_bucket_ip UNIQUE (bucket, ip)
        )
        """
    )
    await conn.execute_script(
        "CREATE INDEX IF NOT EXISTS idx_iplimit_bucket ON iplimit(bucket)"
    )
    logger.info("Created iplimit table")


async def migrate():
    """迁移入口"""
    try:
        await create_iplimit_table()
        logger.info("Migration 019 completed successfully")
    except Exception as e:
        logger.error(f"Migration 019 failed: {e}")
        raise
