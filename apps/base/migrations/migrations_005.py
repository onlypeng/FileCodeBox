"""Migration 005：重建 filecodes 表以调整主键/字段约束；SQLite 无法直接改列，故用「建新表→搬迁→换名」。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections


def _need_upgrade(columns: list[tuple]) -> bool:
    for column in columns:
        # PRAGMA table_info 返回 (cid, name, type, notnull, dflt_value, pk)
        if column[1] == "size":
            column_type = (column[2] or "").upper()
            return "BIGINT" not in column_type
    return False


async def migrate():
    conn = connections.get("default")
    result = await conn.execute_query("PRAGMA table_info(filecodes)")
    columns = result[1] if result and len(result) > 1 else []

    if not columns or not _need_upgrade(columns):
        return

    await conn.execute_script(
        """
        BEGIN;
        CREATE TABLE IF NOT EXISTS filecodes_new
        (
            id             INTEGER                                not null
                primary key autoincrement,
            code           VARCHAR(255)                           not null
                unique,
            prefix         VARCHAR(255) default ''                not null,
            suffix         VARCHAR(255) default ''                not null,
            uuid_file_name VARCHAR(255),
            file_path      VARCHAR(255),
            size           BIGINT       default 0                 not null,
            text           TEXT,
            expired_at     TIMESTAMP,
            expired_count  INT          default 0                 not null,
            used_count     INT          default 0                 not null,
            created_at     TIMESTAMP    default CURRENT_TIMESTAMP not null,
            file_hash      VARCHAR(128),
            is_chunked     BOOL         default False             not null,
            upload_id      VARCHAR(128)
        );

        INSERT INTO filecodes_new (id, code, prefix, suffix, uuid_file_name, file_path, size, text,
                                   expired_at, expired_count, used_count, created_at, file_hash,
                                   is_chunked, upload_id)
        SELECT id,
               code,
               prefix,
               suffix,
               uuid_file_name,
               file_path,
               size,
               text,
               expired_at,
               expired_count,
               used_count,
               created_at,
               file_hash,
               is_chunked,
               upload_id
        FROM filecodes;

        DROP TABLE filecodes;
        ALTER TABLE filecodes_new
            RENAME TO filecodes;
        CREATE INDEX IF NOT EXISTS idx_filecodes_code_1c7ee7
            on filecodes (code);
        COMMIT;
        """
    )
