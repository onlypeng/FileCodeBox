import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from apps.base.models import CollectionBox
from core.websocket import ws_manager

ws_api = APIRouter(tags=["WebSocket"])


@ws_api.websocket("/ws/collection/{code}")
async def websocket_collection(websocket: WebSocket, code: str):
    """收件箱实时推送 WebSocket"""
    # 支持通过 collection_code 或 delivery_code 连接
    collection = await CollectionBox.filter(collection_code=code).first()
    if not collection:
        collection = await CollectionBox.filter(delivery_code=code).first()
    if not collection:
        await websocket.close(code=404, reason="收件箱不存在")
        return

    if await collection.is_expired():
        await websocket.close(code=410, reason="收件箱已过期")
        return

    room_code = collection.collection_code
    # 等待客户端发送第一条消息获取昵称
    await ws_manager.connect(room_code, websocket, nickname="")
    try:
        # 第一条消息作为昵称注册
        first_msg = await websocket.receive_text()
        nickname = ""
        try:
            msg_data = json.loads(first_msg)
            if isinstance(msg_data, dict) and msg_data.get("type") == "join":
                nickname = msg_data.get("nickname", "")
                # 更新昵称
                for conn in ws_manager.active_connections.get(room_code, []):
                    if conn["ws"] is websocket:
                        conn["nickname"] = nickname
                        break
        except (json.JSONDecodeError, ValueError):
            pass

        # 广播用户加入
        online_users = ws_manager.get_online_users(room_code)
        await ws_manager.broadcast(room_code, {
            "type": "user_joined",
            "nickname": nickname,
            "online_users": online_users,
        })

        while True:
            data = await websocket.receive_text()
            # 客户端发送的进度消息，广播给房间内其他连接
            try:
                msg = json.loads(data)
                if isinstance(msg, dict) and msg.get("type") in ("file_uploading", "file_progress"):
                    await ws_manager.broadcast(room_code, msg)
            except (json.JSONDecodeError, ValueError):
                pass  # 忽略非JSON消息（如ping）
    except WebSocketDisconnect:
        left_nickname = ws_manager.disconnect(room_code, websocket)
        # 广播用户离开
        online_users = ws_manager.get_online_users(room_code)
        await ws_manager.broadcast(room_code, {
            "type": "user_left",
            "nickname": left_nickname,
            "online_users": online_users,
        })