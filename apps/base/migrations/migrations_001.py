"""Migration 001：初始化核心表 filecodes（分享记录）与 keyvalue（配置与运行时状态）。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections


async def create_file_codes_table():
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS filecodes
        (
            id             INTEGER                                not null
                primary key autoincrement,
            code           VARCHAR(255)                           not null
                unique,
            prefix         VARCHAR(255) default ''                not null,
            suffix         VARCHAR(255) default ''                not null,
            uuid_file_name VARCHAR(255),
            file_path      VARCHAR(255),
            size           INT          default 0                 not null,
            text           TEXT,
            expired_at     TIMESTAMP,
            expired_count  INT          default 0                 not null,
            used_count     INT          default 0                 not null,
            created_at     TIMESTAMP    default CURRENT_TIMESTAMP not null
        );
        CREATE INDEX IF NOT EXISTS idx_filecodes_code_1c7ee7
            on filecodes (code);
    """
    )


async def create_key_value_table():
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS keyvalue
        (
            id         INTEGER                             not null
                primary key autoincrement,
            key        VARCHAR(255)                        not null
                unique,
            value      JSON,
            created_at TIMESTAMP default CURRENT_TIMESTAMP not null
        );
        CREATE INDEX IF NOT EXISTS idx_keyvalue_key_eab890
            on keyvalue (key);
    """
    )


async def migrate():
    await create_file_codes_table()
    await create_key_value_table()
