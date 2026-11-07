"""管理后台路由：全局配置的写入口、全量数据的删除口、在线房间的干预口。

## 问题
这是整个系统里**唯一拥有全局写权限**的入口：能删任意分享（含物理文件）、
能改全局配置、能踢掉正在使用的房间。它的调用者只有一个人，但一次误触
（删错文件、改坏存储配置）就是不可撤销的数据损失。因此这里的路由注解
一律以"权限要求 + 副作用"为主线，而不是复述它查了哪张表。

## 方案
- **鉴权提升到 router 级**：`admin_api` 注册时带 `dependencies=[Depends(admin_required)]`，
  新增路由自动受保护；唯一例外 `/admin/login` 走 `ADMIN_PUBLIC_ENDPOINTS` 白名单。
  因此本文件里的路由**默认都需要管理员 Token**，下文不再逐条重复。
- **写操作留审计**：登录、删文件、改配置调用 `core.audit.record()`。
  该函数吞掉所有异常，审计失败**不会**让主流程失败（上传成功却因审计写不进
  去而报错是不可接受的）。
- **删除前查引用**：秒传/去重让多条记录共享同一物理文件，
  所有删除点都经 `is_file_referenced_elsewhere` 判断，避免把别人的文件一起删掉。
- **影响在线连接的改动主动广播**：删房间、删缓存文件都会 `ws_manager` 广播，
  否则前端会继续显示一个点开就 404 的入口。

## 刻意不做
**不提供"单请求批量删除 / 一键清空"接口**。匿名开放上传的场景下，磁盘清理的
正确做法是"按过期时间清理"（core.tasks 的过期清理任务），而不是给后台一把
能一次删光所有文件的枪——后者一旦点错无法挽回。管理端页面上的"批量"是
并发调用单条删除接口实现的，每条仍各自经过确认弹窗、引用检查与审计留痕。
统计类接口只读，不写任何库表。

所有**写操作**（删除、过期变更、备注/房间设置修改）统一写审计：
普通用户的每次取件/下载都留痕，管理员删整个房间却无痕是不合理的。
审计由 `core.audit.record()` 承载，它吞掉异常，绝不让主流程因写日志失败而失败。
"""
# @Time    : 2023/8/14 14:38
# @Author  : Lan
# @File    : views.py
# @Software: PyCharm
import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
import json as _json
from apps.admin.services import FileService, ConfigService
from apps.admin.dependencies import (
    admin_required,
    get_file_service,
    get_config_service,
)
from apps.admin.schemas import IDData, LoginData, UpdateConfigRequest, ExtendExpireRequest, UpdateFileRequest, UpdateRoomRequest
from core.response import APIResponse
from apps.base.models import FileCodes, FileItem, KeyValue, CollectionBox, CollectionFile, Room, RoomMessage, RoomCachedFile, AuditLog
from apps.base.utils import ip_limit
from tortoise.expressions import Q
from tortoise.functions import Count, Sum
from apps.admin.dependencies import create_token
from core.room_cache import build_download_query, delete_room_file
from core.quota import current_storage_usage
from core.audit import record
from core.netcheck import check_turn_servers
from core.file_dedup import delete_physical_files
from core.settings import settings, DEFAULT_CONFIG, BASE_DIR
from core.utils import get_now, verify_password, pagination
from core.logger import logger
from core.websocket import ws_manager

# 基础设施配置：只在启动期/环境变量里有效，后台提交必须显式拒绝而不是静默丢弃
INFRA_CONFIG_KEYS = {
    "dbType", "dbHost", "dbPort", "dbName", "dbUser", "dbPassword", "dbSchema", "redisUrl",
}
# 保存成功但当前进程不会立即生效的启动期参数
RESTART_CONFIG_KEYS = {"serverWorkers", "serverHost", "serverPort"}

# dependencies 挂在 router 上而不是逐个路由上：新增路由自动继承鉴权，
# 漏挂一处就是未授权访问漏洞（且功能测试不会暴露）。登录接口由
# ADMIN_PUBLIC_ENDPOINTS 白名单豁免，见 apps/admin/dependencies.py。
admin_api = APIRouter(
    prefix="/admin", tags=["管理"], dependencies=[Depends(admin_required)]
)


