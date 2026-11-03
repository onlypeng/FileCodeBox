"""Migration 025：room_message 增加历史记录字段，支持断线重连后拉取历史消息。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.db_compat import column_exists
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
            if not await column_exists('room_message', column):
                await conn.execute_query(f"ALTER TABLE room_message ADD COLUMN {column} {ddl}")
                logger.info(f"Migration 025: room_message 新增字段 {column}")
        logger.info("Migration 025: 房间历史记录字段就绪")
    except Exception as e:
        logger.error(f"Migration 025 failed: {e}")
        raise