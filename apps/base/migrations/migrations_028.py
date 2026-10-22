from tortoise import connections
from core.logger import logger

# 新增列定义：(列名, DDL 片段)
_NEW_COLUMNS = [
    # 文件内容哈希（去重键）：同房间内相同内容只落盘一份，重复上传仅续期
    ("file_hash", "ALTER TABLE room_message ADD COLUMN file_hash VARCHAR(64) DEFAULT ''"),
    # 服务器落盘相对路径（相对 data/room_cache）；空串 = 未缓存到服务器
    ("storage_key", "ALTER TABLE room_message ADD COLUMN storage_key TEXT DEFAULT ''"),
    # 缓存过期时间；NULL = 跟随房间生命周期
    ("expires_at", "ALTER TABLE room_message ADD COLUMN expires_at DATETIME"),
]


async def migrate():
    """迁移 028：房间消息与文件消息合一。

    此前聊天消息（room_message）和缓存文件（room_cached_file）是两套记录，
    文件消息要在两张表之间关联，导致历史展示重复、顺序错乱。
    现在把缓存信息直接挂在文件消息上：
    - room_message 新增 file_hash / storage_key / expires_at 三列；
    - msg_type=file 的记录即"文件消息 + 缓存元数据"，历史展示只需读这一张表；
    - room_cached_file 继续保留，仅用于 P2P 多源持有者索引（不参与历史展示）。
    """
    conn = connections.get("default")
    try:
        for column, ddl in _NEW_COLUMNS:
            exists = await conn.execute_query(
                "SELECT COUNT(*) AS c FROM pragma_table_info('room_message') WHERE name = ?",
                [column],
            )
            rows = exists[1] if len(exists) > 1 else exists[0]
            count = rows[0]["c"] if rows else 0
            if count == 0:
                await conn.execute_query(ddl)
                logger.info(f"Migration 028: room_message 新增字段 {column}")

        # file_hash 建索引：去重查询按 (room_code, file_hash) 过滤
        idx = await conn.execute_query(
            "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='index' AND name='idx_room_message_file_hash'"
        )
        idx_rows = idx[1] if len(idx) > 1 else idx[0]
        if (idx_rows[0]["c"] if idx_rows else 0) == 0:
            await conn.execute_query(
                "CREATE INDEX IF NOT EXISTS idx_room_message_file_hash ON room_message (room_code, file_hash)"
            )
            logger.info("Migration 028: room_message 建立 (room_code, file_hash) 索引")

        logger.info("Migration 028: 房间消息与文件消息合并字段就绪")
    except Exception as e:
        logger.error(f"Migration 028 failed: {e}")
        raise
