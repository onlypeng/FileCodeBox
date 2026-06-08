from tortoise import connections
from core.logger import logger


async def add_is_multi_file_column():
    """为 FileCodes 添加 is_multi_file 字段"""
    conn = connections.get("default")
    # 检查字段是否已存在
    result = await conn.execute_query("PRAGMA table_info(filecodes)")
    columns = result[1] if result and len(result) > 1 else []
    column_names = [col[1] for col in columns]

    if "is_multi_file" not in column_names:
        await conn.execute_script(
            "ALTER TABLE filecodes ADD COLUMN is_multi_file BOOL default False NOT NULL;"
        )
        logger.info("Added is_multi_file column to filecodes")


async def create_fileitem_table():
    """创建 fileitem 表"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS fileitem (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            file_code_id   INTEGER NOT NULL REFERENCES filecodes(id) ON DELETE CASCADE,
            file_name      VARCHAR(255) NOT NULL,
            file_size      BIGINT DEFAULT 0 NOT NULL,
            file_hash      VARCHAR(64),
            storage_path   VARCHAR(512) NOT NULL,
            uuid_file_name VARCHAR(255),
            sort_order     INT DEFAULT 0 NOT NULL,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    logger.info("Created fileitem table")


async def create_collection_table():
    """创建 collection 表"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS collection (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_code VARCHAR(6) NOT NULL UNIQUE,
            title           VARCHAR(255) DEFAULT '' NOT NULL,
            description     TEXT DEFAULT '',
            password        VARCHAR(255) DEFAULT '',
            max_files       INT DEFAULT 20 NOT NULL,
            expired_at      TIMESTAMP,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    await conn.execute_script(
        "CREATE INDEX IF NOT EXISTS idx_collection_code ON collection(collection_code)"
    )
    logger.info("Created collection table")


async def create_collectionfile_table():
    """创建 collectionfile 表"""
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS collectionfile (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id  INTEGER NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
            file_name      VARCHAR(255) NOT NULL,
            file_size      BIGINT DEFAULT 0 NOT NULL,
            file_hash      VARCHAR(64),
            storage_path   VARCHAR(512) NOT NULL,
            uuid_file_name VARCHAR(255),
            uploader_name  VARCHAR(100) DEFAULT '',
            uploader_ip    VARCHAR(45) DEFAULT '' NOT NULL,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """
    )
    logger.info("Created collectionfile table")


async def migrate_old_data():
    """数据迁移：为旧的单文件 FileCodes 记录创建对应的 FileItem"""
    conn = connections.get("default")

    # 先确保 fileitem 表存在
    result = await conn.execute_query("PRAGMA table_info(fileitem)")
    if not result or not result[1]:
        logger.warning("fileitem table does not exist, skipping data migration")
        return

    # 检查字段是否存在
    columns_result = await conn.execute_query("PRAGMA table_info(filecodes)")
    columns = columns_result[1] if columns_result and len(columns_result) > 1 else []
    column_names = [col[1] for col in columns]

    if "is_multi_file" not in column_names:
        logger.warning("is_multi_file column does not exist, skipping data migration")
        return

    # 执行数据迁移
    await conn.execute_script(
        """
        INSERT INTO fileitem (file_code_id, file_name, file_size, file_hash, storage_path, uuid_file_name, sort_order)
        SELECT id,
               CASE WHEN prefix = 'Text' THEN '' ELSE prefix || suffix END,
               size,
               file_hash,
               COALESCE(file_path, ''),
               uuid_file_name,
               0
        FROM filecodes
        WHERE is_multi_file = 0
          AND text IS NULL
          AND uuid_file_name IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM fileitem WHERE fileitem.file_code_id = filecodes.id
          )
        """
    )
    logger.info("Migrated old file codes to fileitem")


async def migrate():
    """迁移入口"""
    try:
        await add_is_multi_file_column()
        await create_fileitem_table()
        await create_collection_table()
        await create_collectionfile_table()
        await migrate_old_data()
        logger.info("Migration 006 completed successfully")
    except Exception as e:
        logger.error(f"Migration 006 failed: {e}")
        raise
