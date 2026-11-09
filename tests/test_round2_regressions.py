"""第 2 轮只读审查确认的缺陷 + 候选功能中，适合函数级固化的回归测试。

设计原则与前一份一致：走真实函数、真实临时 SQLite、真实磁盘，不 mock 掉被测逻辑。
HTTP 层的端到端行为（A1/A3/A4/A5/B2/B3/B5/G1）另由
`.workbuddy/e2e_round2_test.py` 覆盖；这里补齐那些只有落到持久化/磁盘层
才能证明的行为。

覆盖：
- B3  `pagination()` 必须回传钳制后的 size（否则 .limit(size) 仍用原始上万值）
- G2  count 额度扣到 0 时写入 expired_at（耗尽时刻，供清理宽限起算）
- A1  最后一次取件签发的 token 在短窗口内放行；陈旧/存量 2 段 token 一律拒绝
- G4  `core.audit.record()` 未显式传 ip 时回落到请求上下文注入的可信来源 IP
- B6  迁移 020 单行落盘失败时保留 text 列（不再无条件 DROP COLUMN 丢正文）
- A2  房间缓存去重共享物理文件：仍有其它条目/历史消息引用时不删，无引用才删
"""
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.base.models import AuditLog, FileCodes, RoomCachedFile, RoomMessage
from apps.base.views import (
    _LAST_RETRIEVE_WINDOW,
    _count_exhausted_download_allowed,
    _token_issued_at,
    update_file_usage,
)
from core import room_cache
from core.audit import get_request_ip, record, set_request_ip
from core.utils import get_now, pagination


# ---------------------------------------------------------------- B3
def test_b3_pagination_returns_clamped_size():
    """size 被钳制后必须回传，否则调用方 .limit(size) 仍用原始值。"""
    assert pagination(1, 9999) == (1, 100, 0)
    assert pagination(3, 50) == (3, 50, 100)
    assert pagination(0, 0) == (1, 1, 0)
    assert pagination(2, 500, max_size=20) == (2, 20, 20)


# ---------------------------------------------------------------- G2
async def test_g2_count_exhaustion_records_expired_at(tortoise_db):
    """count 模式扣到 0 时记下真实耗尽时刻；时间型不能被误写。"""
    fc = await FileCodes.create(code="G2EXH001", size=0,
                                expired_count=1, expired_at=None)
    assert fc.expired_at is None
    assert await update_file_usage(fc) is True
    await fc.refresh_from_db()
    assert fc.expired_count == 0
    assert fc.expired_at is not None, "额度耗尽必须写入 expired_at"

    # 对照：时间/永久模式（expired_count=-1）不应被写入耗尽时刻
    fc2 = await FileCodes.create(
        code="G2TIME01", size=0, expired_count=-1,
        expired_at=await get_now() + timedelta(days=1),
    )
    before = fc2.expired_at
    assert await update_file_usage(fc2) is True
    await fc2.refresh_from_db()
    assert fc2.expired_at == before


# ---------------------------------------------------------------- A1
def test_a1_last_retrieve_download_window():
    """只放行伴随最后一次取件签发的 token；陈旧/伪造/存量链接一律拒绝。"""
    now = int(time.time())
    fresh = f"hash.salt.{now}"
    stale = f"hash.salt.{now - _LAST_RETRIEVE_WINDOW - 10}"
    forged = f"hash.salt.{now + 120}"  # 签发时间被推到未来
    legacy = "hash.salt"               # 存量 2 段格式，无时间戳
    exhausted = SimpleNamespace(expired_count=0)

    assert _token_issued_at(fresh) == now
    assert _token_issued_at(legacy) == 0
    assert _count_exhausted_download_allowed(exhausted, fresh) is True
    assert _count_exhausted_download_allowed(exhausted, stale) is False
    assert _count_exhausted_download_allowed(exhausted, forged) is False
    assert _count_exhausted_download_allowed(exhausted, legacy) is False


