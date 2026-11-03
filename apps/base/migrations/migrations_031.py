"""Migration 031：建立 audit_log 表与索引，管理后台关键操作可追溯。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.db_compat import db_type
from core.logger import logger


async def migrate():
    """迁移 031：新建审计日志表（audit_log）。

    合规与安全事件溯源需要记录"谁在何时对什么做了什么、结果如何"。
    按数据库类型生成建表语句：SQLite 用 AUTOINCREMENT、Postgres 用 SERIAL、
    MySQL 用 AUTO_INCREMENT；MySQL 不支持 CREATE INDEX IF NOT EXISTS，
    故索引创建逐个 try/except 兜底（已存在时忽略）。
    """
    conn = connections.get("default")
    kind = db_type()

    if kind == "postgres":
        id_ddl = "id SERIAL PRIMARY KEY"
    elif kind == "mysql":
        id_ddl = "id INT NOT NULL AUTO_INCREMENT PRIMARY KEY"
    else:
        id_ddl = "id INTEGER PRIMARY KEY AUTOINCREMENT"

    try:
        await conn.execute_script(f"""
            CREATE TABLE IF NOT EXISTS audit_log (
                {id_ddl},
                action VARCHAR(32) NOT NULL,
                target_type VARCHAR(32) NOT NULL DEFAULT '',
                target_id VARCHAR(64) NOT NULL DEFAULT '',
                result VARCHAR(16) NOT NULL DEFAULT 'success',
                detail VARCHAR(255) NOT NULL DEFAULT '',
                ip VARCHAR(64) NOT NULL DEFAULT '',
                actor VARCHAR(64) NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        indexes = (
            ("idx_audit_action", "audit_log (action)"),
            ("idx_audit_target", "audit_log (target_type, target_id)"),
            ("idx_audit_created", "audit_log (created_at)"),
        )
        for name, cols in indexes:
            try:
                if kind == "mysql":
                    await conn.execute_script(f"CREATE INDEX {name} ON {cols}")
                else:
                    await conn.execute_script(
                        f"CREATE INDEX IF NOT EXISTS {name} ON {cols}"
                    )
            except Exception as e:
                # 索引已存在 / 该库不支持 IF NOT EXISTS —— 都不影响功能
                logger.info(f"Migration 031: 索引 {name} 跳过（{type(e).__name__}）")

        logger.info("Migration 031: audit_log 表与索引就绪")
    except Exception as e:
        logger.error(f"Migration 031 failed: {e}")
        raise
