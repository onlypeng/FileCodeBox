from tortoise import connections
from core.logger import logger


async def migrate():
    """迁移 022：collectionfile 表增加分片上传会话字段。

    投递（delivery）分片上传：投递记录记录上传会话信息（upload_id/file_name/
    file_size/chunk_size/total_chunks），complete 合并后回填 file_code。
    存量记录保持 NULL，不影响现有直传行为。
    """
    conn = connections.get("default")
    try:
        existing = {
            row["name"]
            for row in await conn.execute_query_dict("PRAGMA table_info(collectionfile)")
        }
        additions = [
            ("upload_id", "VARCHAR(36)"),
            ("file_name", "VARCHAR(255)"),
            ("file_size", "BIGINT"),
            ("chunk_size", "INT"),
            ("total_chunks", "INT"),
        ]
        for col, col_type in additions:
            if col not in existing:
                await conn.execute_query(
                    f"ALTER TABLE collectionfile ADD COLUMN {col} {col_type} NULL"
                )
                logger.info(f"Migration 022: collectionfile 增加列 {col}")
        await conn.execute_query(
            "CREATE INDEX IF NOT EXISTS idx_collectionfile_upload_id "
            "ON collectionfile (upload_id)"
        )
        logger.info("Migration 022: 完成（collectionfile 分片上传字段）")
    except Exception as e:
        logger.error(f"Migration 022 failed: {e}")
        raise
