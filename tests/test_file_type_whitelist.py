# 上传文件类型白名单（fileTypeWhitelist）：5 个上传入口全部生效
from core.settings import settings


async def _enable_whitelist(monkeypatch):
    monkeypatch.setattr(settings, "fileTypeWhitelist", "jpg,png,pdf")


async def _create_collection(client) -> str:
    resp = await client.post(
        "/collection/create/",
        json={"title": "t", "max_files": 10, "expire_style": "day", "expire_value": 7},
    )
    assert resp.status_code == 200
    return resp.json()["detail"]["delivery_code"]


async def test_share_file_rejects_disallowed(client, monkeypatch):
    """普通分享：白名单外类型被拒（403），白名单内通过（200）。"""
    await _enable_whitelist(monkeypatch)
    resp = await client.post(
        "/share/file/",
        data={"expire_value": "1", "expire_style": "day"},
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert resp.status_code == 403
    assert "不允许上传" in resp.text

    resp = await client.post(
        "/share/file/",
        data={"expire_value": "1", "expire_style": "day"},
        files={"file": ("img.png", b"png", "image/png")},
    )
    assert resp.status_code == 200


async def test_share_file_multi_rejects(client, monkeypatch):
    """多文件分享：任一文件超白名单即整体拒绝。"""
    await _enable_whitelist(monkeypatch)
    resp = await client.post(
        "/share/file/",
        data={"expire_value": "1", "expire_style": "day"},
        files=[
            ("files", ("a.png", b"png", "image/png")),
            ("files", ("b.exe", b"MZ", "application/octet-stream")),
        ],
    )
    assert resp.status_code == 403


async def test_chunk_init_rejects(client, monkeypatch):
    """分片上传 init：白名单外文件名被拒。"""
    await _enable_whitelist(monkeypatch)
    resp = await client.post(
        "/chunk/upload/init/",
        json={"file_name": "evil.exe", "file_size": 1024, "chunk_size": 1024, "file_hash": "h"},
    )
    assert resp.status_code == 403


async def test_presign_init_rejects(client, monkeypatch):
    """预签名上传 init：白名单外文件名被拒（此前漏校验的入口）。"""
    await _enable_whitelist(monkeypatch)
    resp = await client.post(
        "/presign/upload/init",
        json={"file_name": "evil.exe", "file_size": 1024, "expire_value": 1, "expire_style": "day"},
    )
    assert resp.status_code == 403


async def test_delivery_upload_rejects(client, monkeypatch):
    """投递直传：白名单外文件被拒。"""
    await _enable_whitelist(monkeypatch)
    dcode = await _create_collection(client)
    resp = await client.post(
        "/delivery/upload/",
        data={"delivery_code": dcode, "uploader_name": "t"},
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert resp.status_code == 403


async def test_delivery_chunk_init_rejects(client, monkeypatch):
    """投递分片 init：白名单外文件名被拒。"""
    await _enable_whitelist(monkeypatch)
    dcode = await _create_collection(client)
    resp = await client.post(
        "/delivery/chunk/init/",
        json={
            "delivery_code": dcode, "uploader_name": "t",
            "file_name": "evil.exe", "file_size": 1024,
            "chunk_size": 1024, "file_hash": "h",
        },
    )
    assert resp.status_code == 403


async def test_whitelist_disabled_allows_all(client, monkeypatch):
    """白名单留空（默认）时不限制任何类型。"""
    monkeypatch.setattr(settings, "fileTypeWhitelist", "")
    resp = await client.post(
        "/share/file/",
        data={"expire_value": "1", "expire_style": "day"},
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert resp.status_code == 200