def _using_default_password() -> bool:
    """是否仍在使用默认管理员口令。

    默认口令是公开常量，仅靠启动日志 warning 不足以阻止它长期留在线上，
    因此登录后返回该标记，前端据此强制弹出改密弹窗；后端中间件也会
    拦截除"改密所必需"之外的后台接口。
    """
    default_token = str(DEFAULT_CONFIG.get("admin_token") or "")
    return bool(default_token) and verify_password(default_token, settings.admin_token)


@admin_api.post("/login")
async def login(data: LoginData, ip: str = Depends(ip_limit["error"])):
    """管理员登录。**本路由是唯一免鉴权的后台接口**（ADMIN_PUBLIC_ENDPOINTS 白名单）。

    副作用：
    - 失败时把该 IP 计入 `error` 限流桶（连续失败会被锁 423），成功**不计数**
    - 成功写审计 `login`、失败写 `login_failed`（结果 denied）

    前提：请求 IP 由限流依赖解析（可信代理后的真实 IP），失败日志用它而不是
    `request.client.host`，否则反代场景下审计里全是网关地址。
    返回 `must_change_password` 供前端强制弹改密弹窗（另见中间件拦截逻辑）。
    """
    if not verify_password(data.password, settings.admin_token):
        await ip_limit["error"].add_ip(ip)
        logger.warning(f"管理后台登录失败: ip={ip}")
        await record("login_failed", target_type="admin", result="denied",
                     detail="密码错误", ip=ip)
        raise HTTPException(status_code=401, detail="密码错误")

    token = create_token({"is_admin": True})
    logger.info(f"管理后台登录成功: ip={ip}")
    await record("login", target_type="admin", result="success", ip=ip, actor="admin")
    return APIResponse(detail={
        "token": token,
        "token_type": "Bearer",
        "must_change_password": _using_default_password(),
    })


@admin_api.get("/verify")
async def verify():
    """校验当前 Token 是否有效（前端路由守卫/页面刷新时调用）。

    只读，无任何副作用（不写审计、不刷新 Token——JWT 无状态，刷新只能重新登录）。
    能走到这里就说明 Token 已被 `admin_required` 校验通过，故 `valid` 恒为 True。
    """
    return APIResponse(detail={
        "valid": True,
        "must_change_password": _using_default_password(),
    })


@admin_api.post("/logout")
async def logout():
    """退出登录。

    后端 Token 采用无状态 JWT（HS256，30 天有效期），服务端不保存会话，
    因此退出仅需客户端丢弃 Token；此处路由为前端显式调用提供一致的语义。

    注意：**本路由不写审计日志，也不做任何服务端失效处理**。
    退出后旧 Token 在过期前理论上仍可用——这是无状态 JWT 的固有取舍，
    真正的即时失效只能轮换 `JWT_SECRET`（见 apps/admin/dependencies）。
    """
    logger.info("管理后台退出登录")
    return APIResponse()


