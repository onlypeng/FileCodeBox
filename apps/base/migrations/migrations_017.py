from tortoise import connections
from core.db_compat import table_exists
from core.logger import logger


async def create_room_table():
    """创建 room 表（聊天/文件传输/直连快传合并为一）"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS room (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code    VARCHAR(6) NOT NULL UNIQUE,
            title        VARCHAR(100) DEFAULT '' NOT NULL,
            creator_ip   VARCHAR(45) DEFAULT '' NOT NULL,
            max_members  INT DEFAULT 10 NOT NULL,
            mode         VARCHAR(20) DEFAULT 'direct' NOT NULL,
            expire_style VARCHAR(20) DEFAULT 'day' NOT NULL,
            expire_value INT DEFAULT 1 NOT NULL,
            expired_at   TIMESTAMP,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    await conn.execute_script(
        "CREATE INDEX IF NOT EXISTS idx_room_room_code ON room(room_code)"
    )
    logger.info("Created room table")


async def create_roommessage_table():
    """创建 roommessage 表（房间消息，随房间删除）"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS roommessage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id     INTEGER NOT NULL REFERENCES room(id) ON DELETE CASCADE,
            sender_name VARCHAR(50) DEFAULT '' NOT NULL,
            content     TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    await conn.execute_script(
        "CREATE INDEX IF NOT EXISTS idx_roommessage_room_id ON roommessage(room_id)"
    )
    logger.info("Created roommessage table")


async def migrate_rooms():
    """把旧的 chatroom / transferroom / directroom 数据合并进 room 表"""
    conn = connections.get("default")
    # 先查旧表是否存在（全新安装时可能没有）——跨库判断，不用 sqlite_master
    exist = {
        t
        for t in ("chatroom", "transferroom", "directroom")
        if await table_exists(t)
    }

    for table, mode in (("chatroom", "chat"), ("transferroom", "transfer"), ("directroom", "direct")):
        if table not in exist:
            continue
        await conn.execute_query(
            f"""
            INSERT INTO room (room_code, title, creator_ip, max_members, mode, expire_style, expire_value, expired_at, created_at)
            SELECT room_code, title, creator_ip, max_members, ?, expire_style, expire_value, expired_at, created_at
            FROM {table}
            WHERE room_code NOT IN (SELECT room_code FROM room)
            """,
            [mode],
        )
        logger.info(f"Migrated {table} -> room (mode={mode})")


async def migrate_messages():
    """把旧的 chatmessage / directmessage 数据合并进 roommessage 表"""
    conn = connections.get("default")
    exist = {
        t
        for t in ("chatmessage", "directmessage", "chatroom", "directroom")
        if await table_exists(t)
    }
    if "chatmessage" in exist and "chatroom" in exist:
        await conn.execute_query(
            """
            INSERT INTO roommessage (room_id, sender_name, content, created_at)
            SELECT r.id, m.sender_name, m.content, m.created_at
            FROM chatmessage m
            JOIN chatroom c ON m.room_id = c.id
            JOIN room r ON r.room_code = c.room_code AND r.mode = 'chat'
            """
        )
        logger.info("Migrated chatmessage -> roommessage")
    if "directmessage" in exist and "directroom" in exist:
        await conn.execute_query(
            """
            INSERT INTO roommessage (room_id, sender_name, content, created_at)
            SELECT r.id, m.sender_name, m.content, m.created_at
            FROM directmessage m
            JOIN directroom c ON m.room_id = c.id
            JOIN room r ON r.room_code = c.room_code AND r.mode = 'direct'
            """
        )
        logger.info("Migrated directmessage -> roommessage")


async def migrate():
    """迁移入口：合并聊天/传输/直连房间为统一的 room 表"""
    try:
        await create_room_table()
        await create_roommessage_table()
        await migrate_rooms()
        await migrate_messages()
        logger.info("Migration 017 completed successfully")
    except Exception as e:
        logger.error(f"Migration 017 failed: {e}")
        raise
