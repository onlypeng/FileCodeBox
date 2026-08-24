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
from apps.admin.schemas import IDData, LoginData, UpdateConfigRequest, ExtendExpireRequest, UpdateFileRequest, UpdateRoomRequest
from core.response import APIResponse
from apps.base.models import FileCodes, KeyValue, CollectionBox, CollectionFile, Room
from apps.base.utils import ip_limit
from tortoise.expressions import Q
from tortoise.functions import Count, Sum
from apps.admin.dependencies import create_token
from core.settings import settings
from core.utils import get_now, verify_password, pagination
from core.logger import logger
from core.websocket import ws_manager

admin_api = APIRouter(
    prefix="/admin", tags=["管理"], dependencies=[Depends(admin_required)]
)


@admin_api.post("/login")
async def login(data: LoginData, ip: str = Depends(ip_limit["error"])):
    if not verify_password(data.password, settings.admin_token):
        await ip_limit["error"].add_ip(ip)
        logger.warning(f"管理后台登录失败: ip={ip}")
        raise HTTPException(status_code=401, detail="密码错误")

    token = create_token({"is_admin": True})
    logger.info(f"管理后台登录成功: ip={ip}")
    return APIResponse(detail={"token": token, "token_type": "Bearer"})


@admin_api.get("/verify")
async def verify():
    """校验当前 Token 是否有效（前端路由守卫/页面刷新时调用）"""
    return APIResponse(detail={"valid": True})


@admin_api.post("/logout")
async def logout():
    """退出登录。

    后端 Token 采用无状态 JWT（HS256，30 天有效期），服务端不保存会话，
    因此退出仅需客户端丢弃 Token；此处路由为前端显式调用提供一致的语义，
    并记录审计日志。
    """
    logger.info("管理后台退出登录")
    return APIResponse()


