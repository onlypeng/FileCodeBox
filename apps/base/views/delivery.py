import datetime
import os
import uuid
from urllib.parse import unquote

from fastapi import APIRouter, Form, UploadFile, File, Depends, HTTPException

from apps.admin.dependencies import share_required_login
from apps.base.models import CollectionBox, CollectionFile, FileCodes
from apps.base.services.expire import validate_expire_config
from apps.base.utils import ip_limit
from core.response import APIResponse
from core.settings import settings
from core.storage import storages, FileStorageInterface
from core.utils import sanitize_filename
from core.websocket import ws_manager
from core.tasks import CollectionEvents

from .share import _generate_code, _validate_code, validate_file_size, _build_collection_response, _build_delivery_response

delivery_api = APIRouter(prefix="/delivery", tags=["投递"])


@delivery_api.get("/{delivery_code}")
async def get_delivery_page(delivery_code: str):
    """获取投递页面信息（仅上传表单，不暴露文件列表）"""
    box = await CollectionBox.filter(delivery_code=delivery_code).first()
    if not box:
        raise HTTPException(404, "投件码不存在")
    if await box.is_expired():
        raise HTTPException(410, "收件箱已过期")
    if await box.is_delivery_expired():
        raise HTTPException(410, "投件码已过期")

    return APIResponse(detail=await _build_delivery_response(box))


@delivery_api.post("/upload/", dependencies=[Depends(share_required_login)])
async def delivery_upload(
    delivery_code: str = Form(...),
    file: UploadFile = File(...),
    uploader_name: str = Form(default=""),
    ip: str = Depends(ip_limit["upload"]),
):
    """投递文件到收件箱，使用 delivery_code 认证"""
    delivery_code = _validate_code(delivery_code)
    box = await CollectionBox.filter(delivery_code=delivery_code).first()
    if not box:
        raise HTTPException(404, "投件码不存在")
    if await box.is_expired():
        raise HTTPException(410, "收件箱已过期")
    if await box.is_delivery_expired():
        raise HTTPException(410, "投件码已过期或投递次数已用完")

    # 校验投递次数限制
    if box.delivery_count > 0 and box.delivery_used_count >= box.delivery_count:
        raise HTTPException(403, "投递次数已用完")

    current_count = await CollectionFile.filter(collection=box).count()
    if current_count >= box.max_files:
        raise HTTPException(403, "收件箱已满")

    file_size = await validate_file_size(file, settings.uploadSize)

    # 创建投递记录（状态：uploading）
    collection_file = await CollectionFile.create(
        collection=box,
        uploader_name=uploader_name,
        uploader_ip=ip,
        status="uploading",
    )

    # 广播上传中状态
    await ws_manager.broadcast(box.collection_code, {
        "type": "file_uploading",
        "file_id": collection_file.id,
        "filename": file.filename or "",
        "progress": 0,
        "uploader": uploader_name or "匿名",
    })

    try:
        # 保存文件并创建 FileCode 记录
        file_storage: FileStorageInterface = storages[settings.file_storage]()
        today = datetime.datetime.now()
        storage_path_prefix = settings.storage_path.strip("/")
        file_uuid = uuid.uuid4().hex
        base_path = f"collection/{box.collection_code}/{today.strftime('%Y/%m/%d')}/{file_uuid}"
        path = f"{storage_path_prefix}/{base_path}" if storage_path_prefix else base_path
        filename = await sanitize_filename(unquote(file.filename or ""))
        save_path = f"{path}/{filename}"

        await file_storage.save_file(file, save_path)

        prefix, suffix = os.path.splitext(filename)
        # 创建关联的 FileCode 记录（用于复用文件存储和下载逻辑）
        file_code = await FileCodes.create(
            code=f"col_{collection_file.id}_{_generate_code()}",
            prefix=prefix,
            suffix=suffix,
            uuid_file_name=filename,
            file_path=path,
            size=file_size,
            expired_at=None,
            expired_count=-1,
            used_count=0,
            expire_style="forever",
            expire_value=0,
        )

        # 更新投递记录
        collection_file.file_code = file_code
        collection_file.status = "completed"
        await collection_file.save()

        # 广播上传完成
        await ws_manager.broadcast(box.collection_code, {
            "type": "file_completed",
            "file_id": collection_file.id,
            "filename": filename,
            "file_size": file_size,
            "uploader": uploader_name or "匿名",
        })

        # 触发事件
        await CollectionEvents.emit("file.delivered", {
            "file_id": collection_file.id,
            "collection_code": box.collection_code,
            "filename": filename,
            "file_size": file_size,
            "uploader": uploader_name or "匿名",
        })

        ip_limit["upload"].add_ip(ip)
        # 投递成功，递增次数
        if box.delivery_count > 0:
            await CollectionBox.filter(id=box.id).update(delivery_used_count=box.delivery_used_count + 1)
        return APIResponse(detail={
            "id": collection_file.id,
            "filename": filename,
            "file_size": file_size,
            "status": "completed",
        })

    except Exception as e:
        # 上传失败
        collection_file.status = "failed"
        await collection_file.save()
        await ws_manager.broadcast(box.collection_code, {
            "type": "file_delivery_failed",
            "file_id": collection_file.id,
            "filename": file.filename or "",
            "error": str(e),
        })
        # 触发事件
        await CollectionEvents.emit("file.delivery_failed", {
            "file_id": collection_file.id,
            "collection_code": box.collection_code,
            "filename": file.filename or "",
            "error": str(e),
        })
        raise HTTPException(500, "文件上传失败，请重试")