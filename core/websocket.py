# @Time    : 2024/01/01
# @Author  : FileCodeBox
# @File    : websocket.py
# @Software: PyCharm
import json
import uuid
from typing import Dict, List, Optional

from fastapi import WebSocket

from core.logger import logger


class ConnectionManager:
    """WebSocket 连接管理器，按房间 code 分组

    client_id 由服务端签发（首帧 join 时生成并回传给客户端），客户端不得自报，
    防止伪造他人 client_id 接收/劫持定向文件传输（中继目标按 client_id 路由）。
    持久化场景（断线重连复用身份）由客户端在 join 时携带上次签发的 id，
    服务端校验其归属（该 id 无其他在线连接时允许沿用）。
    """

    def __init__(self):
        # room_code -> list[{ws: WebSocket, nickname: str, client_id: str}]
        self.active_connections: Dict[str, List[dict]] = {}
        # 文件/媒体定向中继目标：room_code -> {sender_client_id: set(target_client_id)}
        # 单目标（文件传输）用 set_file_target 覆盖；多目标（媒体广播）用 add_file_target 追加
        self.file_targets: Dict[str, Dict[str, set]] = {}

    async def connect(self, code: str, websocket: WebSocket, nickname: str = "", client_id: str = ""):
        """接受连接并加入房间；client_id 为空则由服务端签发"""
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
        return client_id

    def _is_client_id_available(self, code: str, client_id: str) -> bool:
        """client_id 在房间内未被其他在线连接占用（可用则允许沿用，否则重新签发）"""
        return client_id and not any(
            c.get("client_id") == client_id
            for c in self.active_connections.get(code, [])
        )

    def _issue_client_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def disconnect(self, code: str, websocket: WebSocket):
        """断开连接并离开房间，返回断开连接的昵称"""
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
        logger.info(f"WebSocket 连接离开房间 {code}")
        return nickname

    def get_online_users(self, code: str) -> list:
        """获取房间内在线用户昵称列表"""
        if code not in self.active_connections:
            return []
        return [c["nickname"] for c in self.active_connections[code] if c["nickname"]]

    def get_members(self, code: str) -> list:
        """获取房间内在线成员（含 client_id 与昵称）"""
        if code not in self.active_connections:
            return []
        return [
            {"client_id": c.get("client_id", ""), "nickname": c["nickname"]}
            for c in self.active_connections[code]
            if c["nickname"]
        ]

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
        """
        if not code or not client_id:
            return False
        return any(
            c.get("client_id") == client_id
            for c in self.active_connections.get(code, [])
        )

    async def broadcast(self, code: str, message: dict):
        """向房间内所有连接广播消息"""
        if code not in self.active_connections:
            return
        data = json.dumps(message, ensure_ascii=False)
        disconnected = []
        for conn in self.active_connections[code]:
            try:
                await conn["ws"].send_text(data)
            except Exception:
                disconnected.append(conn["ws"])
        # 清理断开的连接
        for ws in disconnected:
            self.disconnect(code, ws)

    async def broadcast_exclude(self, code: str, exclude_ws: WebSocket, message: dict):
        """向房间内除指定连接外的所有连接广播消息"""
        if code not in self.active_connections:
            return
        data = json.dumps(message, ensure_ascii=False)
        disconnected = []
        for conn in self.active_connections[code]:
            if conn["ws"] is exclude_ws:
                continue
            try:
                await conn["ws"].send_text(data)
            except Exception:
                disconnected.append(conn["ws"])
        for ws in disconnected:
            self.disconnect(code, ws)

    async def send_personal(self, code: str, websocket: WebSocket, message: dict):
        """向单个连接发送消息"""
        try:
            data = json.dumps(message, ensure_ascii=False)
            await websocket.send_text(data)
        except Exception:
            self.disconnect(code, websocket)

    async def send_to_client(self, code: str, client_id: str, message: dict):
        """按 client_id 定向发送文本消息"""
        if code not in self.active_connections:
            return
        for conn in self.active_connections[code]:
            if conn.get("client_id") == client_id:
                try:
                    await conn["ws"].send_text(json.dumps(message, ensure_ascii=False))
                except Exception:
                    self.disconnect(code, conn["ws"])
                return

    async def send_bytes_to_client(self, code: str, client_id: str, data: bytes):
        """按 client_id 定向转发二进制帧（文件数据中继，不落盘）"""
        if code not in self.active_connections:
            return
        for conn in self.active_connections[code]:
            if conn.get("client_id") == client_id:
                try:
                    await conn["ws"].send_bytes(data)
                except Exception:
                    self.disconnect(code, conn["ws"])
                return

    # ============ 文件/媒体定向中继目标管理 ============
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

    async def sweep_rooms(self):
        """心跳探测：剔除已断开但尚未清理的连接（防止在线人数虚高）"""
        for code, conns in list(self.active_connections.items()):
            stale = []
            for conn in conns:
                try:
                    # 客户端对非 JSON 的 ping 会忽略，可安全用作探活
                    await conn["ws"].send_text("ping")
                except Exception:
                    stale.append(conn["ws"])
            for ws in stale:
                self.disconnect(code, ws)


# 全局单例
ws_manager = ConnectionManager()
