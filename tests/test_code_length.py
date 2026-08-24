# 验证口令/码位数调整后的兼容性：6 位码与 8 位码均可正常取件
import pytest
from fastapi import HTTPException

from apps.base.models import FileCodes
from apps.base.views import _validate_code
from core.settings import settings
from core.utils import get_random_string


# ==================== 单元测试：码格式校验 ====================

@pytest.mark.parametrize(
    "code",
    [
        "A1B2",         # 4 位（下限）
        "ABCDEF",       # 6 位（默认位数）
        "ABCDEFGH",     # 8 位（调大后的位数）
        "A1B2C3D4E5F6", # 12 位（上限）
    ],
)
def test_validate_code_accepts_6_and_8_digit_codes(code):
    """6 位与 8 位码都应通过格式校验（范围 4~12）。"""
    assert _validate_code(code) == code


@pytest.mark.parametrize(
    "code",
    [
        "ABC",              # 3 位：过短
        "A1B2C3D4E5F6G7",   # 13 位：过长
        "A1B2-C3D4",        # 含非法字符
        "A1B2 C3",          # 含空格
        "",                 # 空码
    ],
)
def test_validate_code_rejects_invalid_codes(code):
    """超出 4~12 位范围或包含非法字符的码应被拒绝。"""
    with pytest.raises(HTTPException):
        _validate_code(code)


def test_validate_code_normalizes_lowercase():
    """小写字母码应自动转大写后通过。"""
    assert _validate_code("abcd1234") == "ABCD1234"


# ==================== 单元测试：码生成长度 ====================

@pytest.mark.parametrize(
    ("configured", "expected_len"),
    [
        (8, 8),   # 调大到 8 位 → 生成 8 位
        (6, 6),   # 默认 6 位 → 生成 6 位
        (13, 12), # 超出上限 → 钳制到 12 位
        (2, 4),   # 低于下限 → 钳制到 4 位
    ],
)
async def test_generate_code_length_follows_setting(monkeypatch, configured, expected_len):
    """码位数配置决定新生成码的长度（钳制在 4~12）。"""
    monkeypatch.setattr(settings, "codeLength", configured)
    code = await get_random_string()
    assert len(code) == expected_len
    assert code.isalnum()


# ==================== 集成测试：真实取件流程 ====================

async def _create_text_share(code: str, text: str) -> FileCodes:
    """在测试库中创建一条永不过期的文本分享记录（模拟对应位数配置下生成的码）。

    文本内容存 remark（无文件路径），expire_style=forever（expired_count=-1、expired_at=None）。
    """
    return await FileCodes.create(
        code=code,
        prefix="Text",
        suffix="",
        remark=text,
        size=len(text),
        expire_style="forever",
        expire_value=0,
        expired_at=None,
        expired_count=-1,
        used_count=0,
    )


async def _assert_retrieve(client, code: str, expected_text: str):
    """走完整取件流程：check_code 识别类型 → select 获取内容。"""
    # 1. 检查码类型
    resp = await client.get(f"/share/check_code/?code={code}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["detail"]["type"] == "file"

    # 2. 取件
    resp = await client.post("/share/select/", json={"code": code})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["detail"]["code"] == code
    assert body["detail"]["text"] == expected_text


async def test_6_digit_code_can_retrieve(client):
    """6 位码可以正常取件。"""
    await _create_text_share("ABC123", "六位码文本")
    await _assert_retrieve(client, "ABC123", "六位码文本")


async def test_8_digit_code_can_retrieve(client):
    """8 位码可以正常取件。"""
    await _create_text_share("ABCD1234", "八位码文本")
    await _assert_retrieve(client, "ABCD1234", "八位码文本")


async def test_6_and_8_digit_codes_coexist(client):
    """核心场景：调整码位数后新旧码共存，两者都能正常取件。"""
    await _create_text_share("ABC123", "六位码文本")
    await _create_text_share("ABCD1234", "八位码文本")
    await _assert_retrieve(client, "ABC123", "六位码文本")
    await _assert_retrieve(client, "ABCD1234", "八位码文本")


async def test_retrieve_with_unknown_code_returns_404(client):
    """不存在的码取件应返回业务码 404。"""
    resp = await client.post("/share/select/", json={"code": "ZZZZ9999"})
    assert resp.status_code == 200
    assert resp.json()["code"] == 404
