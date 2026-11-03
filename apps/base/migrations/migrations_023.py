"""Migration 023：把 count 型（按次数过期）文件的 expired_at 置空，避免两种过期机制互相打架。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.logger import logger


async def migrate():
    """迁移 023：count（按次）型文件的 expired_at 置 NULL。

    按次分享语义为"次数用尽前永久有效"，无时间上限：
    - is_expired 对次数型（expired_count >= 0）仅按剩余次数判断，不读取 expired_at
    - 清理任务对次数型仅按次数用尽判断，不读取 expired_at
    此前 count 型误设了 1 天/365 天的占位过期时间（仅影响管理面板显示），统一置空。
    """
    conn = connections.get("default")
    try:
        result = await conn.execute_query(
            "UPDATE filecodes SET expired_at = NULL "
            "WHERE expire_style = 'count' AND expired_count >= 0 AND expired_at IS NOT NULL"
        )
        updated = result[0] if result else 0
        logger.info(f"Migration 023: 已将 {updated} 条 count 型文件 expired_at 置空")
    except Exception as e:
        logger.error(f"Migration 023 failed: {e}")
        raise
