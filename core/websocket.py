"""房间 WebSocket 连接管理：成员、在线判定、文本广播、文件中继路由。

## 问题

直连快传需要四类能力，全部依赖"谁在这个房间里"这份状态：

1. 成员上下线与人数统计（前端要显示"房间里 3 个人"）
2. 文本/Chat/打字状态/房间事件的广播
3. WebRTC 信令与文件的**定向**投递（按 client_id 路由，不是广播）
4. 房间缓存的 HTTP 接口鉴权（"你是本房间成员"要有凭据）

最早这些状态散落在路由函数里的一个全局 dict 上，于是出现两个真问题：
- client_id 由客户端自报 → 任何人都能伪造成别人去接收定向文件
- 状态在进程内存里 → 多 worker 时消息只有一半人收到（且极难排查）

## 方案

- **client_id 服务端签发**：首帧 join 时 uuid4 下发，客户端不得自报；断线重连
  想复用身份，需在 join 时带上次的 id，由服务端确认房间内无冲突才允许沿用。
  由于路由一律按 client_id 转发，伪造他人身份变得不可行。
- **本地连接与远端镜像分表**：`active_connections` 只放**本进程真实连接**，
  其它实例的成员镜像放在 `_remote`，对外一律通过方法合并返回。
  合二为一是诱人的简化，但远端成员没有可用的 ws，一旦被 send/close 循环读到
  就是 None 崩溃，或是被 disconnect 误删。
- **集群事件收敛为** join / leave / hb / bye / text / kick 六类，语义尽量简单：
  前四类维护成员表（hb 是覆盖式全量快照，用来自愈丢包的增量事件），
  text 投递文本信令，kick 关闭连接。传输由 core.cluster 负责；未配 Redis 时
  全部本地 no-op，行为与单实例逐字节等价。

## 刻意不做

- **二进制文件中继不跨实例转发**。单帧上限 256KB，走 pub/sub 要把吞吐放大 1.4
  倍（base64）且延迟不可控；正确做法是接入层按房间做会话亲和。发送方会拿到
  明确的"接收方不在本实例"提示，而不是"传到一半没了"。
- **不接受"客户端自报 client_id"的降级模式**。
  `is_member_online()` 之所以能作为房间 HTTP 接口的鉴权凭据，前提是"这个 id
  是服务端签发的、且此刻正连着本房间"。放开自报等于同时拆掉中鉴定向投递与
  接口鉴权两件事的安全性，收益却只是省掉一次 join 握手。
"""
import asyncio
import json
import time
import uuid
from typing import Dict, List, Optional

from fastapi import WebSocket

from core import cluster
from core.logger import logger

# 成员快照心跳间隔与实例失效阈值。
# 心跳存在的原因：增量事件（join/leave）在进程崩溃、消息丢失时会漂移，
# 周期性全量快照做"覆盖写"即可自愈；沉默超过 3 个周期的实例视为死亡并摘掉其成员。
HEARTBEAT_INTERVAL = 5.0
INSTANCE_TIMEOUT = 15.0


