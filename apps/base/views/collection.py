import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from apps.admin.dependencies import share_required_login
from apps.base.models import CollectionBox, CollectionFile, FileCodes
from apps.base.schemas import CreateCollectionRequest, UpdateCollectionConfigRequest
from apps.base.services.expire import validate_expire_config, expire_to_seconds, calculate_expired_at, validate_expire_not_exceed_collection
from apps.base.utils import ip_limit
from core.response import APIResponse
from core.settings import settings
from core.storage import storages, FileStorageInterface, create_zip_stream
from core.utils import get_now, hash_password
from core.websocket import ws_manager
from core.tasks import CollectionEvents

from .share import _generate_code, _validate_code, _get_unique_code, _build_collection_response, _build_retrieve_response

collection_api = APIRouter(prefix="/collection", tags=["收件箱"])


@collection_api.post("/create/", dependencies=[Depends(share_required_login)])
async def create_collection(data: CreateCollectionRequest, ip: str = Depends(ip_limit["upload"])):
    """创建收件箱，返回三码"""
    collection_code = await _get_unique_code(CollectionBox, "collection_code")
    delivery_code = await _get_unique_code(CollectionBox, "delivery_code")
    retrieve_code = await _get_unique_code(CollectionBox, "retrieve_code")
    now = await get_now()

    # 校验最大文件数不超过系统限制
    max_collection_files = getattr(settings, "maxCollectionFiles", 100)
    if data.max_files < 1:
        raise HTTPException(400, "最大文件数不能小于1")
    if data.max_files > max_collection_files:
        raise HTTPException(400, f"最大文件数不能超过{max_collection_files}")

    # 校验过期时间类型
    validate_expire_config(data.expire_style, data.expire_value, label="收件箱过期时间")
    validate_expire_config(data.delivery_expire_style, data.delivery_expire_value, label="投件码过期时间")
    validate_expire_config(data.retrieve_expire_style, data.retrieve_expire_value, label="取件码过期时间")

    # 校验投件码和取件码过期时间不超过收件箱过期时间
    validate_expire_not_exceed_collection(
        data.delivery_expire_style, data.delivery_expire_value,
        data.expire_style, data.expire_value,
        child_label="投件码"
    )
    validate_expire_not_exceed_collection(
        data.retrieve_expire_style, data.retrieve_expire_value,
        data.expire_style, data.expire_value,
        child_label="取件码"
    )

    # 向后兼容：旧字段 expire_days 自动转换
    if data.expire_days is not None and data.expire_style == "day" and data.expire_value == 7:
        expire_value = data.expire_days
        expire_style = "day"
    else:
        expire_value = data.expire_value
        expire_style = data.expire_style

    # 统一计算过期时间
    expired_at = calculate_expired_at(expire_style, expire_value, now)
    delivery_expired_at = calculate_expired_at(data.delivery_expire_style, data.delivery_expire_value, now)
    retrieve_expired_at = calculate_expired_at(data.retrieve_expire_style, data.retrieve_expire_value, now)

    box = await CollectionBox.create(
        collection_code=collection_code,
        delivery_code=delivery_code,
        retrieve_code=retrieve_code,
        title=data.title,
        description=data.description,
        password=data.password if not data.password else hash_password(data.password),
        max_files=data.max_files,
        expire_style=expire_style,
        expire_value=expire_value,
        expired_at=expired_at,
        delivery_expire_style=data.delivery_expire_style,
        delivery_expire_value=data.delivery_expire_value,
        delivery_expired_at=delivery_expired_at,
        retrieve_expire_style=data.retrieve_expire_style,
        retrieve_expire_value=data.retrieve_expire_value,
        retrieve_expired_at=retrieve_expired_at,
        creator_ip=ip,
        creator_name="匿名",
    )

    ip_limit["upload"].add_ip(ip)

    # 触发事件
    await CollectionEvents.emit("collection.created", {
        "collection_code": box.collection_code,
        "delivery_code": box.delivery_code,
        "title": box.title,
        "creator_ip": ip,
    })

    return APIResponse(detail=await _build_collection_response(box, include_files=False))


