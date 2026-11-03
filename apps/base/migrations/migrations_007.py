"""Migration 007：下线旧的收件箱表结构，为新的 collection/delivery 模型让路。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.logger import logger


async def drop_old_tables():
    """删除旧的 collection 和 collectionfile 表"""
    conn = connections.get("default")
    await conn.execute_script("DROP TABLE IF EXISTS collectionfile")
    await conn.execute_script("DROP TABLE IF EXISTS collection")
    logger.info("Dropped old collection tables")


async def create_collectionbox_table():
    """创建 collectionbox 表"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS collectionbox (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_code VARCHAR(6) NOT NULL UNIQUE,
            delivery_code   VARCHAR(6) NOT NULL UNIQUE,
            title           VARCHAR(100) DEFAULT '' NOT NULL,
            description     TEXT DEFAULT '',
            creator_ip      VARCHAR(45) DEFAULT '' NOT NULL,
            creator_name    VARCHAR(50) DEFAULT '匿名' NOT NULL,
            password        VARCHAR(255) DEFAULT '',
            max_files       INT DEFAULT 20 NOT NULL,
            expired_at      TIMESTAMP,
            expire_count    INT DEFAULT -1 NOT NULL,
            is_active       BOOL DEFAULT 1 NOT NULL,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    await conn.execute_script(
        "CREATE INDEX IF NOT EXISTS idx_collectionbox_code ON collectionbox(collection_code)"
    )
    await conn.execute_script(
        "CREATE INDEX IF NOT EXISTS idx_collectionbox_delivery ON collectionbox(delivery_code)"
    )
    logger.info("Created collectionbox table")


async def create_new_collectionfile_table():
    """创建新的 collectionfile 表（关联 FileCode）"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS collectionfile (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id  INTEGER NOT NULL REFERENCES collectionbox(id) ON DELETE CASCADE,
            file_code_id   INTEGER REFERENCES filecodes(id) ON DELETE SET NULL,
            uploader_name  VARCHAR(50) DEFAULT '',
            uploader_ip    VARCHAR(45) DEFAULT '' NOT NULL,
            status         VARCHAR(20) DEFAULT 'uploading' NOT NULL,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    logger.info("Created new collectionfile table")


async def migrate():
    """迁移入口"""
    try:
        await drop_old_tables()
        await create_collectionbox_table()
        await create_new_collectionfile_table()
        logger.info("Migration 007 completed successfully")
    except Exception as e:
        logger.error(f"Migration 007 failed: {e}")
        raise
