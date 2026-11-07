"""提交后审查发现的 6 个缺陷 + 2 个新增功能的回归测试。

设计原则：尽量走真实函数、真实临时 SQLite、真实磁盘，而不是把被测逻辑 mock 掉。
这里的每一条都对应「修复前会失败、修复后通过」的具体行为，便于将来改动时
第一时间发现回归，而不是靠一遍遍人工重放浏览器操作。

覆盖：
- B1 `core/config.refresh_settings` 必须解密 `fe:` 新格式密文
- B2 `core/tasks.delete_expire_files` 在 forever（retention=None）时不删除
- B3 count 模式 expire_value 负数/0 必须被钳制为至少 1 次
- B4 多文件分享的物理文件（由 FileItem 管理）必须能被删除，且共享文件要保留
- B5 `find_duplicate_file` 只命中有效的次数型/时间型记录
- F1 `chunkExpireHours` 可后台保存、可读回、非法值被拒
- F3 `/admin/dashboard` 下发秒传去重统计且口径正确
"""
import asyncio
import logging
from datetime import timedelta

import pytest

from apps.base.models import FileCodes, FileItem, KeyValue
from apps.base.utils import get_expire_info
from apps.base.views import _calculate_expired_at
from core.config import refresh_settings
from core.file_dedup import delete_physical_files, find_duplicate_file
from core.settings import settings
from core.storage import SystemFileStorage
from core.tasks import delete_expire_files
from core.utils import encrypt_value, get_now

logger = logging.getLogger("fix-regressions")


@pytest.fixture(autouse=True)
def _restore_runtime_settings():
    """被测逻辑会重灌 settings.user_config，测试结束后恢复，避免污染其它用例。"""
    saved = dict(settings.user_config)
    yield
    settings.user_config = saved


async def _create_settings_row(value: dict) -> None:
    await KeyValue.create(key="settings", value=value)


async def _run_one_cleanup_sweep() -> None:
    """让 `delete_expire_files` 跑完一轮逻辑后退出（它是 while True + 600s sleep）。

    用 cancel 打断 sleep，已经执行过的那一轮查询/删除逻辑是真实的。
    """
    task = asyncio.create_task(delete_expire_files())
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------- B1
async def test_b1_refresh_settings_decrypts_fe_prefix(tortoise_db):
    """encrypt_value 产出 fe: 前缀；只认 enc: 会把密文当明文灌进运行时。"""
    plain = "s3-plaintext-secret"
    encrypted = encrypt_value(plain)
    assert encrypted.startswith("fe:")
    await _create_settings_row({"s3_secret_access_key": encrypted})

    await refresh_settings()

    assert settings.user_config["s3_secret_access_key"] == plain


# ---------------------------------------------------------------- B2
async def test_b2_forever_retention_keeps_expired_records(tortoise_db, tmp_path, monkeypatch):
    """forever（retention=None）必须整轮跳过，不能反而立即删除过期数据。"""
    import core.storage as storage_mod
    import core.tasks as tasks_mod

    monkeypatch.setattr(storage_mod, "data_root", tmp_path)
    monkeypatch.setattr(tasks_mod, "data_root", tmp_path)
    await _create_settings_row({"expiredRetentionStyle": "forever", "expiredRetentionValue": 1})

    now = await get_now()
    rec = await FileCodes.create(
        code="FOREVER1", size=0, expired_count=-1,
        expired_at=now - timedelta(days=2),
    )

    await _run_one_cleanup_sweep()

    assert await FileCodes.filter(id=rec.id).exists(), "forever 不应清理任何过期记录"


async def test_b2_finite_retention_still_deletes(tortoise_db, tmp_path, monkeypatch):
    """对照：有限宽限期到期后仍要真的删除，确认上一用例不是把功能整体关掉。"""
    import core.storage as storage_mod
    import core.tasks as tasks_mod

    monkeypatch.setattr(storage_mod, "data_root", tmp_path)
    monkeypatch.setattr(tasks_mod, "data_root", tmp_path)
    await _create_settings_row({"expiredRetentionStyle": "minute", "expiredRetentionValue": 1})

    now = await get_now()
    rec = await FileCodes.create(
        code="FINITE1", size=0, expired_count=-1,
        expired_at=now - timedelta(days=2),
    )

    await _run_one_cleanup_sweep()

    assert not await FileCodes.filter(id=rec.id).exists(), "宽限期已过应删除"


# ---------------------------------------------------------------- B3
async def test_b3_count_expire_value_is_clamped(tortoise_db):
    """count 模式负数会让 expired_count=-1 → 时间型 + expired_at=None → 永不过期。"""
    _, negative, _, _ = await get_expire_info(-1, "count")
    _, zero, _, _ = await get_expire_info(0, "count")
    _, normal, _, _ = await get_expire_info(5, "count")

    assert negative == 1
    assert zero == 1
    assert normal == 5
    # 钳制后的记录是「有效、还剩 1 次」，不是绕过配额的永久分享
    assert not await FileCodes(expired_at=None, expired_count=negative).is_expired()

    # 收件箱子码的 count 分支不能把 365 天 × 负数算成过去时间
    from datetime import datetime, timezone

    base = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))
    assert _calculate_expired_at("count", -5, base) > base


