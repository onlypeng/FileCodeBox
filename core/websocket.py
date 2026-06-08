# @Time    : 2024/01/01
# @Author  : FileCodeBox
# @File    : websocket.py
# @Software: PyCharm
import json
from typing import Dict, List

from fastapi import WebSocket

from core.logger import logger


class ConnectionManager:
    """WebSocket 连接管理器，按收件箱 code 分组"""

    def __init__(self):
        # room_code -> list[{ws: WebSocket, nickname: str}]
        self.active_connections: Dict[str, List[dict]] = {}

    async def connect(self, code: str, websocket: WebSocket, nickname: str = ""):
        """接受连接并加入房间"""
        await websocket.accept()
        if code not in self.active_connections:
            self.active_connections[code] = []
        self.active_connections[code].append({"ws": websocket, "nickname": nickname})
        logger.info(f"WebSocket 连接加入房间 {code}，当前连接数: {len(self.active_connections[code])}")

    def disconnect(self, code: str, websocket: WebSocket):
        """断开连接并离开房间，返回断开连接的昵称"""
        nickname = ""
        if code in self.active_connections:
            for i, conn in enumerate(self.active_connections[code]):
                if conn["ws"] is websocket:
                    nickname = conn.get("nickname", "")
                    self.active_connections[code].pop(i)
                    break
            if not self.active_connections[code]:
                del self.active_connections[code]
        logger.info(f"WebSocket 连接离开房间 {code}")
        return nickname

    def get_online_users(self, code: str) -> list:
        """获取房间内在线用户昵称列表"""
        if code not in self.active_connections:
            return []
        return [c["nickname"] for c in self.active_connections[code] if c["nickname"]]

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

    async def send_personal(self, code: str, websocket: WebSocket, message: dict):
        """向单个连接发送消息"""
        try:
            data = json.dumps(message, ensure_ascii=False)
            await websocket.send_text(data)
        except Exception:
            self.disconnect(code, websocket)


# 全局单例
ws_manager = ConnectionManager()
