"""多实例部署（集群协调）测试。

用 fakeredis 的 SharedServer 模拟"两个 worker 连同一个 Redis"：两个 hub + 两个
ConnectionManager 各代表一个实例，各自持有自己的假 WebSocket 连接，验证
房间成员镜像、跨实例文本广播/定向投递、踢人、心跳自愈、分布式锁与能力判定。

真实 Redis 环境未在本机提供，但这些断言覆盖了协议层的实际行为（pub/sub 投递、
SET NX 抢锁、Lua 释放），不是只测"函数被调用过"。
"""
import asyncio
import json

import pytest
import pytest_asyncio

from core import capabilities, cluster
from core.websocket import ConnectionManager


class FakeWS:
    """最小可用的 WebSocket 替身：只关心服务端主动发出的三类动作。"""

    def __init__(self):
        self.text_frames = []
        self.binary_frames = []
        self.closed = None
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_text(self, data):
        self.text_frames.append(data)

    async def send_bytes(self, data):
        self.binary_frames.append(data)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def messages(self):
        """解析后的事件列表（忽略 ping 探活帧）。"""
        out = []
        for raw in self.text_frames:
            if raw == "ping":
                continue
            try:
                out.append(json.loads(raw))
            except ValueError:
                continue
        return out


async def wait_until(predicate, timeout=3.0, interval=0.02):
    """等待条件成立（pub/sub 投递是异步的，不能假设立即到达）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


@pytest_asyncio.fixture
async def two_instances():
    """两个实例共用同一个 fake Redis；用完正确关停（避免残留订阅任务）。"""
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    import fakeredis.aioredis as fake_aioredis

    hubs = []
    managers = []
    for _ in range(2):
        client = fake_aioredis.FakeRedis(server=server, decode_responses=True)
        hub = cluster.RedisHub("redis://fake:6379/0", client=client)
        await hub.start()
        manager = ConnectionManager(hub)
        await manager.start()
        hubs.append(hub)
        managers.append(manager)
    try:
        yield managers[0], managers[1]
    finally:
        for manager in managers:
            await manager.stop()
        for hub in hubs:
            await hub.close()


# ==================== 单实例（默认形态）行为不变 ====================


@pytest.mark.asyncio
async def test_local_mode_behaves_like_before():
    manager = ConnectionManager()  # 默认 LocalHub
    ws1, ws2 = FakeWS(), FakeWS()
    await manager.connect("ROOM01", ws1, nickname="甲")
    await manager.connect("ROOM01", ws2, nickname="乙")

    await manager.broadcast("ROOM01", {"type": "user_joined", "nickname": "乙"})
    assert [m["type"] for m in ws1.messages()] == ["user_joined"]
    assert [m["type"] for m in ws2.messages()] == ["user_joined"]

    # 定向投递：本地命中即为已投递
    assert await manager.send_to_client("ROOM01", "no-such-id", {"type": "x"}) is False
    cid = manager.get_members("ROOM01")[0]["client_id"]
    assert await manager.send_to_client("ROOM01", cid, {"type": "direct"}) is True

    assert manager.room_size("ROOM01") == 2
    assert manager.active_rooms() == {"ROOM01"}
    assert manager.is_member_online("ROOM01", cid) is True
    assert manager.is_member_online("ROOM01", "fake") is False


@pytest.mark.asyncio
async def test_local_mode_has_no_cluster_overhead():
    hub = cluster.LocalHub()
    await hub.start()
    # 单实例下 publish 是空操作、lock 是进程内锁
    await hub.publish({"t": "text", "room": "R", "msg": {}})
    assert hub.state()["distributed"] is False
    async with hub.lock("k"):
        pass


# ==================== 多实例 ====================


@pytest.mark.asyncio
async def test_remote_member_is_visible_across_instances(two_instances):
    m1, m2 = two_instances
    ws1 = FakeWS()
    cid = await m1.connect("ROOM02", ws1, nickname="甲")

    assert await wait_until(lambda: m2.is_member_online("ROOM02", cid))
    assert m2.room_size("ROOM02") == 1
    assert m2.get_online_users("ROOM02") == ["甲"]
    assert m2.get_members("ROOM02") == [{"client_id": cid, "nickname": "甲"}]
    assert "ROOM02" in m2.active_rooms()


@pytest.mark.asyncio
async def test_nickname_refresh_propagates(two_instances):
    m1, m2 = two_instances
    cid = await m1.connect("ROOM03", FakeWS(), nickname="")
    assert await wait_until(lambda: m2.is_member_online("ROOM03", cid))

    await m1.refresh_presence("ROOM03", cid, "真实昵称")
    assert await wait_until(lambda: m2.get_online_users("ROOM03") == ["真实昵称"])


@pytest.mark.asyncio
async def test_leave_removes_remote_member(two_instances):
    m1, m2 = two_instances
    ws1 = FakeWS()
    cid = await m1.connect("ROOM04", ws1, nickname="甲")
    assert await wait_until(lambda: m2.is_member_online("ROOM04", cid))

    await m1.disconnect("ROOM04", ws1)
    assert await wait_until(lambda: not m2.is_member_online("ROOM04", cid))
    assert m2.room_size("ROOM04") == 0


@pytest.mark.asyncio
async def test_broadcast_reaches_other_instance(two_instances):
    m1, m2 = two_instances
    ws1, ws2 = FakeWS(), FakeWS()
    await m1.connect("ROOM05", ws1, nickname="甲")
    await m2.connect("ROOM05", ws2, nickname="乙")
    ws1.text_frames.clear()
    assert await wait_until(lambda: m1.room_size("ROOM05") == 2)

    await m1.broadcast("ROOM05", {"type": "message", "text": "跨实例"})

    assert [m["type"] for m in ws1.messages()] == ["message"]
    assert await wait_until(lambda: [m["type"] for m in ws2.messages()] == ["message"])
    assert ws2.messages()[0]["text"] == "跨实例"


@pytest.mark.asyncio
async def test_targeted_send_crosses_instance(two_instances):
    m1, m2 = two_instances
    ws2 = FakeWS()
    cid2 = await m2.connect("ROOM06", ws2, nickname="乙")
    await m1.connect("ROOM06", FakeWS(), nickname="甲")
    assert await wait_until(lambda: m1.is_member_online("ROOM06", cid2))

    # 目标在别的实例：本地未投递（返回 False），但对方实例收到了
    delivered = await m1.send_to_client("ROOM06", cid2, {"type": "file_offer"})
    assert delivered is False
    assert await wait_until(lambda: [m["type"] for m in ws2.messages()] == ["file_offer"])


@pytest.mark.asyncio
async def test_broadcast_exclude_skips_sender_on_other_instance(two_instances):
    m1, m2 = two_instances
    ws1, ws2 = FakeWS(), FakeWS()
    await m1.connect("ROOM07", ws1, nickname="甲")
    await m2.connect("ROOM07", ws2, nickname="乙")
    ws1.text_frames.clear()
    ws2.text_frames.clear()
    assert await wait_until(lambda: m1.room_size("ROOM07") == 2)

    await m2.broadcast_exclude("ROOM07", ws2, {"type": "typing"})

    # 发送者本人（在 m2）不收到；另一实例上的成员收到
    assert ws2.messages() == []
    assert await wait_until(lambda: [m["type"] for m in ws1.messages()] == ["typing"])


@pytest.mark.asyncio
async def test_kick_room_closes_connections_on_every_instance(two_instances):
    m1, m2 = two_instances
    ws1, ws2 = FakeWS(), FakeWS()
    await m1.connect("ROOM08", ws1, nickname="甲")
    await m2.connect("ROOM08", ws2, nickname="乙")
    assert await wait_until(lambda: m1.room_size("ROOM08") == 2)

    await m1.kick_room("ROOM08", reason="房间已被删除")

    assert ws1.closed == (4001, "房间已被删除")
    assert await wait_until(lambda: ws2.closed is not None)
    assert ws2.closed[0] == 4001
    assert m1.room_size("ROOM08") == 0
    assert await wait_until(lambda: m2.room_size("ROOM08") == 0)


@pytest.mark.asyncio
async def test_heartbeat_snapshot_heals_lost_join_event(two_instances):
    """join 事件丢包（进程抖动/网络瞬断）后，快照心跳必须把成员补回来。"""
    m1, m2 = two_instances
    cid = await m1.connect("ROOM09", FakeWS(), nickname="甲")
    # 手动抹掉 m2 的远端记录，模拟"这条 join 没收到"
    m2._remote.clear()
    assert not m2.is_member_online("ROOM09", cid)

    await m1._publish_snapshot()
    assert await wait_until(lambda: m2.is_member_online("ROOM09", cid))
    assert m2.get_online_users("ROOM09") == ["甲"]


@pytest.mark.asyncio
async def test_dead_instance_members_are_pruned():
    """实例心跳超时 → 其成员被摘掉（避免在线人数虚高）。"""
    manager = ConnectionManager(cluster.LocalHub())
    manager._remote["ROOM10"] = {"cid-1": {"nickname": "幽灵", "inst": "dead-instance"}}
    manager._inst_seen["dead-instance"] = 0.0  # 远古时刻，必然超时

    manager._prune_dead_instances()

    assert manager.room_size("ROOM10") == 0
    assert "dead-instance" not in manager._inst_seen


@pytest.mark.asyncio
async def test_graceful_bye_drops_members_immediately(two_instances):
    m1, m2 = two_instances
    cid = await m1.connect("ROOM11", FakeWS(), nickname="甲")
    assert await wait_until(lambda: m2.is_member_online("ROOM11", cid))

    await m1.hub.close()  # 优雅下线：发 bye，另一个实例立刻摘掉本实例成员

    assert await wait_until(lambda: not m2.is_member_online("ROOM11", cid))


# ==================== 分布式锁 ====================


@pytest.mark.asyncio
async def test_lock_serializes_across_instances(two_instances):
    m1, m2 = two_instances
    events = []

    async def worker(hub, tag):
        async with hub.lock("room-cache:ROOM12", ttl_ms=5000, wait_ms=5000):
            events.append(f"{tag}-enter")
            await asyncio.sleep(0.12)
            events.append(f"{tag}-exit")

    await asyncio.gather(worker(m1.hub, "A"), worker(m2.hub, "B"))

    # 交叉进入说明锁没生效；必须是 A 进出后才轮到 B（顺序不限）
    assert events in (
        ["A-enter", "A-exit", "B-enter", "B-exit"],
        ["B-enter", "B-exit", "A-enter", "A-exit"],
    )


@pytest.mark.asyncio
async def test_lock_is_released_after_exception(two_instances):
    m1, m2 = two_instances
    with pytest.raises(RuntimeError):
        async with m1.hub.lock("room-cache:ROOM13", ttl_ms=5000, wait_ms=2000):
            raise RuntimeError("boom")

    # 异常后锁必须释放，否则该房间的缓存上传会永久卡住
    acquired = []

    async def second():
        async with m2.hub.lock("room-cache:ROOM13", ttl_ms=5000, wait_ms=2000):
            acquired.append(True)

    await asyncio.wait_for(second(), timeout=3)
    assert acquired == [True]


# ==================== 能力判定 ====================


@pytest.mark.asyncio
async def test_capability_requires_db_and_cluster(monkeypatch, two_instances):
    m1, _ = two_instances
    monkeypatch.setattr(cluster, "hub", m1.hub)
    monkeypatch.setattr(capabilities, "db_type", lambda: "sqlite")
    assert capabilities.supports_multi_worker() is False

    monkeypatch.setattr(capabilities, "db_type", lambda: "postgres")
    assert capabilities.supports_multi_worker() is True
    report = capabilities.capability_report()
    assert report["cluster_mode"] == "redis"
    assert report["cluster_distributed"] is True
    assert report["multi_worker_supported"] is True
    # 允许启用即附带"必须知道"的约束（会话亲和 / 限流按实例计）
    assert any("会话亲和" in note for note in report["multi_worker_notes"])


@pytest.mark.asyncio
async def test_capability_blocks_when_cluster_missing(monkeypatch):
    monkeypatch.setattr(cluster, "hub", cluster.LocalHub())
    monkeypatch.setattr(capabilities, "db_type", lambda: "postgres")
    assert capabilities.supports_multi_worker() is False
    blockers = capabilities.multi_worker_blockers()
    assert any("redisUrl" in b for b in blockers)
