# @Time    : 2023/8/14 14:38
# @Author  : Lan
# @File    : views.py
# @Software: PyCharm
import datetime

from fastapi import APIRouter, Depends, HTTPException
from apps.admin.services import FileService, ConfigService
from apps.admin.dependencies import (
    admin_required,
    get_file_service,
    get_config_service,
)
from apps.admin.schemas import IDData, LoginData, UpdateConfigRequest
from core.response import APIResponse
from apps.base.models import FileCodes, KeyValue, CollectionBox, CollectionFile
from tortoise.expressions import Q
from apps.admin.dependencies import create_token
from core.settings import settings
from core.utils import get_now, verify_password

admin_api = APIRouter(
    prefix="/admin", tags=["管理"], dependencies=[Depends(admin_required)]
)


@admin_api.post("/login")
async def login(data: LoginData):
    if not verify_password(data.password, settings.admin_token):
        raise HTTPException(status_code=401, detail="密码错误")

    token = create_token({"is_admin": True})
    return APIResponse(detail={"token": token, "token_type": "Bearer"})


@admin_api.get("/dashboard")
async def dashboard():
    all_codes = await FileCodes.all()
    all_size = str(sum([code.size for code in all_codes]))
    sys_start = await KeyValue.filter(key="sys_start").first()
    now = await get_now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - datetime.timedelta(days=1)
    yesterday_end = today_start - datetime.timedelta(microseconds=1)
    yesterday_codes = FileCodes.filter(
        created_at__gte=yesterday_start, created_at__lte=yesterday_end
    )
    today_codes = FileCodes.filter(created_at__gte=today_start)

    # 收件箱统计
    total_collections = await CollectionBox.all().count()
    active_collections = await CollectionBox.filter(
        Q(expire_style="forever") | Q(expired_at=None) | Q(expired_at__gte=now)
    ).count()

    total_deliveries = await CollectionFile.all().count()
    today_deliveries = await CollectionFile.filter(created_at__gte=today_start).count()
    today_delivery_files = await CollectionFile.filter(
        created_at__gte=today_start
    ).prefetch_related('file_code')
    today_deliveries_size = sum(
        df.file_code.size for df in today_delivery_files if df.file_code
    )

    yesterday_delivery_files = await CollectionFile.filter(
        created_at__gte=yesterday_start, created_at__lte=yesterday_end
    ).prefetch_related('file_code')
    yesterday_deliveries = len(yesterday_delivery_files)
    yesterday_deliveries_size = sum(
        df.file_code.size for df in yesterday_delivery_files if df.file_code
    )

    return APIResponse(
        detail={
            "totalFiles": len(all_codes),
            "storageUsed": all_size,
            "sysUptime": sys_start.value,
            "yesterdayCount": await yesterday_codes.count(),
            "yesterdaySize": str(sum([code.size for code in await yesterday_codes])),
            "todayCount": await today_codes.count(),
            "todaySize": str(sum([code.size for code in await today_codes])),
            "totalCollections": total_collections,
            "activeCollections": active_collections,
            "totalDeliveries": total_deliveries,
            "todayDeliveries": today_deliveries,
            "todayDeliveriesSize": str(today_deliveries_size),
            "yesterdayDeliveries": yesterday_deliveries,
            "yesterdayDeliveriesSize": str(yesterday_deliveries_size),
        }
    )


@admin_api.delete("/file/delete")
async def file_delete(
    data: IDData,
    file_service: FileService = Depends(get_file_service),
):
    await file_service.delete_file(data.id)
    return APIResponse()


@admin_api.get("/file/list")
async def file_list(
    page: int = 1,
    size: int = 10,
    keyword: str = "",
    file_service: FileService = Depends(get_file_service),
):
    files, total = await file_service.list_files(page, size, keyword)
    return APIResponse(
        detail={
            "page": page,
            "size": size,
            "data": files,
            "total": total,
        }
    )