@admin_api.get("/dashboard")
async def dashboard():
    try:
        return await _dashboard_stats()
    except Exception as e:
        logger.error(f"dashboard error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="服务内部错误，请稍后再试")


async def _dashboard_stats():
    total_stats = await FileCodes.all().annotate(
        total_count=Count("id"), total_size=Sum("size")
    ).first()
    total_files = total_stats.total_count if total_stats else 0
    total_size = str(total_stats.total_size or 0) if total_stats else "0"
    sys_start = await KeyValue.filter(key="sys_start").first()
    now = await get_now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - datetime.timedelta(days=1)
    yesterday_end = today_start - datetime.timedelta(microseconds=1)
    yesterday_stats = await FileCodes.filter(
        created_at__gte=yesterday_start, created_at__lte=yesterday_end
    ).annotate(total_count=Count("id"), total_size=Sum("size")).first()
    today_stats = await FileCodes.filter(
        created_at__gte=today_start
    ).annotate(total_count=Count("id"), total_size=Sum("size")).first()

    # 收件箱统计
    total_collections = await CollectionBox.all().count()
    active_collections = await CollectionBox.filter(
        Q(expire_style="forever") | Q(expired_at=None) | Q(expired_at__gte=now)
    ).count()

    total_deliveries = await CollectionFile.all().count()
    today_deliveries = await CollectionFile.filter(created_at__gte=today_start).count()
    # 今日/昨日投递总大小：数据库关联聚合，避免全量加载记录到内存再 Python sum
    today_size_agg = await CollectionFile.filter(
        created_at__gte=today_start
    ).annotate(total=Sum("file_code__size")).first()
    today_deliveries_size = today_size_agg.total if today_size_agg and today_size_agg.total else 0

    yesterday_size_agg = await CollectionFile.filter(
        created_at__gte=yesterday_start, created_at__lte=yesterday_end
    ).annotate(total=Sum("file_code__size")).first()
    yesterday_deliveries = await CollectionFile.filter(
        created_at__gte=yesterday_start, created_at__lte=yesterday_end
    ).count()
    yesterday_deliveries_size = yesterday_size_agg.total if yesterday_size_agg and yesterday_size_agg.total else 0

    # 房间统计（统一直连快传房间，仅保留基本统计；聊天消息不落库、无消息统计）
    total_rooms = await Room.all().count()
    active_rooms = await Room.filter(
        Q(expire_style="forever") | Q(expired_at=None) | Q(expired_at__gte=now)
    ).count()
    today_rooms = await Room.filter(created_at__gte=today_start).count()

    # 在线房间数（ws_manager 中存在活跃连接的房间）
    online_rooms = 0
    if ws_manager.active_connections:
        active_room_codes = set(await Room.all().values_list("room_code", flat=True))
        online_rooms = sum(
            1 for code in ws_manager.active_connections if code in active_room_codes
        )

    # 扩展统计（文件健康度）
    # 过期判定与模型 is_expired 保持一致：
    # - 时间型（expired_count < 0）：expired_at 早于当前时间视为过期
    # - 次数型（expired_count >= 0）：次数耗尽（<= 0）视为过期（expired_at 为 NULL，无时间上限）
    time_expired = await FileCodes.filter(
        expired_at__isnull=False, expired_count__lt=0, expired_at__lte=now
    ).count()
    count_expired = await FileCodes.filter(
        expired_count__gte=0, expired_count__lte=0
    ).count()
    expired_count = time_expired + count_expired
    active_count = total_files - expired_count
    text_count = 0  # 文本已合并为文件，不再单独统计
    file_count = total_files - text_count
    chunked_count = await FileCodes.filter(is_chunked=True).count()
    used_total = await FileCodes.all().annotate(total=Sum("used_count")).first()
    used_count = used_total.total if used_total else 0

    # 文件类型分布（Top 后缀）
    suffix_rows = (
        await FileCodes.exclude(suffix="")
        .group_by("suffix")
        .annotate(count=Count("id"))
        .order_by("-count")
        .limit(5)
    )
    top_suffixes = [{"suffix": s.suffix, "count": s.count} for s in suffix_rows]

    # 最近文件
    recent_file_rows = await FileCodes.all().order_by("-created_at").limit(10)
    recent_files = []
    for f in recent_file_rows:
        recent_files.append({
            "id": f.id,
            "code": f.code,
            "name": f"{f.prefix}{f.suffix}",
            "suffix": f.suffix,
            "size": f.size,
            "text": False,  # 文本已合并为文件，不再单独区分
            "expiredAt": f.expired_at.isoformat() if f.expired_at else None,
            "expiredCount": f.expired_count,
            "usedCount": f.used_count,
            "createdAt": f.created_at.isoformat() if f.created_at else None,
            "isExpired": await f.is_expired(),
        })

    # 最近收件箱（annotate 聚合文件数，避免逐箱 count 查询）
    recent_boxes = (
        await CollectionBox.all()
        .annotate(file_count=Count("files"))
        .order_by("-created_at")
        .limit(5)
    )
    recent_collections = []
    for box in recent_boxes:
        recent_collections.append({
            "id": box.id,
            "title": box.title,
            "collection_code": box.collection_code,
            "delivery_code": box.delivery_code,
            "retrieve_code": box.retrieve_code,
            "file_count": box.file_count or 0,
            "max_files": box.max_files,
            "is_expired": await box.is_expired(),
            "created_at": box.created_at.isoformat() if box.created_at else None,
        })

    return APIResponse(
        detail={
            "totalFiles": total_files,
            "storageUsed": total_size,
            "sysUptime": sys_start.value,
            "yesterdayCount": yesterday_stats.total_count if yesterday_stats else 0,
            "yesterdaySize": str(yesterday_stats.total_size or 0) if yesterday_stats else "0",
            "todayCount": today_stats.total_count if today_stats else 0,
            "todaySize": str(today_stats.total_size or 0) if today_stats else "0",
            "totalCollections": total_collections,
            "activeCollections": active_collections,
            "totalDeliveries": total_deliveries,
            "todayDeliveries": today_deliveries,
            "todayDeliveriesSize": str(today_deliveries_size),
            "yesterdayDeliveries": yesterday_deliveries,
            "yesterdayDeliveriesSize": str(yesterday_deliveries_size),
            # 房间统计（基本房间信息）
            "totalRooms": total_rooms,
            "activeRooms": active_rooms,
            "todayRooms": today_rooms,
            "onlineRooms": online_rooms,
            # 扩展统计（文件健康度）
            "activeCount": active_count,
            "expiredCount": expired_count,
            "textCount": text_count,
            "fileCount": file_count,
            "chunkedCount": chunked_count,
            "usedCount": used_count,
            "storageBackend": settings.file_storage,
            "uploadSizeLimit": settings.uploadSize,
            "openUpload": settings.openUpload,
            "enableChunk": settings.enableChunk,
            "maxSaveSeconds": _expired_retention_seconds(),
            "topSuffixes": top_suffixes,
            "recentFiles": recent_files,
            "recentCollections": recent_collections,
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
    status: str = "",
    file_service: FileService = Depends(get_file_service),
):
    files, total = await file_service.list_files(page, size, keyword, status)
    return APIResponse(
        detail={
            "page": page,
            "size": size,
            "data": files,
            "total": total,
        }
    )


@admin_api.get("/file/{file_id}")
async def file_detail(file_id: int):
    """单文件详情：返回完整文本内容（列表接口不携带，避免大文本拖慢列表）"""
    from tortoise.exceptions import DoesNotExist

    try:
        f = await FileCodes.get(id=file_id)
    except DoesNotExist:
        raise HTTPException(status_code=404, detail="文件不存在")
    return APIResponse(
        detail={
            "id": f.id,
            "code": f.code,
            "prefix": f.prefix,
            "remark": f.remark or None,  # 文本分享内容（备注字段）
            "size": f.size,
            "used_count": f.used_count,
            "expired_at": f.expired_at.isoformat() if f.expired_at else None,
            "expired_count": f.expired_count,
            "created_at": f.created_at.isoformat() if f.created_at else None,
            "is_multi_file": f.is_multi_file,
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
    status: str = "",
):
    """收件箱列表"""
    page, offset = pagination(page, size)
    now = await get_now()
    query = CollectionBox.all().order_by("-created_at")
    if keyword:
        query = query.filter(Q(title__icontains=keyword) | Q(collection_code__icontains=keyword) | Q(delivery_code__icontains=keyword) | Q(retrieve_code__icontains=keyword))
    # 状态筛选（多选，逗号分隔；整箱过期判定：forever 永不过期；expired_at 早于现在）
    statuses = [s for s in status.split(",") if s in ("active", "expired")]
    if statuses and "active" not in statuses:
        query = query.filter(~Q(expire_style="forever") & Q(expired_at__lt=now))   # 仅 expired
    elif statuses and "expired" not in statuses:
        query = query.exclude(~Q(expire_style="forever") & Q(expired_at__lt=now))  # 仅 active
    # active+expired 或空 → 不过滤（全部）

    total = await query.count()
    # 使用 annotate 聚合文件数，消除 N+1 查询
    boxes = await query.annotate(file_count=Count("files")).limit(size).offset(offset)

    # 批量获取本页收件箱的全部投递文件（按 collection_id 分组求和取件次数），避免逐箱查询
    box_ids = [b.id for b in boxes]
    used_by_box: dict = {}
    if box_ids:
        cfiles = await CollectionFile.filter(collection_id__in=box_ids).prefetch_related("file_code")
        for cf in cfiles:
            if cf.file_code_id and cf.file_code:
                used_by_box[cf.collection_id] = used_by_box.get(cf.collection_id, 0) + (cf.file_code.used_count or 0)

    result = []
    for box in boxes:
        file_count = box.file_count or 0
        # 收件箱取件次数 = 各投递文件（关联 FileCode）的取件次数总和
        box_used = used_by_box.get(box.id, 0)
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
            "used_count": box_used,
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


@admin_api.get("/room/list")
async def room_list(
    page: int = 1,
    size: int = 10,
    keyword: str = "",
    status: str = "",
):
    """房间管理列表（聊天/传输/直连合并，按创建时间倒序）"""
    page, offset = pagination(page, size)
    keyword = keyword.strip()
    query = Room.all().order_by("-created_at")
    if keyword:
        query = query.filter(
            Q(room_code__icontains=keyword) | Q(title__icontains=keyword)
        )
    # 状态筛选（多选，逗号分隔）：count 模式按剩余次数（expired_count <= 0）；否则按过期时间（forever/未设永不过期）
    statuses = [s for s in status.split(",") if s in ("active", "expired")]
    if statuses:
        now = await get_now()
        count_expired = Q(expire_style="count", expired_count__lte=0)
        time_expired = ~Q(expire_style__in=["count", "forever"]) & Q(expired_at__lt=now)
        expired_q = count_expired | time_expired
        if "active" not in statuses:
            query = query.filter(expired_q)          # 仅 expired
        elif "expired" not in statuses:
            query = query.exclude(expired_q)         # 仅 active
        # active+expired → 不过滤（全部）

    total = await query.count()
    rooms = await query.limit(size).offset(offset)

    result = []
    for room in rooms:
        result.append({
            "id": room.id,
            "room_code": room.room_code,
            "title": room.title,
            "max_members": room.max_members,
            "online_count": len(ws_manager.active_connections.get(room.room_code, [])),
            "is_expired": await room.is_expired(),
            "expire_style": room.expire_style,
            "expire_value": room.expire_value,
            "expired_at": room.expired_at.isoformat() if room.expired_at else None,
            "created_at": room.created_at.isoformat() if room.created_at else None,
        })

    return APIResponse(detail={
        "page": page,
        "size": size,
        "data": result,
        "total": total,
    })


@admin_api.delete("/room/{room_id}")
async def delete_room(room_id: int):
    """删除房间"""
    room = await Room.filter(id=room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    # 清理该房间的在线连接
    for conn in list(ws_manager.active_connections.get(room.room_code, [])):
        try:
            await conn["ws"].close(code=4001, reason="房间已被删除")
        except Exception:
            pass
    ws_manager.active_connections.pop(room.room_code, None)
    ws_manager.file_targets.pop(room.room_code, None)
    await room.delete()


# ==================== 延长过期 ====================
async def _apply_expire(obj, style: str, value: int, prefix: str = ""):
    """延长过期时间：**基于当前过期时间顺延**（而非从当前时刻重新计算）。
    收件箱三码通过 prefix 区分字段；count 模式仅文件/房间有剩余次数（收件箱无次数字段）。
    - day/hour/minute：新过期时间 = max(当前过期时间, 现在) + N 单位（已过期则从现在起算，不会越退）
    - count：剩余次数 += N（当前非次数模式则设 N）
    - forever：清空过期时间/次数，永不过期
    """
    now = await get_now()

    def get_expire(attr):
        return getattr(obj, f"{prefix}{attr}") if prefix else getattr(obj, attr)

    def set_expire(attr, val):
        if prefix:
            setattr(obj, f"{prefix}{attr}", val)
        else:
            setattr(obj, attr, val)

    if style == "forever":
        set_expire("expire_style", "forever")
        set_expire("expire_value", 0)
        set_expire("expired_at", None)
        if not prefix:
            setattr(obj, "expired_count", -1)
    elif style == "count":
        cur = get_expire("expired_count")
        base = cur if (cur is not None and cur >= 0) else 0
        set_expire("expire_style", "count")
        set_expire("expire_value", value)
        # 按次模式：无时间上限（次数用尽前永久有效），is_expired 仅按剩余次数判断
        set_expire("expired_at", None)
        if not prefix:
            setattr(obj, "expired_count", base + value)
    else:
        delta_map = {
            "day": datetime.timedelta(days=value),
            "hour": datetime.timedelta(hours=value),
            "minute": datetime.timedelta(minutes=value),
        }
        delta = delta_map.get(style, datetime.timedelta(days=value))
        # 基于当前过期时间顺延；当前已过期或未设 → 从现在起算
        cur_at = get_expire("expired_at")
        base = cur_at if (cur_at and cur_at > now) else now
        set_expire("expire_style", style)
        set_expire("expire_value", value)
        set_expire("expired_at", base + delta)
        if not prefix:
            setattr(obj, "expired_count", -1)
    await obj.save()


async def _reset_expire(obj, style: str, value: int, prefix: str = ""):
    """重设过期时间：按当前时间重新计算（与创建时一致，get_expire_info 规则）。
    收件箱三码通过 prefix 区分；整箱（prefix=''）创建后不可改由调用方限制。"""
    from apps.base.utils import get_expire_info

    expired_at, expired_count, _, _ = await get_expire_info(value, style)
    if prefix:
        setattr(obj, f"{prefix}expire_style", style)
        setattr(obj, f"{prefix}expire_value", value)
        setattr(obj, f"{prefix}expired_at", expired_at)
    else:
        obj.expire_style = style
        obj.expire_value = value
        obj.expired_at = expired_at
        obj.expired_count = expired_count
    await obj.save()


@admin_api.post("/file/extend")
async def file_extend(data: ExtendExpireRequest, file_service: FileService = Depends(get_file_service)):
    """延长文件分享过期时间（day/hour/minute/count/forever；count 时 value 为剩余次数）"""
    f = await FileCodes.filter(id=data.id).first()
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    if data.expire_style not in ("day", "hour", "minute", "count", "forever"):
        raise HTTPException(status_code=400, detail="不支持的过期类型")
    await _apply_expire(f, data.expire_style, data.expire_value)
    return APIResponse()


@admin_api.post("/collection/{collection_id}/extend")
async def collection_extend(collection_id: int, data: ExtendExpireRequest):
    """延长收件箱过期：target=manage（整箱）/ deliver（投递）/ retrieve（取件）"""
    box = await CollectionBox.filter(id=collection_id).first()
    if not box:
        raise HTTPException(status_code=404, detail="收件箱不存在")
    target = data.target or "manage"
    if target == "deliver":
        prefix = "delivery_"
    elif target == "retrieve":
        prefix = "retrieve_"
    else:
        prefix = ""
    await _apply_expire(box, data.expire_style, data.expire_value, prefix)
    return APIResponse()


@admin_api.post("/room/{room_id}/extend")
async def room_extend(room_id: int, data: ExtendExpireRequest):
    """延长房间过期时间（count 时 value 为剩余会话次数）"""
    room = await Room.filter(id=room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    await _apply_expire(room, data.expire_style, data.expire_value)
    return APIResponse()


@admin_api.patch("/file/{file_id}")
async def file_update(file_id: int, data: UpdateFileRequest):
    """文件管理：修改备注（文本分享内容即备注；长度不超过后台 maxTextLength）"""
    f = await FileCodes.filter(id=file_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    if data.remark is not None:
        max_len = getattr(settings, "maxTextLength", 200000)
        if len(data.remark) > max_len:
            raise HTTPException(status_code=400, detail=f"备注过长，最多 {max_len} 字符")
        f.remark = data.remark
        await f.save()
    return APIResponse()


@admin_api.patch("/room/{room_id}")
async def room_update(room_id: int, data: UpdateRoomRequest):
    """房间管理：修改人员上限（1~后台 defaultMaxMembers，不超上限）"""
    room = await Room.filter(id=room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    if data.max_members is not None:
        default_max = max(1, int(getattr(settings, "defaultMaxMembers", 10) or 10))
        room.max_members = max(1, min(int(data.max_members), default_max))
        await room.save()
    return APIResponse()


# ==================== 管理弹窗：保存过期时间（按当前时间重设，与创建一致） ====================
@admin_api.patch("/file/{file_id}/expire")
async def file_save_expire(file_id: int, data: ExtendExpireRequest):
    """保存文件过期时间（day/hour/minute/count/forever）"""
    f = await FileCodes.filter(id=file_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    if data.expire_style not in ("day", "hour", "minute", "count", "forever"):
        raise HTTPException(status_code=400, detail="不支持的过期类型")
    await _reset_expire(f, data.expire_style, data.expire_value)
    return APIResponse()


@admin_api.patch("/collection/{collection_id}/expire")
async def collection_save_expire(collection_id: int, data: ExtendExpireRequest):
    """保存收件箱过期（target=manage(整箱)/deliver(投递)/retrieve(取件)，按当前时间重设）"""
    box = await CollectionBox.filter(id=collection_id).first()
    if not box:
        raise HTTPException(status_code=404, detail="收件箱不存在")
    target = data.target or "manage"
    if target not in ("manage", "deliver", "retrieve"):
        raise HTTPException(status_code=400, detail="不支持的过期目标")
    if data.expire_style not in ("day", "hour", "minute", "count", "forever"):
        raise HTTPException(status_code=400, detail="不支持的过期类型")
    prefix = {"manage": "", "deliver": "delivery_", "retrieve": "retrieve_"}[target]
    await _reset_expire(box, data.expire_style, data.expire_value, prefix)
    return APIResponse()


@admin_api.patch("/room/{room_id}/expire")
async def room_save_expire(room_id: int, data: ExtendExpireRequest):
    """保存房间过期时间（day/hour/minute/count/forever）"""
    room = await Room.filter(id=room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    if data.expire_style not in ("day", "hour", "minute", "count", "forever"):
        raise HTTPException(status_code=400, detail="不支持的过期类型")
    await _reset_expire(room, data.expire_style, data.expire_value)
    return APIResponse()


def _expired_retention_seconds() -> int:
    """过期保存时间 → 秒（forever/无效配置返回 0 = 不限制）；
    与清理任务 core.tasks 的宽限逻辑保持一致。"""
    style = getattr(settings, "expiredRetentionStyle", "forever")
    value = int(getattr(settings, "expiredRetentionValue", 0) or 0)
    if style == "forever" or value <= 0:
        return 0
    units = {"day": 86400, "hour": 3600, "minute": 60}
    return value * units.get(style, 0)
