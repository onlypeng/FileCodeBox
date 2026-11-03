"""Migration 014：建立 chatroom 表，支撑跨设备聊天房间。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.logger import logger


async def create_chatroom_table():
    """创建 chatroom 表（跨设备文本消息房间）"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS chatroom (
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
        "CREATE INDEX IF NOT EXISTS idx_chatroom_room_code ON chatroom(room_code)"
    )
    logger.info("Created chatroom table")


async def create_chatmessage_table():
    """创建 chatmessage 表（聊天消息，随房间删除）"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS chatmessage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id     INTEGER NOT NULL REFERENCES chatroom(id) ON DELETE CASCADE,
            sender_name VARCHAR(50) DEFAULT '' NOT NULL,
            content     TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    await conn.execute_script(
        "CREATE INDEX IF NOT EXISTS idx_chatmessage_room_id ON chatmessage(room_id)"
    )
    logger.info("Created chatmessage table")


async def migrate():
    """迁移入口"""
    try:
        await create_chatroom_table()
        await create_chatmessage_table()
        logger.info("Migration 014 completed successfully")
    except Exception as e:
        logger.error(f"Migration 014 failed: {e}")
        raise