@collection_api.get("/manage/{collection_code}")
async def get_collection_manage(collection_code: str):
    """收件人管理页面 - 获取投递文件列表"""
    collection_code = _validate_code(collection_code)
    box = await CollectionBox.filter(collection_code=collection_code).first()
    if not box:
        raise HTTPException(404, "收件箱不存在")
    if await box.is_expired():
        raise HTTPException(410, "收件箱已过期")

    return APIResponse(detail=await _build_collection_response(box))


@collection_api.get("/retrieve/{retrieve_code}")
async def get_collection_retrieve(retrieve_code: str):
    """取件码访问 - 只读查看文件列表（不可删除/管理）"""
    retrieve_code = _validate_code(retrieve_code)
    box = await CollectionBox.filter(retrieve_code=retrieve_code).first()
    if not box:
        raise HTTPException(404, "取件码不存在")
    if await box.is_expired():
        raise HTTPException(410, "收件箱已过期")
    if await box.is_retrieve_expired():
        raise HTTPException(410, "取件码已过期")

    return APIResponse(detail=await _build_retrieve_response(box))


@collection_api.get("/manage-by-delivery/{delivery_code}")
async def get_collection_manage_by_delivery(delivery_code: str):
    """通过投件码获取收件箱文件列表（投件码持有者可查看已投递的文件）"""
    delivery_code = _validate_code(delivery_code)
    box = await CollectionBox.filter(delivery_code=delivery_code).first()
    if not box:
        raise HTTPException(404, "投件码不存在")
    if await box.is_expired():
        raise HTTPException(410, "收件箱已过期")
    # 注意：不检查投件码是否过期，已投递的文件应始终可查看

    return APIResponse(detail=await _build_delivery_response(box))


@collection_api.get("/status/{collection_code}")
async def get_collection_status(collection_code: str):
    """获取收件箱状态（同管理页面数据）"""
    return await get_collection_manage(collection_code)


@collection_api.patch("/update/{collection_code}")
async def update_collection_config(collection_code: str, data: UpdateCollectionConfigRequest):
    """更新收件箱配置（收件箱过期时间不可更改，仅投件码/取件码过期时间和最大文件数可修改）"""
    box = await CollectionBox.filter(collection_code=collection_code).first()
    if not box:
        raise HTTPException(404, "收件箱不存在")
    if await box.is_expired():
        raise HTTPException(410, "收件箱已过期")

    update_data = {}

    # 标题与描述（允许更新）
    if data.title is not None:
        title = str(data.title).strip()
        if not title:
            raise HTTPException(400, "收件箱标题不能为空")
        if len(title) > 100:
            raise HTTPException(400, "收件箱标题长度不能超过100个字符")
        update_data["title"] = title
    if data.description is not None:
        description = str(data.description).strip()
        if len(description) > 2000:
            raise HTTPException(400, "收件箱描述长度不能超过2000个字符")
        update_data["description"] = description

    # 投件码有效期（从当前时间重新计算，支持延长已过期的投件码）
    if data.delivery_expire_style is not None and data.delivery_expire_value is not None:
        validate_expire_config(data.delivery_expire_style, data.delivery_expire_value, label="投件码过期时间")
        validate_expire_not_exceed_collection(
            data.delivery_expire_style, data.delivery_expire_value,
            box.expire_style, box.expire_value,
            child_label="投件码"
        )
        update_data["delivery_expire_style"] = data.delivery_expire_style
        update_data["delivery_expire_value"] = data.delivery_expire_value
        update_data["delivery_expired_at"] = calculate_expired_at(
            data.delivery_expire_style, data.delivery_expire_value, box.created_at or await get_now()
        )

    # 取件码有效期（从当前时间重新计算，支持延长已过期的取件码）
    if data.retrieve_expire_style is not None and data.retrieve_expire_value is not None:
        validate_expire_config(data.retrieve_expire_style, data.retrieve_expire_value, label="取件码过期时间")
        validate_expire_not_exceed_collection(
            data.retrieve_expire_style, data.retrieve_expire_value,
            box.expire_style, box.expire_value,
            child_label="取件码"
        )
        update_data["retrieve_expire_style"] = data.retrieve_expire_style
        update_data["retrieve_expire_value"] = data.retrieve_expire_value
        update_data["retrieve_expired_at"] = calculate_expired_at(
            data.retrieve_expire_style, data.retrieve_expire_value, box.created_at or await get_now()
        )

    if data.max_files is not None:
        max_collection_files = getattr(settings, "maxCollectionFiles", 100)
        if data.max_files > max_collection_files:
            raise HTTPException(400, f"最大文件数不能超过{max_collection_files}")
        current_file_count = await CollectionFile.filter(collection=box).count()
        if data.max_files < current_file_count:
            raise HTTPException(400, f"最大文件数不能小于当前已有文件数({current_file_count})")
        update_data["max_files"] = data.max_files

    if update_data:
        await CollectionBox.filter(collection_code=collection_code).update(**update_data)
        await box.refresh_from_db()

    return APIResponse(detail=await _build_collection_response(box))


