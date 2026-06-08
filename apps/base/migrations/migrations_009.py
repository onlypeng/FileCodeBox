from tortoise import connections
from core.logger import logger


async def add_retrieve_code_column():
    """为 collectionbox 表添加 retrieve_code 字段"""
    conn = connections.get("default")

    try:
        await conn.execute_script(
            "ALTER TABLE collectionbox ADD COLUMN retrieve_code VARCHAR(6) DEFAULT '' NOT NULL"
        )
    except Exception:
        logger.info("Column retrieve_code already exists, skipping")

    logger.info("Added retrieve_code column to collectionbox")


async def generate_existing_retrieve_codes():
    """为已有的收件箱生成 retrieve_code"""
    import secrets
    import string

    conn = connections.get("default")
    chars = string.ascii_uppercase + string.digits

    # 查找所有没有 retrieve_code 的记录
    rows = await conn.execute_query_dict(
        "SELECT id FROM collectionbox WHERE retrieve_code = '' OR retrieve_code IS NULL"
    )

    for row in rows:
        box_id = row["id"]
        # 生成唯一码
        for _ in range(20):
            code = "".join(secrets.choice(chars) for _ in range(6))
            existing = await conn.execute_query_dict(
                "SELECT id FROM collectionbox WHERE retrieve_code = ? AND id != ?",
                [code, box_id],
            )
            if not existing:
                await conn.execute_query_dict(
                    "UPDATE collectionbox SET retrieve_code = ? WHERE id = ?",
                    [code, box_id],
                )
                break

    # 创建唯一索引
    try:
        await conn.execute_script(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_collectionbox_retrieve_code ON collectionbox (retrieve_code)"
        )
    except Exception:
        logger.info("retrieve_code unique index already exists, skipping")

    logger.info(f"Generated retrieve_codes for {len(rows)} existing collection boxes")


async def migrate():
    """迁移入口"""
    try:
        await add_retrieve_code_column()
        await generate_existing_retrieve_codes()
        logger.info("Migration 009 completed successfully")
    except Exception as e:
        logger.error(f"Migration 009 failed: {e}")
        raise
