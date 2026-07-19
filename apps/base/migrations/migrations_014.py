from tortoise import connections
from core.logger import logger


async def migrate():
    """创建 ActivityLog 活动日志表"""
    conn = connections.get("default")

    try:
        result = await conn.execute_query("PRAGMA table_info(activitylog)")
        columns = result[1] if result and len(result) > 1 else []

        if columns:
            logger.info("ActivityLog table already exists, skipping migration 014")
            return

        await conn.execute_script("""
            CREATE TABLE IF NOT EXISTS activitylog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action VARCHAR(50) NOT NULL,
                target_type VARCHAR(50) NULL,
                target_id INTEGER NULL,
                user_ip VARCHAR(45) NOT NULL DEFAULT '',
                user_agent TEXT NULL,
                detail TEXT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute_script("CREATE INDEX IF NOT EXISTS idx_activitylog_action ON activitylog (action)")
        await conn.execute_script("CREATE INDEX IF NOT EXISTS idx_activitylog_target ON activitylog (target_type, target_id)")
        await conn.execute_script("CREATE INDEX IF NOT EXISTS idx_activitylog_created_at ON activitylog (created_at)")

        logger.info("Migration 014 completed successfully")
    except Exception as e:
        logger.error(f"Migration 014 failed: {e}")
        raise