@admin_api.get("/config/get")
async def get_config(
    config_service: ConfigService = Depends(get_config_service),
):
    return APIResponse(detail=config_service.get_config())


@admin_api.patch("/config/update")
async def update_config(
    data: UpdateConfigRequest,
    config_service: ConfigService = Depends(get_config_service),
):
    # 只取非None的字段
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    await config_service.update_config(update_data)
    return APIResponse()


@admin_api.get("/collection/list")
async def collection_list(
    page: int = 1,
    size: int = 10,
    keyword: str = "",
):
    """收件箱列表"""
    offset = (page - 1) * size
    now = await get_now()
    query = CollectionBox.all().order_by("-created_at")
    if keyword:
        query = query.filter(Q(title__icontains=keyword) | Q(collection_code__icontains=keyword) | Q(delivery_code__icontains=keyword) | Q(retrieve_code__icontains=keyword))

    total = await query.count()
    boxes = await query.limit(size).offset(offset)

    result = []
    for box in boxes:
        file_count = await CollectionFile.filter(collection=box).count()
        is_expired = await box.is_expired()
        is_delivery_expired = await box.is_delivery_expired()
        is_retrieve_expired = await box.is_retrieve_expired()
        result.append({
            "id": box.id,
            "collection_code": box.collection_code,
            "delivery_code": box.delivery_code,
            "retrieve_code": box.retrieve_code,
            "title": box.title,
            "description": box.description,
            "max_files": box.max_files,
            "file_count": file_count,
            "is_expired": is_expired,
            "is_delivery_expired": is_delivery_expired,
            "is_retrieve_expired": is_retrieve_expired,
            "expire_style": box.expire_style,
            "expire_value": box.expire_value,
            "expired_at": box.expired_at.isoformat() if box.expired_at else None,
            "delivery_expire_style": box.delivery_expire_style,
            "delivery_expire_value": box.delivery_expire_value,
            "delivery_expired_at": box.delivery_expired_at.isoformat() if box.delivery_expired_at else None,
            "retrieve_expire_style": box.retrieve_expire_style,
            "retrieve_expire_value": box.retrieve_expire_value,
            "retrieve_expired_at": box.retrieve_expired_at.isoformat() if box.retrieve_expired_at else None,
            "created_at": box.created_at.isoformat(),
        })

    return APIResponse(detail={
        "page": page,
        "size": size,
        "data": result,
        "total": total,
    })


@admin_api.get("/collection/{collection_id}/files")
async def collection_files(collection_id: int):
    """获取收件箱中的文件列表"""
    box = await CollectionBox.filter(id=collection_id).first()
    if not box:
        raise HTTPException(status_code=404, detail="收件箱不存在")

    files = await CollectionFile.filter(collection=box).prefetch_related('file_code').order_by("created_at")
    file_list = []
    for f in files:
        file_list.append({
            "id": f.id,
            "file_name": f"{f.file_code.prefix}{f.file_code.suffix}" if f.file_code else "未知",
            "file_size": f.file_code.size if f.file_code else 0,
            "uploader_name": f.uploader_name or "",
            "status": f.status,
            "created_at": f.created_at.isoformat(),
        })

    return APIResponse(detail=file_list)


@admin_api.delete("/collection/{collection_id}")
async def delete_collection(collection_id: int):
    """删除收件箱及其所有文件"""
    box = await CollectionBox.filter(id=collection_id).first()
    if not box:
        raise HTTPException(status_code=404, detail="收件箱不存在")

    # 删除所有关联文件
    collection_files = await CollectionFile.filter(collection=box).all()
    for cf in collection_files:
        if cf.file_code_id:
            file_code = await FileCodes.filter(id=cf.file_code_id).first()
            if file_code:
                from core.storage import storages
                file_storage = storages[settings.file_storage]()
                try:
                    await file_storage.delete_file(file_code)
                except Exception:
                    pass
                await file_code.delete()

    await box.delete()
    return APIResponse(detail="收件箱已删除")
