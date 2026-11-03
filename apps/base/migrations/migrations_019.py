"""Migration 019：建立 iplimit 表，把失败限流计数从进程内存迁到数据库。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
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
