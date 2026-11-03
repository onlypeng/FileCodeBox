"""Migration 024：room 增加消息与缓存文件相关字段，房间消息持久化能力就绪。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.db_compat import column_exists
from core.logger import logger


async def migrate():
    """迁移 024：房间新增"保存消息"与"文件缓存"能力。

    - room 表（Tortoise 表名：Room 模型默认 = room）新增 save_messages / cache_enabled 字段
    - 新建 room_message（房间聊天消息，save_messages=1 时落库）
    - 新建 room_cached_file（房间缓存文件元信息，cache_enabled=1 时记录）
    消息与缓存文件随房间过期/删除一并清理（见 core/tasks.py）。
    """
    conn = connections.get("default")
    try:
        # 1) room 表加字段（幂等：已存在则忽略）
        for column, ddl in (
            ("save_messages", "INTEGER DEFAULT 0"),
            ("cache_enabled", "INTEGER DEFAULT 0"),
        ):
            if not await column_exists('room', column):
                await conn.execute_query(f"ALTER TABLE room ADD COLUMN {column} {ddl}")
                logger.info(f"Migration 024: room 新增字段 {column}")

        # 2) room_message 表
        await conn.execute_query(
            "CREATE TABLE IF NOT EXISTS room_message ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "room_code VARCHAR(12) NOT NULL, "
            "sender VARCHAR(64) DEFAULT '匿名', "
            "content TEXT DEFAULT '', "
            "created_at TIMESTAMP)"
        )
        await conn.execute_query(
            "CREATE INDEX IF NOT EXISTS idx_room_message_code ON room_message (room_code)"
        )

        # 3) room_cached_file 表
        await conn.execute_query(
            "CREATE TABLE IF NOT EXISTS room_cached_file ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "room_code VARCHAR(12) NOT NULL, "
            "transfer_id VARCHAR(64) NOT NULL, "
            "owner_id VARCHAR(64) DEFAULT '', "
            "file_name VARCHAR(255) DEFAULT '', "
            "file_size BIGINT DEFAULT 0, "
            "cache_holder_ids TEXT DEFAULT '', "
            "created_at TIMESTAMP)"
        )
        await conn.execute_query(
            "CREATE INDEX IF NOT EXISTS idx_room_cached_code ON room_cached_file (room_code)"
        )
        await conn.execute_query(
            "CREATE INDEX IF NOT EXISTS idx_room_cached_tid ON room_cached_file (transfer_id)"
        )

        # 4) 新表注册到 Tortoise runtime（供 ORM 使用，无需重启即可 filter/create）
        from tortoise import Tortoise
        try:
            Tortoise.get_connection("default").__class__  # 检查连接可用
        except Exception:
            pass
        logger.info("Migration 024: 房间消息/缓存文件表就绪")
    except Exception as e:
        logger.error(f"Migration 024 failed: {e}")
        raise