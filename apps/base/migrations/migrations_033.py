"""Migration 033: FileCodes 增加 access_code_hash（提取口令 / 二次口令）。

取件码通常在聊天、邮件、工单里明文流转，一旦泄露任何人都能取件。
二次口令走另一条信道（口头/电话），两者都具备才能取件，把"码泄露 = 文件泄露"
降级为"码泄露 ≠ 文件泄露"。

存 PBKDF2 哈希而非明文：库被拖走时口令不可逆推（与管理员口令同一套工具）。
空值表示该分享不需要口令，存量分享行为完全不变。
"""
from core.logger import logger
from core.db_compat import column_exists


async def migrate():
    from tortoise import connections

    conn = connections.get("default")

    if not await column_exists("filecodes", "access_code_hash"):
        await conn.execute_query(
            "ALTER TABLE filecodes ADD COLUMN access_code_hash VARCHAR(200)"
        )
        logger.info("Migration 033: 已添加 filecodes.access_code_hash 列")
    else:
        logger.info("Migration 033: filecodes.access_code_hash 列已存在，跳过")
