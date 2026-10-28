"""Migration 032: FileCodes 增加 owner_token（发起者凭证）。

无账号体系下，"我发起的分享"与"提前作废"都需要一个只属于发起者的凭证：
仅凭取件码任何人都能取件，但撤销必须限于发起者本人。
上传成功时随取件码一并返回，前端存入发件记录。
"""
from core.logger import logger
from core.db_compat import column_exists


async def migrate():
    from tortoise import connections

    conn = connections.get("default")

    if not await column_exists("filecodes", "owner_token"):
        await conn.execute_query("ALTER TABLE filecodes ADD COLUMN owner_token VARCHAR(64)")
        logger.info("Migration 032: 已添加 filecodes.owner_token 列")
    else:
        logger.info("Migration 032: filecodes.owner_token 列已存在，跳过")

