"""跨数据库的类型差异适配。

项目原本只支持 SQLite，迁移里直接用了 SQLite 专有的
`pragma_table_info(...)` 与 `sqlite_master`。接入 Postgres / MySQL 后这些语句会
直接报错，导致启动时迁移失败。

本模块提供各库通用的等价查询，迁移应统一走这里，而不是直接写方言 SQL。
"""
from __future__ import annotations

import re

from tortoise import connections

from core.settings import settings


def db_type() -> str:
    """当前数据库类型：sqlite / postgres / mysql（未知按 sqlite 处理）。"""
    return str(getattr(settings, "dbType", "sqlite") or "sqlite").strip().lower()


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> str:
    """校验标识符，避免拼进 SQL 时产生注入。"""
    if not _IDENT_RE.match(name or ""):
        raise ValueError(f"非法的数据库标识符: {name!r}")
    return name


async def column_exists(table: str, column: str) -> bool:
    """判断表中是否存在指定列（跨库）。

    SQLite 的 pragma_table_info 是表值函数，**不支持参数绑定**，表名必须以字面量
    拼进 SQL（故先用 _safe_ident 校验），只有列名可以绑定。
    """
    conn = connections.get("default")
    kind = db_type()
    if kind == "postgres":
        sql = (
            "SELECT COUNT(*) AS c FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2"
        )
    elif kind == "mysql":
        sql = (
            "SELECT COUNT(*) AS c FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s"
        )
        res = await conn.execute_query(sql, [table, column])
        rows = res[1] if len(res) > 1 else res[0]
        return (rows[0]["c"] if rows else 0) > 0
    else:
        sql = f"SELECT COUNT(*) AS c FROM pragma_table_info('{_safe_ident(table)}') WHERE name = ?"

    res = await conn.execute_query(sql, [column])
    rows = res[1] if len(res) > 1 else res[0]
    return (rows[0]["c"] if rows else 0) > 0


async def table_exists(name: str) -> bool:
    """判断表是否存在（跨库）。"""
    conn = connections.get("default")
    kind = db_type()
    if kind == "postgres":
        sql = "SELECT COUNT(*) AS c FROM information_schema.tables WHERE table_name = $1"
    elif kind == "mysql":
        sql = (
            "SELECT COUNT(*) AS c FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s"
        )
        res = await conn.execute_query(sql, [name])
        rows = res[1] if len(res) > 1 else res[0]
        return (rows[0]["c"] if rows else 0) > 0
    else:
        sql = f"SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table' AND name = '{_safe_ident(name)}'"

    res = await conn.execute_query(sql)
    rows = res[1] if len(res) > 1 else res[0]
    return (rows[0]["c"] if rows else 0) > 0


async def index_exists(name: str) -> bool:
    """判断索引是否存在（跨库）。"""
    conn = connections.get("default")
    kind = db_type()
    if kind == "postgres":
        sql = "SELECT COUNT(*) AS c FROM pg_indexes WHERE indexname = $1"
    elif kind == "mysql":
        sql = (
            "SELECT COUNT(*) AS c FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND index_name = %s"
        )
        res = await conn.execute_query(sql, [name])
        rows = res[1] if len(res) > 1 else res[0]
        return (rows[0]["c"] if rows else 0) > 0
    else:
        sql = "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='index' AND name = ?"

    res = await conn.execute_query(sql, [name])
    rows = res[1] if len(res) > 1 else res[0]
    return (rows[0]["c"] if rows else 0) > 0