class ConnectionManager:
    """WebSocket 连接管理器，按房间 code 分组

    client_id 由服务端签发（首帧 join 时生成并回传给客户端），客户端不得自报，
    防止伪造他人 client_id 接收/劫持定向文件传输（中继目标按 client_id 路由）。
    持久化场景（断线重连复用身份）由客户端在 join 时携带上次签发的 id，
    服务端校验其归属（该 id 无其他在线连接时允许沿用）。

    ## 多实例

    本地连接表 `active_connections` 只放**本进程**的真实连接；
    其它实例的成员镜像在 `_remote`，对外一律通过方法合并返回
    （`room_size` / `get_online_users` / `get_members` / `is_member_online`）。

    刻意不把远端成员塞进 `active_connections`：那里没有可用的 ws，
    一旦被 send/close 循环读到就是 None 崩溃，或者被 disconnect 误删。
    两类状态分开存，读时合并 —— 用一点样板代码换掉一整类隐患。
    """

    def __init__(self, hub: Optional["cluster.LocalHub"] = None):
        # room_code -> list[{ws: WebSocket, nickname: str, client_id: str}]
        self.active_connections: Dict[str, List[dict]] = {}
        # 文件/媒体定向中继目标：room_code -> {sender_client_id: set(target_client_id)}
        # 单目标（文件传输）用 set_file_target 覆盖；多目标（媒体广播）用 add_file_target 追加
        self.file_targets: Dict[str, Dict[str, set]] = {}
        # 远端成员镜像：room_code -> client_id -> {nickname, inst}
        self._remote: Dict[str, Dict[str, dict]] = {}
        # 各实例最近一次心跳时刻（monotonic）
        self._inst_seen: Dict[str, float] = {}
        self.hub = hub or cluster.LocalHub()
        self.hub.on_event(self._on_cluster_event)
        self._heartbeat_task: Optional[asyncio.Task] = None

    # ==================== 生命周期 ====================

    def use_hub(self, hub: "cluster.LocalHub") -> None:
        """运行期切换协调件。

        全局 `ws_manager` 在模块导入时就已经构造（hub 是默认的 LocalHub），
        而实际实现要等配置加载完才知道，故由 main.lifespan 在此处替换。
        必须在接收任何连接之前调用，否则已建立的房间要等客户端重连才会进入新集群。
        """
        self.hub = hub
        hub.on_event(self._on_cluster_event)

    async def start(self) -> None:
        """启动集群协调（多实例时才开始心跳）。"""
        if getattr(self.hub, "distributed", False):
            # 订阅由 hub 自己维护；这里只负责周期性发布本实例成员快照
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """停掉心跳；幂等，可重复调用。

        刻意不在 asyncio.CancelledError 时重启 task：stop 意味着进程在关，
        继续发布快照只会让别的实例多看一轮无用的成员表。
        """
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None

    # ==================== 连接管理 ====================

    async def connect(self, code: str, websocket: WebSocket, nickname: str = "", client_id: str = ""):
        """接受连接并加入房间，返回最终生效的 client_id。

        入参 client_id 是**申请**而非断言：房间内已有在线连接占用时会被忽略，
        重新签发。昵称此时通常还未知（前端在 welcome 之后才上报），
        后续的 `refresh_presence` 负责把它同步出去。
        """
        await websocket.accept()
        if not client_id or not self._is_client_id_available(code, client_id):
            client_id = self._issue_client_id()
        if code not in self.active_connections:
            self.active_connections[code] = []
        self.active_connections[code].append({
            "ws": websocket,
            "nickname": nickname,
            "client_id": client_id,
        })
        logger.info(f"WebSocket 连接加入房间 {code}，当前连接数: {len(self.active_connections[code])}")
        await self._sync_join(code, client_id, nickname)
        return client_id

    async def _sync_join(self, code: str, client_id: str, nickname: str) -> None:
        if not getattr(self.hub, "distributed", False) or not client_id:
            return
        await self.hub.publish({
            "t": "join", "room": code, "client_id": client_id, "nickname": nickname or "",
        })

    async def refresh_presence(self, code: str, client_id: str, nickname: str) -> None:
        """昵称在首帧 join 后才确定，此时需要把昵称同步给其它实例。

        否则多实例下远端成员列表里会出现"匿名"条目，成员选择器与人数统计都会错。
        """
        if not client_id:
            return
        for conn in self.active_connections.get(code, []):
            if conn.get("client_id") == client_id:
                conn["nickname"] = nickname
                break
        await self._sync_join(code, client_id, nickname)

    async def drop_presence(self, code: str, client_id: str) -> None:
        """摘掉一个"已经不在本地、但其它实例可能还记着"的成员。

        场景：连接建立时服务端先签发临时 id 并广播了 join，客户端随后沿用上次的
        持久 id 完成重绑 —— 临时 id 必须显式撤销，否则远端会一直多出一个人。
        """
        if not client_id:
            return
        for conn in list(self.active_connections.get(code, [])):
            if conn.get("client_id") == client_id:
                try:
                    self.active_connections[code].remove(conn)
                except ValueError:
                    pass
        if getattr(self.hub, "distributed", False):
            await self.hub.publish({"t": "leave", "room": code, "client_id": client_id})

    def _is_client_id_available(self, code: str, client_id: str) -> bool:
        """client_id 在房间内未被其他在线连接占用（可用则允许沿用，否则重新签发）"""
        if not client_id:
            return False
        if any(c.get("client_id") == client_id for c in self.active_connections.get(code, [])):
            return False
        # 远端实例上同 id 在线也算被占用，否则同一身份会在两个实例各存一份
        return client_id not in self._remote.get(code, {})

    def _issue_client_id(self) -> str:
        return uuid.uuid4().hex[:16]

    async def disconnect(self, code: str, websocket: WebSocket):
        """移除连接并回收其路由状态，返回该连接的昵称（可能为空字符串）。

        副作用不止于本房间：`file_targets` 是跨房间按 client_id 索引的，
        这里会把该 client_id 作为"发送方"和"接收方"的角色一并清掉。
        刻意不加 `code` 过滤 —— client_id 是全局唯一的，若只清本房间，
        跨房间重连时会留下指向已失效连接的陈旧目标，表现为"文件静默发不出去"。
        """
        nickname = ""
        client_id = ""
        if code in self.active_connections:
            for i, conn in enumerate(self.active_connections[code]):
                if conn["ws"] is websocket:
                    nickname = conn.get("nickname", "")
                    client_id = conn.get("client_id", "")
                    self.active_connections[code].pop(i)
                    break
            if not self.active_connections[code]:
                del self.active_connections[code]
        # 清理该连接的定向中继目标（作为发送方 与 作为接收方）
        if client_id:
            for code_map in self.file_targets.values():
                # 该连接作为发送方：整条移除
                code_map.pop(client_id, None)
                # 该连接作为接收方：从各发送方的目标集合中移除
                for targets in code_map.values():
                    targets.discard(client_id)
            if getattr(self.hub, "distributed", False):
                await self.hub.publish({"t": "leave", "room": code, "client_id": client_id})
        logger.info(f"WebSocket 连接离开房间 {code}")
        return nickname

    async def replace_client(self, code: str, client_id: str, keep_ws: WebSocket) -> None:
        """顶替同 client_id 的旧连接（断线残留/多标签页/其它实例上的同名连接）。

        多实例下旧连接可能在别的进程：本地能关的直接关，跨实例的发 kick 事件，
        由持有该连接的实例关闭 —— 否则同一身份会在两个实例重复在线。
        """
        for old in list(self.active_connections.get(code, [])):
            if old is None or old.get("ws") is keep_ws:
                continue
            if old.get("client_id") != client_id:
                continue
            try:
                await old["ws"].close(code=1000, reason="replaced")
            except Exception:
                pass
            try:
                self.active_connections[code].remove(old)
            except ValueError:
                pass
        if getattr(self.hub, "distributed", False):
            await self.hub.publish({
                "t": "kick", "room": code, "client_id": client_id, "reason": "replaced",
            })

    # ==================== 在线状态查询（本地 + 远端合并） ====================

    def room_size(self, code: str) -> int:
        """房间内总在线人数（含其它实例的成员）。"""
        return len(self.active_connections.get(code, [])) + len(self._remote.get(code, {}))

    def active_rooms(self) -> set:
        """有在线成员的房间码集合（含远端）。"""
        rooms = {code for code, conns in self.active_connections.items() if conns}
        rooms |= {code for code, members in self._remote.items() if members}
        return rooms

    def get_online_users(self, code: str) -> list:
        """获取房间内在线用户昵称列表"""
        names = [c["nickname"] for c in self.active_connections.get(code, []) if c.get("nickname")]
        local_ids = {c.get("client_id") for c in self.active_connections.get(code, [])}
        for cid, info in self._remote.get(code, {}).items():
            if cid in local_ids:
                continue  # 同一身份本地也有连接时以本地为准，不重复计数
            if info.get("nickname"):
                names.append(info["nickname"])
        return names

    def get_members(self, code: str) -> list:
        """获取房间内在线成员（含 client_id 与昵称）"""
        members = [
            {"client_id": c.get("client_id", ""), "nickname": c["nickname"]}
            for c in self.active_connections.get(code, [])
            if c.get("nickname")
        ]
        local_ids = {c.get("client_id") for c in self.active_connections.get(code, [])}
        for cid, info in self._remote.get(code, {}).items():
            if cid in local_ids or not info.get("nickname"):
                continue
            members.append({"client_id": cid, "nickname": info["nickname"]})
        return members

    def get_client_id(self, code: str, websocket: WebSocket) -> str:
        """根据连接获取其 client_id"""
        for c in self.active_connections.get(code, []):
            if c["ws"] is websocket:
                return c.get("client_id", "")
        return ""

    def is_member_online(self, code: str, client_id: str) -> bool:
        """校验 client_id 是否为该房间当前在线成员。

        client_id 由服务端在 join 时签发（uuid4 前 16 位），未加入房间者无从得知，
        因此"该 id 正在本房间保持 WebSocket 连接"可作为成员身份凭证，
        用于房间 HTTP 接口（缓存文件上传/删除）的鉴权，阻断匿名越权调用。
        多实例下"在线"包含其它实例上的连接（成员表已镜像同步）。
        """
        if not code or not client_id:
            return False
        if any(c.get("client_id") == client_id for c in self.active_connections.get(code, [])):
            return True
        return client_id in self._remote.get(code, {})

    # ==================== 消息投递 ====================

    async def broadcast(self, code: str, message: dict):
        """向房间全员广播（本地直投 + 通知其它实例）。

        投递是"尽力而为"：本实例发送失败的连接会被即时摘掉（不等显式 close），
        远端实例是否送达无法在本进程观测——这是 pub/sub 的必要代价。
        """
        await self._deliver_local(code, message)
        if getattr(self.hub, "distributed", False):
            await self.hub.publish({"t": "text", "room": code, "msg": message})

    async def broadcast_exclude(self, code: str, exclude_ws: WebSocket, message: dict):
        """向房间内除指定连接外的所有连接广播消息"""
        exclude_cid = self.get_client_id(code, exclude_ws)
        await self._deliver_local(code, message, exclude_cid=exclude_cid)
        if getattr(self.hub, "distributed", False):
            await self.hub.publish({
                "t": "text", "room": code, "msg": message, "exclude": exclude_cid,
            })

    async def _deliver_local(
        self, code: str, message: dict, exclude_cid: str = "", only_cid: str = ""
    ) -> int:
        """只在本进程内投递，返回成功投递的连接数。"""
        conns = self.active_connections.get(code)
        if not conns:
            return 0
        data = json.dumps(message, ensure_ascii=False)
        disconnected = []
        delivered = 0
        for conn in list(conns):
            cid = conn.get("client_id")
            if exclude_cid and cid == exclude_cid:
                continue
            if only_cid and cid != only_cid:
                continue
            try:
                await conn["ws"].send_text(data)
                delivered += 1
            except Exception:
                disconnected.append(conn["ws"])
        for ws in disconnected:
            await self.disconnect(code, ws)
        return delivered

    async def send_personal(self, code: str, websocket: WebSocket, message: dict):
        """向单个连接发送消息（调用方自己的连接，永远在本地）"""
        try:
            data = json.dumps(message, ensure_ascii=False)
            await websocket.send_text(data)
        except Exception:
            await self.disconnect(code, websocket)

    async def send_to_client(self, code: str, client_id: str, message: dict) -> bool:
        """按 client_id 定向发送文本消息。

        返回是否"已在本地投递"。多实例下目标可能挂在别的实例：本地没投递成功
        才发到集群通道，避免每条定向消息都多一次 pub/sub 往返。
        """
        if not client_id:
            return False
        delivered = await self._deliver_local(code, message, only_cid=client_id)
        if delivered:
            return True
        if getattr(self.hub, "distributed", False):
            await self.hub.publish({
                "t": "text", "room": code, "msg": message, "only": client_id,
            })
        return False

    async def send_bytes_to_client(self, code: str, client_id: str, data: bytes) -> bool:
        """按 client_id 定向转发二进制帧（文件数据中继，不落盘）。

        返回值标识"是否在本实例投递成功"。跨实例刻意不转发（见 core/cluster 模块
        说明）：二进制帧走 pub/sub 要把吞吐放大 1.4 倍且延迟不可控，
        正确做法是接入层对房间做会话亲和。返回 False 时调用方据此提示发送方。
        """
        if code not in self.active_connections:
            return False
        for conn in self.active_connections[code]:
            if conn.get("client_id") == client_id:
                try:
                    await conn["ws"].send_bytes(data)
                    return True
                except Exception:
                    await self.disconnect(code, conn["ws"])
                    return False
        return False

    # ==================== 文件/媒体定向中继目标管理 ====================
    def set_file_target(self, code: str, sender_client_id: str, target_client_id: str):
        """单目标（文件传输）：覆盖为仅该接收者"""
        self.file_targets.setdefault(code, {})[sender_client_id] = {target_client_id}

    def add_file_target(self, code: str, sender_client_id: str, target_client_id: str):
        """追加目标（媒体中转广播：一个共享者可被多个查看者订阅）"""
        self.file_targets.setdefault(code, {}).setdefault(sender_client_id, set()).add(target_client_id)

    def get_file_target(self, code: str, sender_client_id: str) -> Optional[str]:
        """兼容单目标：返回第一个目标（文件传输用）"""
        targets = self.file_targets.get(code, {}).get(sender_client_id)
        if targets:
            return next(iter(targets))
        return None

    def get_file_targets(self, code: str, sender_client_id: str) -> list:
        """返回全部目标（媒体中转广播用）"""
        targets = self.file_targets.get(code, {}).get(sender_client_id)
        return list(targets) if targets else []

    def clear_file_target(self, code: str, sender_client_id: str):
        if code in self.file_targets:
            self.file_targets[code].pop(sender_client_id, None)

    def remove_file_target_receiver(self, code: str, receiver_client_id: str):
        """从该房间所有发送方的目标中移除指定接收者（查看者退出中转订阅时调用），
        保证再次订阅时转发目标干净重建。"""
        if code not in self.file_targets:
            return
        for targets in self.file_targets[code].values():
            targets.discard(receiver_client_id)

    # ==================== 踢人 / 房间清理 ====================

    async def kick_client(self, code: str, client_id: str, reason: str = "房间已被删除") -> None:
        """关闭房间内指定 client_id 的连接（含其它实例上的）。"""
        for conn in list(self.active_connections.get(code, [])):
            if conn.get("client_id") != client_id:
                continue
            try:
                await conn["ws"].close(code=4001, reason=reason)
            except Exception:
                pass
            try:
                self.active_connections[code].remove(conn)
            except ValueError:
                pass
        if getattr(self.hub, "distributed", False):
            await self.hub.publish({"t": "kick", "room": code, "client_id": client_id, "reason": reason})

    async def kick_room(self, code: str, reason: str = "房间已被删除") -> None:
        """关闭房间内全部连接并清空本房间的所有状态（含远端镜像）。

        刻意连 `_remote` 一起清：房间已被删除，留下"别处还有成员"的镜像会让
        room_size 长期虚高，而对方的 leave 事件永远不会来了（房间已不存在）。
        """
        for conn in list(self.active_connections.get(code, [])):
            try:
                await conn["ws"].close(code=4001, reason=reason)
            except Exception:
                pass
        self.active_connections.pop(code, None)
        self.file_targets.pop(code, None)
        self._remote.pop(code, None)
        if getattr(self.hub, "distributed", False):
            await self.hub.publish({"t": "kick", "room": code, "all": True, "reason": reason})

    async def sweep_rooms(self):
        """探活清扫：把"操作系统还没报告断开"的死连接摘掉。

        用文本帧 `"ping"` 而不是 ws ping/pong：starlette 的自动 pong 由底层完成，
        应用层感知不到，我们要探测的是"对端应用层是否还活着"。
        客户端遇到非 JSON 文本会忽略，因此不需要额外的协议约定。
        """
        for code, conns in list(self.active_connections.items()):
            stale = []
            for conn in list(conns):
                try:
                    # 客户端对非 JSON 的 ping 会忽略，可安全用作探活
                    await conn["ws"].send_text("ping")
                except Exception:
                    stale.append(conn["ws"])
            for ws in stale:
                await self.disconnect(code, ws)

    # ==================== 集群事件处理 ====================

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await self._publish_snapshot()
                self._prune_dead_instances()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"集群心跳异常: {e}")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _publish_snapshot(self) -> None:
        snapshot: Dict[str, list] = {}
        for code, conns in self.active_connections.items():
            members = [[c.get("client_id"), c.get("nickname") or ""] for c in conns if c.get("client_id")]
            if members:
                snapshot[code] = members
        await self.hub.publish({"t": "hb", "rooms": snapshot})

    def _prune_dead_instances(self) -> None:
        """摘掉心跳超时（`INSTANCE_TIMEOUT`）的远端实例成员。

        只按"最近一次见到该实例的时间"整体摘除，而不是逐个成员按 TTL 过期：
        后者需要给每个成员单独维护时间戳，且实例正常但成员短暂断线时会被误删。
        """
        now = time.monotonic()
        dead = {inst for inst, ts in self._inst_seen.items() if now - ts > INSTANCE_TIMEOUT}
        for inst in dead:
            self._inst_seen.pop(inst, None)
            self._drop_instance(inst)
            logger.warning(f"集群实例心跳超时，已摘除其成员: {inst}")

    def _drop_instance(self, inst: str) -> None:
        for code in list(self._remote.keys()):
            members = self._remote[code]
            for cid in [c for c, i in members.items() if i.get("inst") == inst]:
                members.pop(cid, None)
            if not members:
                self._remote.pop(code, None)

    async def _on_cluster_event(self, event: dict) -> None:
        """处理来自其它实例的事件。

        这里假设事件已是"别人的"（发送方与这里的过滤由 core.cluster 完成：
        自己发的消息不会回环），因此不做 origin == self 的二次判断。
        事件字段一律按"可能畸形"处理（缺字段/类型不对就跳过），因为通道上
        可能混入其它版本实例或非本系统的消息。
        """
        kind = event.get("t")
        origin = str(event.get("origin") or "")
        room = event.get("room") or ""

        if kind == "join":
            if not room or not origin:
                return
            cid = str(event.get("client_id") or "")
            if not cid:
                return
            self._inst_seen[origin] = time.monotonic()
            self._remote.setdefault(room, {})[cid] = {
                "nickname": str(event.get("nickname") or ""), "inst": origin,
            }
            return

        if kind == "leave":
            cid = str(event.get("client_id") or "")
            members = self._remote.get(room)
            if members and cid:
                # 只删"属于发事件那个实例"的条目：客户端换实例重连时，
                # 旧实例的 leave 可能晚于新实例的 join 到达，不能把新连接抹掉
                if members.get(cid, {}).get("inst") == origin:
                    members.pop(cid, None)
                if not members:
                    self._remote.pop(room, None)
            if cid:
                self.remove_file_target_receiver(room, cid)
            return

        if kind == "hb":
            if not origin:
                return
            self._inst_seen[origin] = time.monotonic()
            # 覆盖写：该实例的成员以快照为准，增量事件丢包由此自愈
            self._drop_instance(origin)
            for code, members in (event.get("rooms") or {}).items():
                if not isinstance(members, list):
                    continue
                bucket = self._remote.setdefault(code, {})
                for pair in members:
                    if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0]:
                        bucket[str(pair[0])] = {"nickname": str(pair[1] or ""), "inst": origin}
                if not bucket:
                    self._remote.pop(code, None)
            return

        if kind == "bye":
            if origin:
                self._inst_seen.pop(origin, None)
                self._drop_instance(origin)
            return

        if kind == "text":
            msg = event.get("msg")
            if not isinstance(msg, dict):
                return
            # 只在本实例有该房间成员时才做序列化与发送
            if room not in self.active_connections:
                return
            await self._deliver_local(
                room, msg,
                exclude_cid=str(event.get("exclude") or ""),
                only_cid=str(event.get("only") or ""),
            )
            return

        if kind == "kick":
            if not room:
                return
            reason = str(event.get("reason") or "连接已被替换")
            if event.get("all"):
                for conn in list(self.active_connections.get(room, [])):
                    try:
                        await conn["ws"].close(code=4001, reason=reason)
                    except Exception:
                        pass
                self.active_connections.pop(room, None)
                self.file_targets.pop(room, None)
                # 房间整体销毁：远端镜像一并清掉。否则本实例还记着"别处有成员"，
                # 房间删除后在线人数会虚高（等对方实例发 leave 已经等不到了）。
                self._remote.pop(room, None)
            else:
                cid = str(event.get("client_id") or "")
                for conn in list(self.active_connections.get(room, [])):
                    if conn.get("client_id") != cid:
                        continue
                    try:
                        await conn["ws"].close(code=1000, reason=reason)
                    except Exception:
                        pass
                    try:
                        self.active_connections[room].remove(conn)
                    except ValueError:
                        pass
                members = self._remote.get(room)
                if members and cid:
                    members.pop(cid, None)
                    if not members:
                        self._remote.pop(room, None)
            return


# 全局单例：hub 在 main.lifespan 中按配置替换（core.cluster.hub），
# 这里保持默认的 LocalHub，保证未配置 Redis 时与改造前完全一致。
ws_manager = ConnectionManager()
