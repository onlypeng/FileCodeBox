# 收件箱三视角响应构建：权限字段隔离（管理码/取件码/投件码互不越界）
from datetime import timedelta

from apps.base.models import CollectionBox, CollectionFile, FileCodes
from apps.base.views import (
    _build_collection_response,
    _build_retrieve_response,
    _build_delivery_view_response,
)
from core.utils import get_now


async def _make_box() -> CollectionBox:
    now = await get_now()
    box = await CollectionBox.create(
        collection_code="AAA111",
        delivery_code="BBB222",
        retrieve_code="CCC333",
        title="t",
        description="d",
        max_files=3,
        expire_style="day",
        expire_value=7,
        expired_at=now + timedelta(days=7),
        delivery_expire_style="day",
        delivery_expire_value=3,
        delivery_expired_at=now + timedelta(days=3),
        retrieve_expire_style="hour",
        retrieve_expire_value=5,
        retrieve_expired_at=now + timedelta(hours=5),
        creator_ip="1.2.3.4",
    )
    fc = await FileCodes.create(code="ZZZ9", prefix="f", suffix=".txt", size=10, expired_count=-1)
    await CollectionFile.create(collection=box, file_code=fc, status="completed", uploader_name="u")
    return box


async def _check(resp: dict, expect_have: list, expect_missing: list, name: str):
    for k in expect_have:
        assert k in resp, f"{name}: 缺少字段 {k}"
    for k in expect_missing:
        assert k not in resp, f"{name}: 越权暴露字段 {k}"


async def test_admin_view_has_all_codes(tortoise_db):
    """管理码视角：三码 + 投件/取件子码过期字段齐全。"""
    box = await _make_box()
    resp = await _build_collection_response(box)
    await _check(
        resp,
        ["collection_code", "delivery_code", "retrieve_code",
         "delivery_expire_style", "retrieve_expire_style", "file_count", "total_size", "files"],
        [],
        "admin",
    )


async def test_retrieve_view_isolates_codes(tortoise_db):
    """取件码视角：仅 retrieve_code，不暴露管理码/投件码及其过期字段。"""
    box = await _make_box()
    resp = await _build_retrieve_response(box)
    await _check(
        resp,
        ["retrieve_code", "retrieve_expire_style", "file_count", "total_size", "files"],
        ["collection_code", "delivery_code", "delivery_expire_style"],
        "retrieve",
    )


async def test_delivery_view_isolates_codes(tortoise_db):
    """投件码视角：仅 delivery_code，不暴露管理码/取件码及其过期字段。"""
    box = await _make_box()
    resp = await _build_delivery_view_response(box)
    await _check(
        resp,
        ["delivery_code", "delivery_expire_style", "file_count", "total_size", "files"],
        ["collection_code", "retrieve_code", "retrieve_expire_style"],
        "delivery",
    )


async def test_views_share_common_fields(tortoise_db):
    """三视角公共字段一致（标题/容量/整箱过期/文件列表）。"""
    box = await _make_box()
    admin = await _build_collection_response(box)
    retr = await _build_retrieve_response(box)
    deliv = await _build_delivery_view_response(box)
    for k in ["title", "description", "max_files", "expire_style", "expire_value", "expired_at"]:
        assert admin[k] == retr[k] == deliv[k], k
    assert admin["files"][0]["file_name"] == "f.txt"
    assert admin["file_count"] == retr["file_count"] == deliv["file_count"] == 1
