"""部署形态能力判定：按数据库类型（以及其它进程内状态）决定支持什么。

背景：本项目曾有两类**进程内**状态，决定了能否多 worker 部署：
1. SQLite 是单写者，多进程并发写会冲突（busy_timeout 只能缓解，不能消除）
2. `ws_manager` 的房间连接表在进程内存中，多进程之间不共享——
   即使换掉数据库，房间内的 WebSocket 消息广播仍然只落在本进程

第 2 点现在可以解除：配置 `redisUrl` 后由 core.cluster 走 Redis pub/sub 同步
房间成员与文本信令、并给房间配额校验加分布式锁。

因此"能不能多 worker"= 数据库支持并发写 **且** 集群协调已启用。
本模块把判断集中起来，避免各处各写一遍假设。
"""
from __future__ import annotations

from core import cluster
from core.db_compat import db_type

# 支持多进程写入的数据库（SQLite 不在内：单写者）
MULTI_WRITER_DBS = ("postgres", "mysql")


def db_supports_multi_writer() -> bool:
    return db_type() in MULTI_WRITER_DBS


def cluster_distributed() -> bool:
    """集群协调是否已启用（配置了 redisUrl 且连接成功）。"""
    return bool(getattr(cluster.hub, "distributed", False))


def multi_worker_blockers() -> list[str]:
    """返回当前仍阻止多 worker 部署的原因；为空表示可以。"""
    blockers: list[str] = []
    kind = db_type()
    if not db_supports_multi_writer():
        blockers.append(
            f"数据库为 {kind}（单写者），多进程并发写会冲突；需改用 postgres 或 mysql"
        )
    if not cluster_distributed():
        blockers.append(
            "房间 WebSocket 连接表与房间配额锁仍未跨进程共享；"
            "需配置 redisUrl 启用集群协调（core.cluster）"
        )
    return blockers


def multi_worker_notes() -> list[str]:
    """不阻止多 worker、但运维必须知道的约束。"""
    notes = [
        "房间文件分片中继（二进制帧）不跨实例转发：接入层需按房间做会话亲和"
        "（同一房间的 WebSocket 连接路由到同一实例），否则中转发送方会收到"
        "「接收方不在本实例」提示，P2P 直连不受影响",
        "上传/取件的 IP 限流计数仍在进程内，多实例下实际阈值为配置值 × 实例数"
        "（属近似放宽，不影响正确性）",
    ]
    return notes


def supports_multi_worker() -> bool:
    return not multi_worker_blockers()


def capability_report() -> dict:
    kind = db_type()
    blockers = multi_worker_blockers()
    hub_state = cluster.hub.state()
    return {
        "db_type": kind,
        "db_supports_multi_writer": db_supports_multi_writer(),
        "cluster_mode": hub_state.get("mode"),
        "cluster_distributed": cluster_distributed(),
        "multi_worker_supported": supports_multi_worker(),
        "multi_worker_blockers": blockers,
        "multi_worker_notes": multi_worker_notes() if not blockers else [],
        "single_instance_required": not supports_multi_worker(),
    }