@collection_api.get("/download/{file_id}")
async def download_collection_file(file_id: int, code: str, ip: str = Depends(ip_limit["error"])):
    """下载收件箱中的文件（需管理码或取件码校验）"""
    collection_file = await CollectionFile.filter(id=file_id).first()
    if not collection_file:
        raise HTTPException(404, "文件不存在")

    if not collection_file.file_code_id:
        raise HTTPException(404, "文件记录不存在")

    # 验证码权限：管理码或取件码
    box = await CollectionBox.filter(id=collection_file.collection_id).first()
    if not box:
        raise HTTPException(404, "收件箱不存在")

    if code != box.collection_code and code != box.retrieve_code:
        ip_limit["error"].add_ip(ip)
        raise HTTPException(403, "码不匹配，无权下载")

    if await box.is_expired():
        raise HTTPException(410, "收件箱已过期")

    # 取件码独立过期检查
    if code == box.retrieve_code and await box.is_retrieve_expired():
        raise HTTPException(410, "取件码已过期")

    file_code = await FileCodes.filter(id=collection_file.file_code_id).first()
    if not file_code:
        raise HTTPException(404, "文件已删除")

    file_storage: FileStorageInterface = storages[settings.file_storage]()
    return await file_storage.get_file_response(file_code)


@collection_api.get("/zip/{code}")
async def download_collection_zip(code: str):
    """打包下载收件箱所有文件（支持管理码和取件码）"""
    box = await CollectionBox.filter(collection_code=code).first()
    if not box:
        # 尝试按取件码查找
        box = await CollectionBox.filter(retrieve_code=code).first()
    if not box:
        raise HTTPException(404, "收件箱不存在")
    if await box.is_expired():
        raise HTTPException(410, "收件箱已过期")

    # 取件码独立过期检查
    if code == box.retrieve_code and await box.is_retrieve_expired():
        raise HTTPException(410, "取件码已过期")

    collection_files = await CollectionFile.filter(collection=box, status="completed").order_by("created_at")
    if not collection_files:
        raise HTTPException(404, "没有文件可下载")

    # 获取所有 FileCode 关联的文件
    file_items = []
    for cf in collection_files:
        if cf.file_code_id:
            fc = await FileCodes.filter(id=cf.file_code_id).first()
            if fc and fc.uuid_file_name and fc.file_path:
                file_items.append(fc)

    if not file_items:
        raise HTTPException(404, "没有文件可下载")

    return StreamingResponse(
        create_zip_stream(file_items),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(code)}.zip"
        },
    )


@collection_api.delete("/delete/{file_id}")
async def delete_collection_file(file_id: int):
    """删除收件箱中的文件（收件人操作）"""
    collection_file = await CollectionFile.filter(id=file_id).first()
    if not collection_file:
        raise HTTPException(404, "文件不存在")

    # 删除关联的 FileCode 和存储文件
    if collection_file.file_code_id:
        file_code = await FileCodes.filter(id=collection_file.file_code_id).first()
        if file_code:
            file_storage: FileStorageInterface = storages[settings.file_storage]()
            try:
                await file_storage.delete_file(file_code)
            except Exception:
                pass
            await file_code.delete()

    box = await CollectionBox.filter(id=collection_file.collection_id).first()
    collection_code = box.collection_code if box else ""
    await collection_file.delete()

    # 广播文件删除
    if collection_code:
        await ws_manager.broadcast(collection_code, {
            "type": "file_deleted",
            "file_id": file_id,
        })

    return APIResponse(detail={"message": "文件已删除"})