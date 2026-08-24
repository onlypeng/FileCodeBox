# 验证文本分享/文件备注长度限制（maxTextLength）：
# - 文本分享（share_file 仅备注分支）按字符数校验
# - 文件分享备注（share_file remark / 分片合并 / 预签名）按字符数校验
from core.settings import settings


async def _short_text(client, text: str):
    # 文本分享已合并为"仅备注"分享：走 share_file，remark 携带文本内容、无文件
    return await client.post(
        "/share/file/",
        data={"remark": text, "expire_value": "1", "expire_style": "day"},
        files={},
    )


async def test_text_share_count_units(client, monkeypatch):
    """maxTextLength 按字符数（而非字节）校验：3 个中文字符在限制 3 时恰好通过，4 个拒绝。"""
    monkeypatch.setattr(settings, "maxTextLength", 3)

    resp = await _short_text(client, "三个字")
    assert resp.status_code == 200, resp.text

    resp = await _short_text(client, "四个字了")
    assert resp.status_code == 403
    assert "备注过长" in resp.text


async def test_text_share_respects_config(client, monkeypatch):
    """默认配置（200000）下长文本通过，收紧后超限拒绝。"""
    resp = await _short_text(client, "你好" * 500)
    assert resp.status_code == 200, resp.text

    monkeypatch.setattr(settings, "maxTextLength", 10)
    resp = await _short_text(client, "你好" * 6)  # 12 字符 > 10
    assert resp.status_code == 403


async def test_file_remark_limit(client, monkeypatch):
    """文件分享备注（share_file remark）超限应被拒绝；仅备注分享（无文件）允许。"""
    monkeypatch.setattr(settings, "maxTextLength", 5)

    resp = await client.post(
        "/share/file/",
        data={"remark": "这条备注太长了超过五个字", "expire_value": "1", "expire_style": "day"},
        files={},
    )
    assert resp.status_code == 403
    assert "备注过长" in resp.text

    # 无文件但备注非空：走"仅备注"分享，返回分享码
    resp = await client.post(
        "/share/file/",
        data={"remark": "短备注", "expire_value": "1", "expire_style": "day"},
        files={},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["code"] == 200
    assert resp.json()["detail"]["code"]
