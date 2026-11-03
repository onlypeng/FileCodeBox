"""Migration 030：room_cached_file 增加 file_hash 并回填，支撑房间内秒传与内容校验。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.db_compat import column_exists, index_exists
from core.logger import logger

# 新增列定义：(列名, DDL 片段)
_NEW_COLUMNS = [
    # 内容 SHA-256（服务端落盘时流式计算，非客户端声明值）
    ("file_hash", "ALTER TABLE room_cached_file ADD COLUMN file_hash VARCHAR(64) DEFAULT ''"),
]


async def migrate():
    """迁移 030：房间缓存文件记录内容哈希。

    - room_cached_file 新增 file_hash 列，下发后供客户端校验"多源收取并合并"的
      文件是否完整（此前合并结果只能比对总大小，无法校验内容）。
    - 回填：从 room_message 中 msg_type=file 且 content=transfer_id 的记录取
      file_hash（同一文件在上传时已在消息侧记录了哈希）。
    - 注意：file_hash 由服务端在落盘时流式计算，客户端声明值不再采信，
      避免伪造哈希造成"内容不同却判定为重复"的缓存投毒。
    """
    conn = connections.get("default")
    try:
        for column, ddl in _NEW_COLUMNS:
            if not await column_exists('room_cached_file', column):
                await conn.execute_query(ddl)
                logger.info(f"Migration 030: room_cached_file 新增字段 {column}")

        # 回填历史数据：从文件消息上已记录的哈希补齐缓存条目。
        # 用 COALESCE 兜底：没有对应消息时保持空串，避免写成 NULL
        # （模型字段是非空 CharField，NULL 会让后续读取拿到 None）
        await conn.execute_query(
            "UPDATE room_cached_file SET file_hash = COALESCE(("
            "  SELECT m.file_hash FROM room_message m"
            "  WHERE m.room_code = room_cached_file.room_code"
            "    AND m.msg_type = 'file'"
            "    AND m.content = room_cached_file.transfer_id"
            "    AND m.file_hash <> ''"
            "  ORDER BY m.id DESC LIMIT 1"
            "), '')"
            "WHERE file_hash = ''"
        )
        logger.info("Migration 030: room_cached_file 内容哈希回填完成")

        # 查询索引：按 (room_code, file_hash) 过滤
        if not await index_exists('idx_room_cached_file_hash'):
            await conn.execute_query(
                "CREATE INDEX IF NOT EXISTS idx_room_cached_file_hash"
                " ON room_cached_file (room_code, file_hash)"
            )
            logger.info("Migration 030: room_cached_file 建立 (room_code, file_hash) 索引")

        logger.info("Migration 030: 房间缓存文件内容哈希字段就绪")
    except Exception as e:
        logger.error(f"Migration 030 failed: {e}")
        raise
