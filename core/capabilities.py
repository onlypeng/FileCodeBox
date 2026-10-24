"""部署形态能力判定：按数据库类型（以及其它进程内状态）决定支持什么。

背景：本项目有两类**进程内**状态，决定了能否多 worker 部署：
1. SQLite 是单写者，多进程并发写会冲突（busy_timeout 只能缓解，不能消除）
2. `ws_manager` 的房间连接表在进程内存中，多进程之间不共享——
   即使换掉数据库，房间内的 WebSocket 消息广播仍然只落在本进程

因此"能不能多 worker"不是只由数据库类型决定，必须两项都满足。
本模块把判断集中起来，避免各处各写一遍假设。
"""
from __future__ import annotations

from core.db_compat import db_type

# 支持多进程写入的数据库（SQLite 不在内：单写者）
MULTI_WRITER_DBS = ("postgres", "mysql")


def db_supports_multi_writer() -> bool:
    return db_type() in MULTI_WRITER_DBS


def multi_worker_blockers() -> list[str]:
    """返回当前仍阻止多 worker 部署的原因；为空表示可以。"""
    blockers: list[str] = []
    kind = db_type()
    if not db_supports_multi_writer():
        blockers.append(
            f"数据库为 {kind}（单写者），多进程并发写会冲突；需改用 postgres 或 mysql"
        )
    # ws_manager / 房间锁都是进程内状态：即使换成 Postgres/MySQL 也仍未解决
    blockers.append(
        "房间 WebSocket 连接表与房间配额锁均为进程内状态，多进程之间不共享；"
        "需将广播改为 Redis pub/sub（或等价方案）、配额锁改为分布式锁"
    )
    return blockers


def supports_multi_worker() -> bool:
    return not multi_worker_blockers()


def capability_report() -> dict:
    kind = db_type()
    blockers = multi_worker_blockers()
    return {
        "db_type": kind,
        "db_supports_multi_writer": db_supports_multi_writer(),
        "multi_worker_supported": supports_multi_worker(),
        "multi_worker_blockers": blockers,
        "single_instance_required": not supports_multi_worker(),
    }
