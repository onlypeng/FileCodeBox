from tortoise import connections
from core.logger import logger


async def create_directroom_table():
    """创建 directroom 表（直连快传房间：消息 + 文件一体）"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS directroom (
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
        "CREATE INDEX IF NOT EXISTS idx_directroom_room_code ON directroom(room_code)"
    )
    logger.info("Created directroom table")


async def create_directmessage_table():
    """创建 directmessage 表（房间聊天消息，随房间删除）"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS directmessage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id     INTEGER NOT NULL REFERENCES directroom(id) ON DELETE CASCADE,
            sender_name VARCHAR(50) DEFAULT '' NOT NULL,
            content     TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    await conn.execute_script(
        "CREATE INDEX IF NOT EXISTS idx_directmessage_room_id ON directmessage(room_id)"
    )
    logger.info("Created directmessage table")


async def migrate():
    """迁移入口"""
    try:
        await create_directroom_table()
        await create_directmessage_table()
        logger.info("Migration 016 completed successfully")
    except Exception as e:
        logger.error(f"Migration 016 failed: {e}")
        raise
