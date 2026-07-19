from tortoise import connections
from core.logger import logger


async def migrate():
    """将 collectionbox 表的 collection_code/delivery_code/retrieve_code 列从 VARCHAR(6) 扩展为 VARCHAR(16)"""
    conn = connections.get("default")

    try:
        result = await conn.execute_query("PRAGMA table_info(collectionbox)")
        columns = result[1] if result and len(result) > 1 else []

        if not columns:
            logger.info("collectionbox table does not exist, skipping migration 013")
            return

        needs_migration = False
        for col in columns:
            if col[1] in ['collection_code', 'delivery_code', 'retrieve_code']:
                if 'VARCHAR(6)' in col[2]:
                    needs_migration = True
                    break

        if not needs_migration:
            logger.info("collectionbox columns already at VARCHAR(16), skipping migration 013")
            return

        await conn.execute_script("""
            CREATE TABLE IF NOT EXISTS collectionbox_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_code VARCHAR(16) NOT NULL UNIQUE,
                delivery_code VARCHAR(16) NOT NULL UNIQUE,
                retrieve_code VARCHAR(16) NOT NULL UNIQUE DEFAULT '',
                title VARCHAR(100) NOT NULL DEFAULT '',
                description TEXT DEFAULT '' NULL,
                creator_ip VARCHAR(45) NOT NULL DEFAULT '',
                creator_name VARCHAR(50) NOT NULL DEFAULT '匿名',
                password VARCHAR(255) NOT NULL DEFAULT '' NULL,
                max_files INTEGER NOT NULL DEFAULT 20,
                expire_style VARCHAR(20) NOT NULL DEFAULT 'day',
                expire_value INTEGER NOT NULL DEFAULT 7,
                expired_at TIMESTAMP NULL,
                delivery_expire_style VARCHAR(20) NOT NULL DEFAULT 'day',
                delivery_expire_value INTEGER NOT NULL DEFAULT 7,
                delivery_expired_at TIMESTAMP NULL,
                retrieve_expire_style VARCHAR(20) NOT NULL DEFAULT 'day',
                retrieve_expire_value INTEGER NOT NULL DEFAULT 7,
                retrieve_expired_at TIMESTAMP NULL,
                delivery_count INTEGER NOT NULL DEFAULT -1,
                delivery_used_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute_script("""
            INSERT INTO collectionbox_new
            SELECT id, collection_code, delivery_code, retrieve_code,
                   title, description, creator_ip, creator_name, password,
                   max_files, expire_style, expire_value, expired_at,
                   delivery_expire_style, delivery_expire_value, delivery_expired_at,
                   retrieve_expire_style, retrieve_expire_value, retrieve_expired_at,
                   delivery_count, delivery_used_count, created_at, updated_at
            FROM collectionbox
        """)

        await conn.execute_script("DROP TABLE collectionbox")
        await conn.execute_script("ALTER TABLE collectionbox_new RENAME TO collectionbox")

        await conn.execute_script("CREATE UNIQUE INDEX IF NOT EXISTS idx_collectionbox_collection_code ON collectionbox (collection_code)")
        await conn.execute_script("CREATE UNIQUE INDEX IF NOT EXISTS idx_collectionbox_delivery_code ON collectionbox (delivery_code)")
        await conn.execute_script("CREATE INDEX IF NOT EXISTS idx_collectionbox_retrieve_code ON collectionbox (retrieve_code)")

        logger.info("Migration 013 completed successfully")
    except Exception as e:
        logger.error(f"Migration 013 failed: {e}")
        raise
