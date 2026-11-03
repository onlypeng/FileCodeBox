"""Migration 003：建立 presignuploadsession 表，支持 S3 直传的上传会话。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections


async def create_presign_upload_session_table():
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS presignuploadsession (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            upload_id VARCHAR(36) NOT NULL UNIQUE,
            file_name VARCHAR(255) NOT NULL,
            file_size BIGINT NOT NULL,
            save_path VARCHAR(512) NOT NULL,
            mode VARCHAR(10) NOT NULL,
            expire_value INT NOT NULL DEFAULT 1,
            expire_style VARCHAR(20) NOT NULL DEFAULT 'day',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_presignuploadsession_upload_id ON presignuploadsession (upload_id);
        """
    )


async def migrate():
    await create_presign_upload_session_table()