@admin_api.get("/audit")
async def audit_list(
    page: int = 1,
    size: int = 20,
    action: str = "",
    target_id: str = "",
    result: str = "",
):
    """审计日志查询（按时间倒序）。

    合规场景需要回答"某个取件码被谁在何时取走过""管理员何时改过配置"。
    支持按动作 / 对象标识 / 结果过滤。

    权限：管理员。副作用：无（只读）。
    过滤值按字段长度截断而非报错，避免超长输入把查询打崩。
    审计保留期由 `auditLogRetentionDays` 控制（0 = 不清理），
    长期运行的实例务必设一个值，否则 audit_log 表无限增长。
    """
    page, offset = pagination(page, size)

    qs = AuditLog.all()
    if action:
        qs = qs.filter(action=action[:32])
    if target_id:
        qs = qs.filter(target_id=target_id[:64])
    if result:
        qs = qs.filter(result=result[:16])

    total = await qs.count()
    rows = await qs.order_by("-id").offset(offset).limit(size)

    return APIResponse(detail={
        "total": total,
        "page": page,
        "size": size,
        "items": [
            {
                "id": r.id,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "result": r.result,
                "detail": r.detail,
                "ip": r.ip,
                "actor": r.actor,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    })


@admin_api.get("/network/turn-check")
async def turn_check():
    """TURN 连通性自检。

    TURN 配置错误（域名解析失败、端口未放行、协议不匹配）在服务端没有任何征兆，
    只有用户报告"传文件一直卡住"才会被发现。此接口供后台主动探测。

    仅验证端口可达，不验证凭据是否正确——凭据问题需在真实协商中暴露。

    权限：管理员。副作用：**会向外网发起连接探测**，可能耗时数秒（有超时上限）；
    不写库、不改配置。失败统一转 500 并只返回概括文案，
    不把内部异常细节（含 TURN 地址与凭据所在的配置）回给前端。
    """
    try:
        return APIResponse(detail=await check_turn_servers())
    except Exception as e:
        logger.error(f"TURN 连通性自检失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="自检失败，请稍后再试")


@admin_api.get("/dashboard")
async def dashboard():
    """首页概览：文件/收件箱/房间统计 + 存储占用与配额。

    权限：管理员。副作用：无（只读）。
    异常统一转 500 且只返回概括文案——概览页聚合了多张表，
    具体错误可能暴露表结构或部署信息，不应回给前端。
    聚合较重（十几次 COUNT/SUM），故由 `_dashboard_stats` 承载并保持同步执行，
    不要把它挪进请求路径以外的循环里。
    """
    try:
        return await _dashboard_stats()
    except Exception as e:
        logger.error(f"dashboard error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="服务内部错误，请稍后再试")


async def _dashboard_stats():
    """组装概览数据。约定：过期判定必须与模型 `is_expired` 完全一致。

    前提：`KeyValue(key="sys_start")` 记录存在（启动流程写入），
    缺失时本函数会抛异常并由上层转 500。

    时间型/次数型过期在 SQL 里分别统计后相加，而不是把记录读进内存逐个
    `is_expired()`——数据量上来后后者会把概览页拖成全表扫描。
    """
    # 全局存储占用（文件柜累计 + 房间缓存目录）
    storage_usage = await current_storage_usage()
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

    # 在线房间数（含其它实例上的成员；单实例下等价于本地连接表）
    online_rooms = 0
    live_rooms = ws_manager.active_rooms()
    if live_rooms:
        active_room_codes = set(await Room.all().values_list("room_code", flat=True))
        online_rooms = sum(1 for code in live_rooms if code in active_room_codes)

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

    # 秒传/文件柜去重节省：逻辑总量 = 所有分享声明的 size 之和；
    # 物理总量 = 按 (目录, 文件名) 去重后的实际落盘大小之和。多文件分享的
    # FileCodes.uuid_file_name 恒为 None，物理实体在 FileItem 里，两个来源都要统计。
    # 单文件分享同时有 FileCodes 与 FileItem 两条描述，用 seen 去重避免重复计入。
    seen_physical: set = set()
    physical_bytes = 0
    for row in await FileCodes.filter(uuid_file_name__isnull=False).values(
        "file_path", "uuid_file_name", "size"
    ):
        key = (row["file_path"], row["uuid_file_name"])
        if key in seen_physical:
            continue
        seen_physical.add(key)
        physical_bytes += int(row["size"] or 0)
    for row in await FileItem.all().values("storage_path", "uuid_file_name", "file_size"):
        key = (row["storage_path"], row["uuid_file_name"])
        if key in seen_physical:
            continue
        seen_physical.add(key)
        physical_bytes += int(row["file_size"] or 0)
    logical_bytes = int(total_stats.total_size or 0) if total_stats else 0
    dedup_saved_bytes = max(0, logical_bytes - physical_bytes)
    dedup_rate = round(dedup_saved_bytes * 100 / logical_bytes, 1) if logical_bytes else 0.0

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
            # 全局存储占用与配额（匿名开放上传时用于观察磁盘增长）
            "storageUsedBytes": storage_usage["used_bytes"],
            "storageQuotaMB": storage_usage["quota_mb"],
            "storageQuotaPercent": storage_usage["percent"],
            "storageQuotaExceeded": storage_usage["exceeded"],
            "storageBreakdown": storage_usage["breakdown"],
            # 秒传/文件柜去重节省（逻辑总量 vs 去重后物理总量）
            "dedupLogicalBytes": logical_bytes,
            "dedupPhysicalBytes": physical_bytes,
            "dedupSavedBytes": dedup_saved_bytes,
            "dedupRate": dedup_rate,
            "dedupUniqueFiles": len(seen_physical),
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
    """删除一条分享。

    权限：管理员。副作用：
    - 删数据库记录；**物理文件仅在无其它记录引用时删除**（秒传/去重会共享文件）
    - 写审计 `delete`（在 services 层完成）

    刻意不做批量删除：误触的代价不可撤销，磁盘清理应走过期清理任务。
    不存在时由服务层抛 404。
    """
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
    """文件列表（分页 + 关键字 + 有效/过期筛选）。

    权限：管理员。副作用：无（只读）。
    注意列表**排除收件箱投递件**——它们复用同一张表但不是文件分享，
    在这里删掉会连带清空别人的收件箱（收件箱请在收件箱页管理）。
    """
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
    """单文件详情：返回完整文本内容（列表接口不携带，避免大文本拖慢列表）。

    权限：管理员。副作用：无（只读）。
    文本分享的内容本体存在 `remark` 字段里，这里原样回传——
    列表接口刻意裁剪掉它，因为一条文本可达 20 万字符。
    """
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
    """读取系统配置（敏感字段已脱敏为 `******`）。

    权限：管理员。副作用：无（只读）。
    返回的是"默认配置 + 数据库覆盖"的合并结果，且**包含** db*/redisUrl
    这类基础设施配置——它们可查看但不可在此修改（原因见 UpdateConfigRequest）。
    """
    return APIResponse(detail=config_service.get_config())


@admin_api.patch("/config/update")
async def update_config(
    request: Request,
    data: UpdateConfigRequest,
    config_service: ConfigService = Depends(get_config_service),
):
    """更新系统配置（局部更新：只传需要改的字段）。

    权限：管理员。副作用：
    - 写 `KeyValue(key="settings")`，并**立即热生效**（进程内 settings 就地更新，
      IP 限流阈值同步刷新）
    - 写审计 `config_update`；改管理员口令时 target_id 记 `password`
    - 敏感字段加密存储，运行时使用解密后的明文

    存储后端（file_storage）改了不会搬运已有文件：老文件仍在旧后端，
    新文件写新后端。这是刻意行为——静默搬运大流量数据风险更高，
    迁移应由运维显式执行。
    """
    # UpdateConfigRequest 是白名单：未声明的键会被 Pydantic 静默丢弃。
    # 基础设施配置（db*/redisUrl）正是被丢弃的那批，过去 PATCH 返回 200 却什么都没改，
    # 而 config/get 又照样显示它们 —— 形成「保存成功」的假象。这里读原始 body
    # 显式拒绝，让错误在保存时就被看见，而不是上线后排查半天。
    try:
        raw_body = _json.loads(await request.body() or b"{}")
    except Exception:
        raw_body = {}
    # 前端「保存设置」提交的是整份配置，db*/redisUrl 会被原样带回（值与当前一致）。
    # 因此按「值是否真的被改动」判断，而不是「键是否出现」：后者会让后台每一次
    # 保存都 400，形成「点保存没反应」的假生效。空值与脱敏占位符一律视为不修改。
    infra_changed = []
    for key in sorted(set(raw_body) & INFRA_CONFIG_KEYS):
        incoming = raw_body.get(key)
        if incoming is None or str(incoming).strip() in ("", "******"):
            continue
        if str(incoming) != str(getattr(settings, key, None)):
            infra_changed.append(key)
    if infra_changed:
        raise HTTPException(
            status_code=400,
            detail=f"以下配置需通过环境变量设置并重启生效，后台不可修改：{', '.join(infra_changed)}",
        )

    # 记录提示相关字段的旧值：hints 只对「本次真的改了的项」提示，
    # 避免整份表单回传时每次保存都弹「需重启 / 不生效」。
    hint_keys = RESTART_CONFIG_KEYS | {"themesSelect"}
    hint_before = {k: getattr(settings, k, None) for k in hint_keys}

    # 只取非None的字段
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    # 后台「保存设置」是整份表单回传，审计若把 65 个字段全记一遍会淹没真实变更。
    # 先在写库前留一份旧值，写完只记录值确实变化了的字段。
    values_before = {k: getattr(settings, k, None) for k in update_data}
    await config_service.update_config(update_data)
    changed_fields = sorted(
        k for k in update_data
        if str(getattr(settings, k, None)) != str(values_before.get(k))
    )
    # 改管理员密码属于高敏操作，单独留痕（不记录具体口令）
    if "admin_token" in update_data:
        await record("config_update", target_type="admin", target_id="password",
                     result="success", detail="修改管理员密码", actor="admin")
    else:
        # 其余配置只记录**真正变化**的字段名，不记录取值（配置里可能含密钥类字段）
        fields = ",".join(changed_fields)
        if fields:
            await record("config_update", target_type="system",
                         detail=fields[:255], actor="admin")
    # 启动期参数与 dist 部署下的无效项：保存成功也要如实告诉管理员
    # 「不会立刻 / 根本不生效」，不能只回一个空的 200。
    hints: list[str] = []
    restart = sorted(
        k for k in (set(update_data) & RESTART_CONFIG_KEYS)
        if str(update_data[k]) != str(hint_before.get(k))
    )
    if restart:
        hints.append(f"以下配置保存后需重启服务才生效：{', '.join(restart)}")
    themes_changed = (
        "themesSelect" in update_data
        and str(update_data["themesSelect"]) != str(hint_before.get("themesSelect"))
    )
    if themes_changed and (BASE_DIR / "frontend_dist" / "index.html").is_file():
        hints.append(
            "当前使用构建产物(frontend_dist)部署，themesSelect 不参与页面渲染；"
            "请改用「站点默认主题 spaTheme」"
        )
    return APIResponse(detail={"hints": hints} if hints else None)


@admin_api.get("/room/{room_id}/files")
async def admin_room_cache_files(room_id: int):
    """后台查看指定房间的缓存文件（管理员视角：落盘文件与仅 P2P 持有的条目都能看到）。

    权限：管理员。副作用：无（只读）。
    上限 200 条：这是排查/清理用的视图，不做分页——房间缓存条目本身
    受房间配额约束，不该到需要翻页的量级。
    仅落盘条目（`storage_key` 非空）才返回下载 URL；仅 P2P 持有的条目
    服务端没有副本，拿不到 URL 是正常现象而非异常。
    """
    room = await Room.filter(id=room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")

    rows = await RoomCachedFile.filter(room_code=room.room_code).order_by("-id").limit(200)
    files = []
    for c in rows:
        files.append({
            "id": c.id,
            "transfer_id": c.transfer_id,
            "file_name": c.file_name or "",
            "file_size": c.file_size or 0,
            "uploader_name": c.uploader_name or "",
            # 已落盘到服务器磁盘（后台可强制清理以释放空间）
            "server_cached": bool(c.storage_key),
            "expires_at": c.expires_at.isoformat() if c.expires_at else None,
            "cached_at": c.cached_at.isoformat() if c.cached_at else None,
            "download_url": (
                f"/room/{room.room_code}/file/{c.transfer_id}/download?{build_download_query(room.room_code, c.transfer_id)}"
                if c.storage_key else ""
            ),
        })
    return APIResponse(detail={"room_code": room.room_code, "files": files})


@admin_api.delete("/room/cache-file/{file_id}")
async def admin_delete_room_cache_file(file_id: int):
    """后台强制删除任意房间缓存文件。

    房间成员侧的删除接口限制"仅上传者可删"，管理员不受该限制，
    用于清理违规内容或占用空间的落盘文件（磁盘数据一并删除）。

    权限：管理员。副作用（三处，缺一都会留下不一致状态）：
    - 删除磁盘上的缓存文件（仅当 `storage_key` 非空，即确实落过盘）
    - 清空历史里同名文件消息的 `storage_key`：否则历史仍显示"可下载"、点击却 404
    - **向房间内在线成员广播** `room_cache_removed`，让前端立即移除入口

    刻意不做"软删除标记"：缓存文件是临时副本，留着只会占磁盘，
    而房间成员手里的本地副本不受影响。
    写审计 `delete`（target_type=room_cache），与房间成员侧的删除留痕口径一致。
    """
    entry = await RoomCachedFile.filter(id=file_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="文件不存在")

    room_code = entry.room_code
    transfer_id = entry.transfer_id
    if entry.storage_key:
        await delete_room_file(entry.storage_key)
    await entry.delete()
    # 同步清空历史里同名文件消息的落盘标记：否则历史仍显示"可下载"、点击却 404
    await RoomMessage.filter(
        room_code=room_code, msg_type="file", content=transfer_id
    ).update(storage_key="")
    # 通知房间内在线成员移除该条目，避免入口里仍显示已删文件
    await ws_manager.broadcast(room_code, {
        "type": "room_cache_removed",
        "transfer_id": transfer_id,
    })
    await record("delete", target_type="room_cache", target_id=transfer_id or str(file_id),
                 result="success", detail="后台强制清理房间缓存文件", actor="admin")
    return APIResponse()


@admin_api.get("/collection/list")
async def collection_list(
    page: int = 1,
    size: int = 10,
    keyword: str = "",
    status: str = "",
):
    """收件箱列表（分页 + 关键字 + 有效/过期筛选）。

    权限：管理员。副作用：无（只读）。
    关键字同时匹配标题与三码之一：管理员往往只拿到用户报过来的其中一个码，
    要求他先分辨是哪种码不现实。

    注意整箱的"已过期"判定与三码各自过期是**两件事**，
    返回的 `is_expired` / `is_delivery_expired` / `is_retrieve_expired`
    三者独立，前端需分别展示。
    """
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
    """获取收件箱中的投递文件列表（按投递时间正序）。

    权限：管理员。副作用：无（只读）。
    `file_code` 可能为 NULL（投递中/已被删），此时文件名回退为"未知"、大小为 0——
    这是刻意保留的投递痕迹，不要改成跳过这些行，否则创建者会看到文件凭空消失。
    """
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
    """删除收件箱及其所有投递文件。

    权限：管理员。副作用：
    - 逐个删除投递文件对应的物理文件（**先查引用**，被别处共享则只删记录）
    - 级联删除投递记录（CollectionFile）与箱子本身

    写审计 `delete`（target_type=collection），与其它删除接口口径一致。
    单文件删除失败被静默吞掉（`except: pass`）——收件箱删除应尽力完成，
    不该因为某个文件所在存储后端不可用而整体失败，代价是可能残留孤儿文件。
    """
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
                await delete_physical_files(file_storage, file_code, logger)
                await file_code.delete()

    await record("delete", target_type="collection", target_id=box.collection_code,
                 result="success", detail="后台删除收件箱及其投递文件", actor="admin")
    await box.delete()
    return APIResponse(detail="收件箱已删除")


@admin_api.get("/room/list")
async def room_list(
    page: int = 1,
    size: int = 10,
    keyword: str = "",
    status: str = "",
):
    """房间管理列表（聊天/传输/直连合并，按创建时间倒序）。

    权限：管理员。副作用：无（只读）。
    `online_count` 来自**本进程**的 WebSocket 连接表：多实例部署且未配
    `redisUrl` 时，这个数字只反映当前实例上的成员，不是全站在线人数
    （整站的口径见概览 `onlineRooms`，它按房间去重统计）。
    """
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

    # 本页房间的消息数/缓存数一次性聚合，避免逐房间两次 COUNT（一页 10 条原本要 20 次查询）
    room_codes = [r.room_code for r in rooms]
    message_counts: dict = {}
    cached_counts: dict = {}
    if room_codes:
        for row in await RoomMessage.filter(room_code__in=room_codes).group_by("room_code").annotate(cnt=Count("id")):
            message_counts[row.room_code] = row.cnt or 0
        for row in await RoomCachedFile.filter(room_code__in=room_codes).group_by("room_code").annotate(cnt=Count("id")):
            cached_counts[row.room_code] = row.cnt or 0

    result = []
    for room in rooms:
        result.append({
            "id": room.id,
            "room_code": room.room_code,
            "title": room.title,
            "max_members": room.max_members,
            "online_count": ws_manager.room_size(room.room_code),
            "is_expired": await room.is_expired(),
            "expire_style": room.expire_style,
            "expire_value": room.expire_value,
            "expired_at": room.expired_at.isoformat() if room.expired_at else None,
            "created_at": room.created_at.isoformat() if room.created_at else None,
            # 后加功能信息：保存消息 / 文件缓存（含各自记录量，供后台展示）
            "save_messages": bool(room.save_messages),
            "cache_enabled": bool(room.cache_enabled),
            "message_count": message_counts.get(room.room_code, 0),
            "cached_count": cached_counts.get(room.room_code, 0),
        })

    return APIResponse(detail={
        "page": page,
        "size": size,
        "data": result,
        "total": total,
    })


@admin_api.delete("/room/{room_id}")
async def delete_room(room_id: int):
    """删除房间，并踢掉房间内所有在线成员。

    权限：管理员。副作用：
    - **先广播踢人再删记录**：顺序不能反，否则成员会在已消失的房间里继续发消息
    - `kick_room` 会跨实例通知（多实例部署时只关本地连接会留下幽灵连接）
    - 写审计 `delete`（target_type=room），先写后删以便留下房间码

    注意：只删 `Room` 记录。房间消息（RoomMessage）与缓存文件（RoomCachedFile）
    按 `room_code` 关联而非外键，不会级联删除，其磁盘副本依赖缓存自身的过期回收。
    """
    room = await Room.filter(id=room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    # 清理该房间的在线连接（含其它实例上的成员：多实例时只关本地会留下幽灵连接）
    await ws_manager.kick_room(room.room_code, reason="房间已被删除")
    # 审计先于记录删除：target_id 取的是房间码，删完就拿不到了
    await record("delete", target_type="room", target_id=room.room_code,
                 result="success", detail="后台删除房间（含踢出在线成员）", actor="admin")
    await room.delete()
    return APIResponse()


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
    """延长文件分享过期时间（day/hour/minute/count/forever；count 时 value 为剩余次数）。

    权限：管理员。副作用：改库（**在当前过期时间基础上顺延**，见 `_apply_expire`）
    + 写审计 `expire_update`。不影响在线连接。已过期的分享延长后从"现在"起算，不会越延越过去。
    与 `PATCH /file/{id}/expire` 的区别：那是按当前时间**重设**，这是顺延。
    """
    f = await FileCodes.filter(id=data.id).first()
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    if data.expire_style not in ("day", "hour", "minute", "count", "forever"):
        raise HTTPException(status_code=400, detail="不支持的过期类型")
    await _apply_expire(f, data.expire_style, data.expire_value)
    await record("expire_update", target_type="filecode", target_id=f.code,
                 result="success", detail=f"延长文件过期({data.expire_style})", actor="admin")
    return APIResponse()


@admin_api.post("/collection/{collection_id}/extend")
async def collection_extend(collection_id: int, data: ExtendExpireRequest):
    """延长收件箱过期：target=manage（整箱）/ deliver（投递）/ retrieve（取件）。

    权限：管理员。副作用：改库（顺延）+ 写审计 `expire_update`。
    未传 target 时默认整箱。**整箱创建后本不允许改过期**，
    这里的"延长"是在原到期时间上顺延，属于运营干预而非配置修改。
    与重设接口一致：target 非法直接 400，不静默按整箱处理（改错对象不可逆）。
    """
    box = await CollectionBox.filter(id=collection_id).first()
    if not box:
        raise HTTPException(status_code=404, detail="收件箱不存在")
    target = data.target or "manage"
    if target not in ("manage", "deliver", "retrieve"):
        raise HTTPException(status_code=400, detail="不支持的过期目标")
    if target == "deliver":
        prefix = "delivery_"
    elif target == "retrieve":
        prefix = "retrieve_"
    else:
        prefix = ""
    await _apply_expire(box, data.expire_style, data.expire_value, prefix)
    await record("expire_update", target_type="collection", target_id=box.collection_code,
                 result="success", detail=f"延长收件箱过期(target={target})", actor="admin")
    return APIResponse()


@admin_api.post("/room/{room_id}/extend")
async def room_extend(room_id: int, data: ExtendExpireRequest):
    """延长房间过期时间（count 时 value 为剩余会话次数）。

    权限：管理员。副作用：改库（顺延）+ 写审计 `expire_update`。
    **不影响在线连接**：已在线的成员不会被踢，延长也不会重连。
    房间被延长后，正在"房间已过期"提示页上的成员刷新即可恢复。
    """
    room = await Room.filter(id=room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    await _apply_expire(room, data.expire_style, data.expire_value)
    await record("expire_update", target_type="room", target_id=room.room_code,
                 result="success", detail=f"延长房间过期({data.expire_style})", actor="admin")
    return APIResponse()


@admin_api.patch("/file/{file_id}")
async def file_update(file_id: int, data: UpdateFileRequest):
    """文件管理：修改备注（文本分享内容即备注；长度不超过后台 maxTextLength）。

    权限：管理员。副作用：改库 + 写审计 `admin_update`。
    审计只记"改了备注"这一事实，**绝不把备注正文写进日志**——一条文本可达 20 万字符，
    且内容可能含用户隐私。
    """
    f = await FileCodes.filter(id=file_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    if data.remark is not None:
        max_len = getattr(settings, "maxTextLength", 200000)
        if len(data.remark) > max_len:
            raise HTTPException(status_code=400, detail=f"备注过长，最多 {max_len} 字符")
        f.remark = data.remark
        await f.save()
        await record("admin_update", target_type="filecode", target_id=f.code,
                     result="success", detail="后台修改文件备注", actor="admin")
    return APIResponse()


@admin_api.patch("/room/{room_id}")
async def room_update(room_id: int, data: UpdateRoomRequest):
    """房间管理：修改人员上限（1~后台 defaultMaxMembers，不超上限）与功能开关（保存消息/文件缓存）。

    权限：管理员。副作用：改库。**不影响在线连接**——把 `max_members` 调小
    不会踢掉已在线的成员，只阻止后续加入，避免出现"改个数字把正在传文件的人踢下线"。
    `cache_enabled` 关掉不会立即删除已缓存的文件，只是不再新增。
    写审计 `admin_update`（仅在确实有字段变化时）。
    """
    room = await Room.filter(id=room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    changed = False
    if data.max_members is not None:
        default_max = max(1, int(getattr(settings, "defaultMaxMembers", 10) or 10))
        room.max_members = max(1, min(int(data.max_members), default_max))
        changed = True
    if data.save_messages is not None:
        room.save_messages = 1 if int(data.save_messages) else 0
        changed = True
    if data.cache_enabled is not None:
        room.cache_enabled = 1 if int(data.cache_enabled) else 0
        changed = True
    if changed:
        await room.save()
        await record("admin_update", target_type="room", target_id=room.room_code,
                     result="success", detail="后台修改房间设置", actor="admin")
    return APIResponse()


# ==================== 管理弹窗：保存过期时间（按当前时间重设，与创建一致） ====================
@admin_api.patch("/file/{file_id}/expire")
async def file_save_expire(file_id: int, data: ExtendExpireRequest):
    """保存文件过期时间（day/hour/minute/count/forever）——**按当前时间重设**。

    权限：管理员。副作用：改库 + 写审计 `expire_update`。
    与 `POST /file/extend` 的区别：这里是"改成 N 天/次"（重设，见 `_reset_expire`），
    那边是"再加 N 天/次"（顺延）。管理弹窗走重设更符合直觉。
    """
    f = await FileCodes.filter(id=file_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    if data.expire_style not in ("day", "hour", "minute", "count", "forever"):
        raise HTTPException(status_code=400, detail="不支持的过期类型")
    await _reset_expire(f, data.expire_style, data.expire_value)
    await record("expire_update", target_type="filecode", target_id=f.code,
                 result="success", detail=f"重设文件过期({data.expire_style})", actor="admin")
    return APIResponse()


@admin_api.patch("/collection/{collection_id}/expire")
async def collection_save_expire(collection_id: int, data: ExtendExpireRequest):
    """保存收件箱过期（target=manage(整箱)/deliver(投递)/retrieve(取件)，按当前时间重设）。

    权限：管理员。副作用：改库 + 写审计 `expire_update`。
    两个过期接口对 `target` 非法值都直接 400——重设/延长都是不可逆的破坏性操作，
    歧义必须拒绝而不是猜。
    """
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
    await record("expire_update", target_type="collection", target_id=box.collection_code,
                 result="success", detail=f"重设收件箱过期(target={target})", actor="admin")
    return APIResponse()


@admin_api.patch("/room/{room_id}/expire")
async def room_save_expire(room_id: int, data: ExtendExpireRequest):
    """保存房间过期时间（day/hour/minute/count/forever）——按当前时间重设。

    权限：管理员。副作用：改库 + 写审计 `expire_update`。
    """
    room = await Room.filter(id=room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    if data.expire_style not in ("day", "hour", "minute", "count", "forever"):
        raise HTTPException(status_code=400, detail="不支持的过期类型")
    await _reset_expire(room, data.expire_style, data.expire_value)
    await record("expire_update", target_type="room", target_id=room.room_code,
                 result="success", detail=f"重设房间过期({data.expire_style})", actor="admin")
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
