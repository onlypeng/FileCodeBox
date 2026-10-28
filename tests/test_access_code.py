# 提取口令（二次口令）：创建时可选设置，取件必须同时提供
from apps.base.models import FileCodes, FileItem
from apps.base.views import _hash_access_code
from core.utils import get_now


async def _create_file(code: str = "ABC123", access_code: str | None = None, count: int = 5):
    return await FileCodes.create(
        code=code,
        prefix="t",
        suffix=".txt",
        uuid_file_name="t.txt",
        file_path="share/data/test",
        size=10,
        expired_at=None,
        expired_count=count,
        expire_style="count" if count >= 0 else "forever",
        expire_value=0,
        owner_token="owner-" + code,
        access_code_hash=_hash_access_code(access_code) if access_code else None,
    )


async def test_select_without_access_code_when_not_required(client):
    """未设口令的分享：行为与改造前一致，只凭取件码即可取件。"""
    await _create_file()
    resp = await client.post("/share/select/", json={"code": "ABC123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["detail"]["name"] == "t.txt"


async def test_select_requires_access_code(client):
    """设了口令但请求未带：拒绝，且**不消耗**取件次数（否则猜错即成拒绝服务）。"""
    await _create_file(access_code="pass2026", count=5)
    resp = await client.post("/share/select/", json={"code": "ABC123"})
    assert resp.status_code == 403
    assert "提取口令" in resp.json()["detail"]
    record = await FileCodes.get(code="ABC123")
    assert record.expired_count == 5
    assert record.used_count == 0


async def test_select_wrong_access_code_does_not_consume(client):
    """口令错误：同样不扣次数，失败计入限流。"""
    await _create_file(access_code="pass2026", count=5)
    resp = await client.post(
        "/share/select/", json={"code": "ABC123", "access_code": "wrong"}
    )
    assert resp.status_code == 403
    record = await FileCodes.get(code="ABC123")
    assert record.expired_count == 5


async def test_select_with_correct_access_code(client):
    """口令正确：正常返回并扣减一次取件额度。"""
    await _create_file(access_code="pass2026", count=5)
    resp = await client.post(
        "/share/select/", json={"code": "ABC123", "access_code": "pass2026"}
    )
    assert resp.status_code == 200
    assert resp.json()["detail"]["name"] == "t.txt"
    record = await FileCodes.get(code="ABC123")
    assert record.expired_count == 4


async def test_access_code_stored_as_hash_not_plaintext(client):
    """库里不能出现明文口令（落库即哈希）。"""
    record = await _create_file(access_code="pass2026")
    stored = record.access_code_hash or ""
    assert stored, "口令应已落库"
    assert "pass2026" not in stored
    assert stored.startswith("pbkdf2$")


async def test_file_info_masks_when_protected(client):
    """未带口令查详情：只回"需要口令"，文件名/大小/哈希不外泄。"""
    await _create_file(access_code="pass2026")
    resp = await client.get("/share/select/info/?code=ABC123")
    assert resp.status_code == 200
    detail = resp.json()["detail"]
    assert detail["require_access_code"] is True
    assert detail["name"] == ""
    assert detail["size"] == 0
    assert detail["file_hash"] in ("", None)


async def test_file_info_owner_bypasses_access_code(client):
    """发起者本人持 owner_token 查详情：不受口令限制（发件记录页要能刷新）。"""
    await _create_file(access_code="pass2026")
    resp = await client.get(
        "/share/select/info/?code=ABC123&owner_token=owner-ABC123"
    )
    detail = resp.json()["detail"]
    assert detail["require_access_code"] is True
    assert detail["name"] == "t.txt"


async def test_file_info_with_correct_access_code(client):
    """接收方带上正确口令：返回完整详情。"""
    await _create_file(access_code="pass2026")
    resp = await client.get("/share/select/info/?code=ABC123&access_code=pass2026")
    detail = resp.json()["detail"]
    assert detail["name"] == "t.txt"


async def test_zip_requires_token_when_protected(client):
    """打包下载入口只认取件码，受保护分享必须带 select token。"""
    file_code = await _create_file(access_code="pass2026")
    await FileItem.create(
        file_code=file_code,
        file_name="t.txt",
        file_size=10,
        storage_path="share/data/test",
        uuid_file_name="t.txt",
        sort_order=0,
    )
    resp = await client.get("/share/zip/ABC123")
    assert resp.status_code == 403

    from core.utils import get_select_token

    # 伪造 token 同样拒绝（有效 token 会进入文件读取，测试库里没有实体文件，不适合在这里断言）
    resp = await client.get("/share/zip/ABC123?key=forged.salt.1")
    assert resp.status_code == 403

    key = await get_select_token("ABC123")
    from core.utils import verify_select_token

    assert await verify_select_token(key, "ABC123") is True


async def test_download_item_requires_token_when_protected(client):
    """单文件下载入口同样受 token 约束。"""
    file_code = await _create_file(access_code="pass2026")
    item = await FileItem.create(
        file_code=file_code,
        file_name="t.txt",
        file_size=10,
        storage_path="share/data/test",
        uuid_file_name="t.txt",
        sort_order=0,
    )
    resp = await client.get(f"/share/download/item/{item.id}?code=ABC123")
    assert resp.status_code == 403

    from core.utils import get_select_token

    key = await get_select_token("ABC123")
    resp = await client.get(f"/share/download/item/{item.id}?code=ABC123&key={key}")
    # token 通过后进入文件读取；文件实体不存在 → 4xx，但不再是 403 鉴权失败
    assert resp.status_code != 403


async def test_upload_with_access_code_end_to_end(client):
    """上传时带口令：记录落哈希，取件需口令。"""
    resp = await client.post(
        "/share/file/",
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"expire_value": "1", "expire_style": "day", "access_code": "pass2026"},
    )
    assert resp.status_code == 200
    code = resp.json()["detail"]["code"]

    record = await FileCodes.get(code=code)
    assert record.access_code_hash and "pass2026" not in record.access_code_hash

    denied = await client.post("/share/select/", json={"code": code})
    assert denied.status_code == 403

    ok = await client.post("/share/select/", json={"code": code, "access_code": "pass2026"})
    assert ok.status_code == 200


async def test_upload_without_access_code_keeps_legacy_behavior(client):
    """不填口令：记录口令字段为空，取件不需要口令。"""
    resp = await client.post(
        "/share/file/",
        files={"file": ("b.txt", b"hello", "text/plain")},
        data={"expire_value": "1", "expire_style": "day"},
    )
    code = resp.json()["detail"]["code"]
    record = await FileCodes.get(code=code)
    assert not record.access_code_hash


async def test_access_code_length_validated(client):
    """过短口令在创建阶段就被拒绝（避免形同虚设）。"""
    resp = await client.post(
        "/share/file/",
        files={"file": ("c.txt", b"hello", "text/plain")},
        data={"expire_value": "1", "expire_style": "day", "access_code": "a"},
    )
    assert resp.status_code == 400
    assert "提取口令" in resp.json()["detail"]