# ---------------------------------------------------------------- B4
async def test_b4_multi_file_physical_files_are_deleted(tortoise_db, tmp_path, monkeypatch):
    """多文件 FileCodes.uuid_file_name 恒为 None，物理文件由 FileItem 逐项管理。"""
    import core.storage as storage_mod

    monkeypatch.setattr(storage_mod, "data_root", tmp_path)
    storage = SystemFileStorage()
    rel = "share/data/b4case"
    d = tmp_path / rel
    d.mkdir(parents=True)
    (d / "a.txt").write_bytes(b"aaa")
    (d / "b.txt").write_bytes(b"bbb")

    fc = await FileCodes.create(
        code="MFB4", is_multi_file=True, file_path=rel,
        uuid_file_name=None, size=6,
    )
    await FileItem.create(file_code=fc, file_name="a.txt", file_size=3,
                          storage_path=rel, uuid_file_name="a.txt")
    await FileItem.create(file_code=fc, file_name="b.txt", file_size=3,
                          storage_path=rel, uuid_file_name="b.txt")

    await delete_physical_files(storage, fc, logger)

    assert not (d / "a.txt").exists(), "多文件的子文件应被真正删除"
    assert not (d / "b.txt").exists(), "多文件的子文件应被真正删除"


async def test_b4_shared_physical_file_is_preserved(tortoise_db, tmp_path, monkeypatch):
    """被其它分享的 FileItem 引用的物理文件不能删（秒传共享）。"""
    import core.storage as storage_mod

    monkeypatch.setattr(storage_mod, "data_root", tmp_path)
    storage = SystemFileStorage()
    rel = "share/data/b4shared"
    d = tmp_path / rel
    d.mkdir(parents=True)
    (d / "shared.bin").write_bytes(b"shared")

    fc1 = await FileCodes.create(code="SHR1", is_multi_file=True, file_path=rel, uuid_file_name=None)
    fc2 = await FileCodes.create(code="SHR2", is_multi_file=True, file_path=rel, uuid_file_name=None)
    await FileItem.create(file_code=fc1, file_name="shared.bin", file_size=6,
                          storage_path=rel, uuid_file_name="shared.bin")
    await FileItem.create(file_code=fc2, file_name="shared.bin", file_size=6,
                          storage_path=rel, uuid_file_name="shared.bin")

    await delete_physical_files(storage, fc1, logger)

    assert (d / "shared.bin").exists(), "仍被 fc2 引用，不能连带删除"


# ---------------------------------------------------------------- B5
async def test_b5_find_duplicate_matches_only_active_records(tortoise_db):
    """expired_count>=0 是次数型且 0 代表已耗尽；<0 是时间型。"""
    now = await get_now()
    h, s = "a" * 64, 1234

    active = await FileCodes.create(
        code="ACT1", file_hash=h, size=s, expired_count=5, expired_at=None,
    )
    await FileCodes.create(
        code="EXH1", file_hash=h, size=s, expired_count=0, expired_at=None,
    )
    await FileCodes.create(
        code="TME1", file_hash=h, size=s, expired_count=-1,
        expired_at=now - timedelta(days=1),
    )

    hit = await find_duplicate_file(h, s)
    assert hit is not None and hit.id == active.id, "应命中未耗尽的有效次数型分享"

    await active.delete()
    assert await find_duplicate_file(h, s) is None, "已耗尽/已过期记录不能当秒传源"


# ---------------------------------------------------------------- F1
async def test_f1_chunk_expire_hours_is_configurable(client, admin_token):
    """新配置键必须同时进 DEFAULT_CONFIG 与 UpdateConfigRequest 白名单才会生效。"""
    headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.patch(
        "/admin/config/update", json={"chunkExpireHours": 3}, headers=headers
    )
    assert resp.status_code == 200, resp.text

    got = await client.get("/admin/config/get", headers=headers)
    assert got.json()["detail"]["chunkExpireHours"] == 3

    bad = await client.patch(
        "/admin/config/update", json={"chunkExpireHours": 0}, headers=headers
    )
    assert bad.status_code == 400, "小于 1 小时应被服务端拒绝"


# ---------------------------------------------------------------- F3
async def test_f3_dashboard_exposes_dedup_stats(client, admin_token):
    """两条记录共享一份物理文件：逻辑 200 / 物理 100 / 节省 100 / 去重率 50%。"""
    await KeyValue.create(key="sys_start", value=1700000000000)
    rel = "share/data/f3case"
    a = await FileCodes.create(
        code="F3A", file_path=rel, uuid_file_name="same.bin", size=100,
        file_hash="b" * 64,
    )
    await FileCodes.create(
        code="F3B", file_path=rel, uuid_file_name="same.bin", size=100,
        file_hash="b" * 64,
    )
    await FileItem.create(file_code=a, file_name="same.bin", file_size=100,
                          storage_path=rel, uuid_file_name="same.bin")

    resp = await client.get(
        "/admin/dashboard", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()["detail"]

    for key in ("dedupLogicalBytes", "dedupPhysicalBytes", "dedupSavedBytes",
                "dedupRate", "dedupUniqueFiles"):
        assert key in d, f"概览缺少 {key}"
    assert d["dedupLogicalBytes"] == 200
    assert d["dedupPhysicalBytes"] == 100
    assert d["dedupSavedBytes"] == 100
    assert d["dedupRate"] == 50.0
    assert d["dedupUniqueFiles"] == 1