# ---------------------------------------------------------------- G4
async def test_g4_audit_ip_falls_back_to_request_context(tortoise_db):
    """后台写操作不显式传 ip 时，自动取中间件注入的可信来源 IP。"""
    set_request_ip("203.0.113.7")
    try:
        assert get_request_ip() == "203.0.113.7"
        await record("config_update", target_type="system", target_id="probe",
                     result="success", detail="round2-probe", actor="admin")
        row = await AuditLog.filter(detail="round2-probe").order_by("-id").first()
        assert row is not None
        assert row.ip == "203.0.113.7"

        # 显式传 ip 仍优先，保证既有调用方语义不被改坏
        await record("config_update", target_type="system", target_id="probe",
                     result="success", detail="round2-explicit", actor="admin",
                     ip="198.51.100.9")
        row2 = await AuditLog.filter(detail="round2-explicit").order_by("-id").first()
        assert row2 is not None and row2.ip == "198.51.100.9"
    finally:
        set_request_ip("")


# ---------------------------------------------------------------- B6
async def test_b6_migration_keeps_text_column_on_write_failure(
    tortoise_db, tmp_path, monkeypatch
):
    """单行落盘失败时保留 text 列：旧写法无条件 DROP 会不可逆丢掉那几行正文。"""
    import core.settings as settings_mod
    from tortoise import connections
    from apps.base.migrations import migrations_020

    conn = connections.get("default")
    await conn.execute_script("ALTER TABLE filecodes ADD COLUMN text TEXT")
    fc = await FileCodes.create(code="MIG020A", size=0, expired_count=-1)
    await conn.execute_query(
        "UPDATE filecodes SET text = ? WHERE id = ?", ["keep-me-body", fc.id]
    )

    monkeypatch.setattr(settings_mod, "data_root", tmp_path)
    original = Path.write_text

    def selective_failure(self, *args, **kwargs):
        if self.suffix == ".txt" and self.parent.name == "text":
            raise OSError("simulated disk full")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", selective_failure)

    with pytest.raises(RuntimeError, match="落盘失败"):
        await migrations_020.migrate()

    cols = await conn.execute_query_dict("PRAGMA table_info(filecodes)")
    assert any(c["name"] == "text" for c in cols), "text 列必须保留待重跑"
    rows = await conn.execute_query_dict(
        "SELECT text FROM filecodes WHERE id = ?", [fc.id]
    )
    assert rows[0]["text"] == "keep-me-body", "失败的正文不能被丢掉"


# ---------------------------------------------------------------- A2
async def test_a2_room_cache_shared_physical_file_is_preserved(
    tortoise_db, tmp_path, monkeypatch
):
    """去重共享同一落盘文件时，删一条不能让另一条的下载集体 404。"""
    root = tmp_path / "room_cache"
    root.mkdir()
    monkeypatch.setattr(room_cache, "CACHE_ROOT", root)

    key = "ROOM1/shared.bin"
    target = root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"R2-A2-SHARED")

    a = await RoomCachedFile.create(
        room_code="ROOM1", transfer_id="tid-a", storage_key=key
    )
    b = await RoomCachedFile.create(
        room_code="ROOM1", transfer_id="tid-b", storage_key=key
    )

    kept = await room_cache.delete_room_file_if_unreferenced(
        "ROOM1", key, exclude_cached_id=a.id, exclude_transfer_id="tid-a"
    )
    assert kept is False
    assert target.exists(), "另一条缓存仍引用该文件，物理文件必须保留"

    # 本函数只做引用检查与删盘，删记录是调用方的事：模拟第一个删除操作收尾，
    # 否则 a 记录仍在库里，第二次调用会（正确地）继续判定"有引用"。
    await a.delete()
    removed = await room_cache.delete_room_file_if_unreferenced(
        "ROOM1", key, exclude_cached_id=b.id, exclude_transfer_id="tid-b"
    )
    assert removed is True
    assert not target.exists(), "最后一条引用消失后应真正删除物理文件"


async def test_a2_room_cache_history_message_keeps_file(
    tortoise_db, tmp_path, monkeypatch
):
    """历史文件消息仍引用 storage_key 时，过期清理也不能删物理文件。"""
    root = tmp_path / "room_cache"
    root.mkdir()
    monkeypatch.setattr(room_cache, "CACHE_ROOT", root)

    key = "ROOM2/history.bin"
    target = root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"R2-A2-HISTORY")

    await RoomMessage.create(
        room_code="ROOM2", content="tid-old", msg_type="file",
        storage_key=key,
    )
    removed = await room_cache.delete_room_file_if_unreferenced(
        "ROOM2", key, exclude_transfer_id="tid-being-deleted"
    )
    assert removed is False
    assert target.exists(), "历史消息仍引用时不能删"
