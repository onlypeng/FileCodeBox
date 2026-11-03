"""Migration 015：建立 transferroom 表，支撑文件传输房间。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.logger import logger


async def create_transferroom_table():
    """创建 transferroom 表（直连文件传输房间）"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS transferroom (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code    VARCHAR(6) NOT NULL UNIQUE,
            title        VARCHAR(100) DEFAULT '' NOT NULL,
            creator_ip   VARCHAR(45) DEFAULT '' NOT NULL,
            max_members  INT DEFAULT 10 NOT NULL,
            expire_style VARCHAR(20) DEFAULT 'day' NOT NULL,
            expire_value INT DEFAULT 1 NOT NULL,
            expired_at   TIMESTAMP,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    await conn.execute_script(
        "CREATE INDEX IF NOT EXISTS idx_transferroom_room_code ON transferroom(room_code)"
    )
    logger.info("Created transferroom table")


async def migrate():
    """迁移入口"""
    try:
        await create_transferroom_table()
        logger.info("Migration 015 completed successfully")
    except Exception as e:
        logger.error(f"Migration 015 failed: {e}")
        raise
