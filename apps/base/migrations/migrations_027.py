from tortoise import connections
from core.logger import logger

# 新增列定义：(列名, DDL 片段)
_NEW_COLUMNS = [
    # 服务器落盘相对路径（相对 data/room_cache）；空串表示仅 P2P 转发、未落盘
    ("storage_key", "ALTER TABLE room_cached_file ADD COLUMN storage_key TEXT DEFAULT ''"),
    # 服务器缓存过期时间；NULL 表示跟随房间生命周期（房间过期/删除时一并清理）
    ("expires_at", "ALTER TABLE room_cached_file ADD COLUMN expires_at DATETIME"),
    # 落盘时间（用于列表排序与展示）
    ("cached_at", "ALTER TABLE room_cached_file ADD COLUMN cached_at DATETIME"),
    # 上传者昵称（列表展示用；owner_id 仅存 client_id，客户端重连后会变）
    ("uploader_name", "ALTER TABLE room_cached_file ADD COLUMN uploader_name TEXT DEFAULT ''"),
]


async def migrate():
    """迁移 027：房间缓存文件支持真实落盘到服务器。

    - room_cached_file 新增 storage_key / expires_at / cached_at / uploader_name 四列。
    - storage_key 为空串时保持旧语义（仅记录 P2P 持有者，不占服务器磁盘）。
    - expires_at 为 NULL 时表示"跟随房间"，由房间过期清理任务统一回收。
    """
    conn = connections.get("default")
    try:
        for column, ddl in _NEW_COLUMNS:
            exists = await conn.execute_query(
                "SELECT COUNT(*) AS c FROM pragma_table_info('room_cached_file') WHERE name = ?",
                [column],
            )
            rows = exists[1] if len(exists) > 1 else exists[0]
            count = rows[0]["c"] if rows else 0
            if count == 0:
                await conn.execute_query(ddl)
                logger.info(f"Migration 027: room_cached_file 新增字段 {column}")
        logger.info("Migration 027: 房间缓存文件落盘字段就绪")
    except Exception as e:
        logger.error(f"Migration 027 failed: {e}")
        raise
