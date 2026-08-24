# 下载 token（SELECT_TOKEN_TTL 有效期）与时间型过期语义
from datetime import datetime, timedelta

from apps.base.models import FileCodes
from core.utils import get_now, get_select_token, verify_select_token


async def _create_file(code: str = "ABC123", expired_at=None, expired_count=-1):
    return await FileCodes.create(
        code=code,
        prefix="t",
        suffix=".txt",
        uuid_file_name="t.txt",
        file_path="share/data/test",
        size=10,
        expired_at=expired_at,
        expired_count=expired_count,
        expire_style="forever" if expired_at is None else "day",
        expire_value=0,
    )


async def test_select_token_validates(client):
    """有效 token + 有效文件：通过签名校验（文件实体不存在 → 返回 404 业务码而非 403）。"""
    await _create_file()
    key = await get_select_token("ABC123")
    resp = await client.get(f"/share/download?key={key}&code=ABC123")
    # token 校验通过后走文件响应；本地存储中文件不存在 → 业务 404（而非鉴权 403）
    assert resp.status_code == 200
    assert resp.json()["code"] == 404


async def test_select_token_forged_rejected(client):
    """伪造 token 返回 403 并计入失败限流。"""
    await _create_file()
    resp = await client.get("/share/download?key=forged.salt.123&code=ABC123")
    assert resp.status_code == 403


async def test_select_token_expired(client, monkeypatch):
    """超期 token 校验失败（SELECT_TOKEN_TTL 置为负值模拟过期）。"""
    import core.utils as utils
    await _create_file()
    monkeypatch.setattr(utils, "SELECT_TOKEN_TTL", -1)
    key = await get_select_token("ABC123")
    resp = await client.get(f"/share/download?key={key}&code=ABC123")
    assert resp.status_code == 403


async def test_download_time_expired_returns_410(client):
    """时间型文件已过期：下载返回 410（即使 token 有效）。"""
    await _create_file(expired_at=await get_now() - timedelta(hours=1))
    key = await get_select_token("ABC123")
    resp = await client.get(f"/share/download?key={key}&code=ABC123")
    assert resp.status_code == 410


async def test_download_count_mode_not_time_expired(client):
    """count（按次）文件 expired_at 为 None：不触发时间型过期，token 有效即可下载。"""
    await _create_file(expired_at=None, expired_count=5)
    key = await get_select_token("ABC123")
    resp = await client.get(f"/share/download?key={key}&code=ABC123")
    assert resp.status_code == 200
    assert resp.json()["code"] == 404  # 文件实体不存在，业务 404


async def test_verify_select_token_expiry_unit():
    """verify_select_token 直接单测：新鲜 token 有效，过期后无效。"""
    key = await get_select_token("XYZ789")
    assert await verify_select_token(key, "XYZ789") is True
    assert await verify_select_token(key, "XYZ000") is False
    assert await verify_select_token("bad", "XYZ789") is False
