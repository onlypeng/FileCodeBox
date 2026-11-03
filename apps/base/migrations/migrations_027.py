"""Migration 027：room_cached_file 增加落盘字段，缓存文件与物理路径建立关联。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.db_compat import column_exists
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
            if not await column_exists('room_cached_file', column):
                await conn.execute_query(ddl)
                logger.info(f"Migration 027: room_cached_file 新增字段 {column}")
        logger.info("Migration 027: 房间缓存文件落盘字段就绪")
    except Exception as e:
        logger.error(f"Migration 027 failed: {e}")
        raise
