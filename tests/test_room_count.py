# 房间过期规则：count 模式按"房间从有人变为无人"计一次；次数扣尽即过期
import datetime

import pytest

from apps.base.models import Room
from core.utils import get_now


async def _create_room(client, expire_style: str, expire_value: int) -> Room:
    resp = await client.post(
        "/room/create/",
        json={
            "title": "测试房间",
            "expire_style": expire_style,
            "expire_value": expire_value,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    return body["detail"]


async def test_room_check_code_type_is_direct(client):
    """房间码统一识别为 direct（去 chat/transfer/direct 三模式后）。"""
    room = await _create_room(client, "day", 1)
    resp = await client.get(f"/share/check_code/?code={room['room_code']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["detail"]["type"] == "direct"


async def test_create_count_room_sets_expired_count(client):
    """count 模式创建：expired_count = expire_value，且不限制时间（expired_at 为 None）。"""
    room = await _create_room(client, "count", 2)
    assert room["expired_count"] == 2
    assert room["expired_at"] is None


async def test_create_time_room_has_no_expired_count(client):
    """时间模式（如 day）创建：expired_count = -1（不按次数限制），expired_at 有值。"""
    room = await _create_room(client, "day", 1)
    assert room["expired_count"] == -1
    assert room["expired_at"] is not None


@pytest.mark.parametrize(
    ("expired_count", "expected"),
    [
        (2, False),  # 剩余次数 > 0：未过期
        (1, False),
        (0, True),   # 次数扣尽：过期
        (-1, True),  # -1 是默认值，count 模式下视为次数耗尽
    ],
)
async def test_room_is_expired_count_logic(expired_count, expected):
    """count 模式：剩余次数 <= 0 即视为过期。"""
    room = Room(expire_style="count", expire_value=1, expired_count=expired_count, expired_at=None)
    assert await room.is_expired() is expected


async def test_room_is_expired_time_logic():
    """时间模式：按 expired_at 判断；forever 永不过期。"""
    now = await get_now()
    expired_in_past = Room(expire_style="day", expired_at=now - datetime.timedelta(minutes=1), expired_count=-1)
    assert await expired_in_past.is_expired() is True

    valid_in_future = Room(expire_style="day", expired_at=now + datetime.timedelta(days=1), expired_count=-1)
    assert await valid_in_future.is_expired() is False

    forever = Room(expire_style="forever", expired_at=now - datetime.timedelta(days=1), expired_count=-1)
    assert await forever.is_expired() is False
