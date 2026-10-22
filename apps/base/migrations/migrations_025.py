from tortoise import connections
from core.logger import logger


async def migrate():
    """迁移 025：房间历史记录扩展（保存消息房间可查看历史消息与传输过的文件）。

    - room_message 表新增 msg_type / file_name / file_size 列：
      msg_type ∈ text（聊天）/ file（传输文件元信息）；file 时 content 存 transfer_id，
      file_name/file_size 存文件信息，供房间页"历史消息/历史文件"只读展示。
    """
    conn = connections.get("default")
    try:
        for column, ddl in (
            ("msg_type", "VARCHAR(16) DEFAULT 'text'"),
            ("file_name", "VARCHAR(255) DEFAULT ''"),
            ("file_size", "BIGINT DEFAULT 0"),
        ):
            exists = await conn.execute_query(
                "SELECT COUNT(*) AS c FROM pragma_table_info('room_message') WHERE name = ?",
                [column],
            )
            rows = exists[1] if len(exists) > 1 else exists[0]
            count = rows[0]["c"] if rows else 0
            if count == 0:
                await conn.execute_query(f"ALTER TABLE room_message ADD COLUMN {column} {ddl}")
                logger.info(f"Migration 025: room_message 新增字段 {column}")
        logger.info("Migration 025: 房间历史记录字段就绪")
    except Exception as e:
        logger.error(f"Migration 025 failed: {e}")
        raise