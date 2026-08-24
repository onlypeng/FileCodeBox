# 投递（delivery）分片上传：init → 上传分片 → complete 合并；cancel 回滚
from apps.base.models import CollectionFile, UploadChunk


async def _create_collection(client) -> str:
    resp = await client.post(
        "/collection/create/",
        json={"title": "t", "max_files": 10, "expire_style": "day", "expire_value": 7},
    )
    assert resp.status_code == 200
    return resp.json()["detail"]["delivery_code"]


async def test_delivery_chunk_full_flow(client):
    """完整流程：init 创建会话与投递记录 → 上传分片 → complete 合并成功。"""
    dcode = await _create_collection(client)

    # init
    resp = await client.post(
        "/delivery/chunk/init/",
        json={
            "delivery_code": dcode, "uploader_name": "tester",
            "file_name": "big.bin", "file_size": 1500,
            "chunk_size": 1024, "file_hash": "h",
        },
    )
    assert resp.status_code == 200
    detail = resp.json()["detail"]
    uid = detail["upload_id"]
    assert detail["total_chunks"] == 2
    assert detail["uploaded_chunks"] == []
    # 投递记录已创建（uploading 状态）
    cf = await CollectionFile.filter(upload_id=uid).first()
    assert cf is not None and cf.status == "uploading"

    # 上传两个分片
    for idx, content in enumerate((b"x" * 1024, b"y" * 476)):
        resp = await client.post(
            f"/chunk/upload/chunk/{uid}/{idx}",
            files={"chunk": (f"c{idx}.bin", content, "application/octet-stream")},
        )
        assert resp.status_code == 200, resp.text

    # complete
    resp = await client.post(
        "/delivery/chunk/complete/",
        data={"delivery_code": dcode, "upload_id": uid, "uploader_name": "tester"},
    )
    assert resp.status_code == 200
    detail = resp.json()["detail"]
    assert detail["status"] == "completed"
    assert detail["filename"] == "big.bin"

    # 投递记录回填 file_code，文件计数 +1
    cf = await CollectionFile.filter(upload_id=uid).first()
    assert cf is not None and cf.file_code_id is not None and cf.status == "completed"
    page = await client.get(f"/delivery/{dcode}")
    assert page.json()["detail"]["file_count"] == 1
    # 分片会话已清理
    assert await UploadChunk.filter(upload_id=uid).count() == 0


async def test_delivery_chunk_incomplete_rejected(client):
    """分片不完整时 complete 返回 400，投递记录保持 uploading。"""
    dcode = await _create_collection(client)
    resp = await client.post(
        "/delivery/chunk/init/",
        json={
            "delivery_code": dcode, "uploader_name": "t",
            "file_name": "a.bin", "file_size": 2000,
            "chunk_size": 1024, "file_hash": "h",
        },
    )
    uid = resp.json()["detail"]["upload_id"]
    # 只传 1 个分片（共 2 个）
    resp = await client.post(
        f"/chunk/upload/chunk/{uid}/0",
        files={"chunk": ("c.bin", b"x" * 1024, "application/octet-stream")},
    )
    assert resp.status_code == 200
    resp = await client.post(
        "/delivery/chunk/complete/",
        data={"delivery_code": dcode, "upload_id": uid, "uploader_name": "t"},
    )
    assert resp.status_code == 400
    assert "分片不完整" in resp.text


async def test_delivery_chunk_cancel_rolls_back(client):
    """cancel 删除投递记录并清理分片会话。"""
    dcode = await _create_collection(client)
    resp = await client.post(
        "/delivery/chunk/init/",
        json={
            "delivery_code": dcode, "uploader_name": "t",
            "file_name": "a.bin", "file_size": 1024,
            "chunk_size": 1024, "file_hash": "h",
        },
    )
    uid = resp.json()["detail"]["upload_id"]
    assert await CollectionFile.filter(upload_id=uid).count() == 1

    resp = await client.post(
        "/delivery/chunk/cancel/",
        data={"delivery_code": dcode, "upload_id": uid},
    )
    assert resp.status_code == 200
    assert await CollectionFile.filter(upload_id=uid).count() == 0
    assert await UploadChunk.filter(upload_id=uid).count() == 0


async def test_delivery_chunk_invalid_code(client):
    """不存在的投件码 init 返回 404。"""
    resp = await client.post(
        "/delivery/chunk/init/",
        json={
            "delivery_code": "NOPE99", "uploader_name": "t",
            "file_name": "a.bin", "file_size": 1024,
            "chunk_size": 1024, "file_hash": "h",
        },
    )
    assert resp.status_code == 404
