import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from collections import deque
from datetime import timedelta
from urllib.parse import quote, unquote

from typing import Optional, Tuple, Union, List

from fastapi import APIRouter, Form, UploadFile, File, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from starlette import status
from tortoise.expressions import F
from tortoise.functions import Sum
from tortoise.transactions import in_transaction

from apps.admin.dependencies import share_required_login
from apps.base.models import (
    FileCodes,
    UploadChunk,
    PresignUploadSession,
    FileItem,
    CollectionBox,
    CollectionFile,
    Room,
    RoomMessage,
    RoomCachedFile,
)
from apps.base.schemas import (
    SelectFileModel,
    RevokeFileModel,
    InitChunkUploadModel,
    CompleteUploadModel,
    PresignUploadInitRequest,
    PresignConfirmRequest,
    CreateCollectionRequest,
    UpdateCollectionConfigRequest,
    CreateRoomRequest,
    InitDeliveryChunkUploadRequest,
)
from apps.base.utils import (
    get_expire_info,
    get_file_path_name,
    ip_limit,
    get_chunk_file_path_name,
)
from core.response import APIResponse
from core.settings import settings, data_root
from core.room_cache import (
    build_download_query,
    build_storage_key,
    delete_room_file,
    iter_file_range,
    move_room_file,
    parse_range_header,
    resolve_path,
    save_room_file,
    verify_download,
)
from core.room_token import issue_member_token, verify_member_token
from core.file_dedup import find_duplicate_file, is_file_referenced_elsewhere
from core.audit import record
from core.storage import storages, FileStorageInterface, create_zip_stream
from core.utils import (
    get_now,
    get_select_token,
    hash_password,
    sanitize_filename,
    verify_password,
    verify_select_token,
)
from core.websocket import ws_manager
from core import cluster
from core.tasks import CollectionEvents

# 必须用 core.logger 那个已配置 handler 的 'FileCodeBox' logger。
# 此前写的是 logging.getLogger(__name__)（即 'apps.base.views'）：它没有任何 handler，
# 级别回退到 root 的 WARNING —— 本文件所有 logger.info（去重命中、缓存清理等）
# 在生产环境被静默丢弃，排查时看不到任何线索。
logger = logging.getLogger("FileCodeBox")

share_api = APIRouter(prefix="/share", tags=["分享"])


# ============ 公共服务层 ============
class FileUploadService:
    """统一的文件上传服务"""

    @staticmethod
    async def generate_file_path(
        file_name: str, upload_id: Optional[str] = None
    ) -> tuple[str, str, str, str, str]:
        """统一的路径生成"""
        today = datetime.datetime.now()
        storage_path = settings.storage_path.strip("/")
        file_uuid = upload_id or uuid.uuid4().hex
        filename = await sanitize_filename(unquote(file_name))
        base_path = f"share/data/{today.strftime('%Y/%m/%d')}/{file_uuid}"
        path = f"{storage_path}/{base_path}" if storage_path else base_path
        prefix, suffix = os.path.splitext(filename)
        save_path = f"{path}/{filename}"
        return path, suffix, prefix, filename, save_path

    @staticmethod
    async def create_file_record(
        file_name: str,
        file_size: int,
        file_path: str,
        expire_value: int,
        expire_style: str,
        **extra_fields,
    ) -> str:
        """统一创建FileCodes记录，返回code"""
        expired_at, expired_count, used_count, code = await get_expire_info(
            expire_value, expire_style
        )
        prefix, suffix = os.path.splitext(file_name)

        await FileCodes.create(
            code=code,
            prefix=prefix,
            suffix=suffix,
            uuid_file_name=file_name,
            file_path=file_path,
            size=file_size,
            expired_at=expired_at,
            expired_count=expired_count,
            used_count=used_count,
            expire_style=expire_style,
            expire_value=expire_value,
            **extra_fields,
        )
        return code


async def validate_file_size(file: UploadFile, max_size: int) -> int:
    size = file.size
    if size is None:
        await file.seek(0, 2)  # type: ignore[arg-type]
        size = file.file.tell()
        await file.seek(0)
    if size > max_size:
        max_size_mb = max_size / (1024 * 1024)
        raise HTTPException(
            status_code=403, detail=f"大小超过限制,最大为{max_size_mb:.2f} MB"
        )
    return size


async def create_file_code(code, **kwargs):
    return await FileCodes.create(code=code, **kwargs)


@share_api.post("/file/", dependencies=[Depends(share_required_login)])
async def share_file(
    expire_value: int = Form(default=1, gt=0),
    expire_style: str = Form(default="day"),
    remark: str = Form(default=""),
    access_code: str = Form(default=""),
    file: Optional[UploadFile] = File(None),
    files: List[UploadFile] = File(default=[]),
    ip: str = Depends(ip_limit["upload"]),
):
    if expire_style not in settings.expireStyle:
        raise HTTPException(status_code=400, detail="过期时间类型错误")

    # 校验文件备注长度（与文本分享 maxTextLength 一致）
    _validate_remark(remark)

    # 可选二次口令：非空则落哈希，取件时必须同时提供
    access_code_hash = _hash_access_code(access_code)

    file_storage: FileStorageInterface = storages[settings.file_storage]()

    # 兼容单文件模式
    upload_files = list(files) if files else []
    if file and not upload_files:
        upload_files = [file]

    # 兼容"仅备注"分享：无文件但有备注时，创建仅备注记录（文本内容存 remark，无文件路径/文件项）
    if not upload_files and remark:
        expired_at, expired_count, used_count, code = await get_expire_info(
            expire_value, expire_style
        )
        owner_token = _new_owner_token()
        file_code = await FileCodes.create(
            code=code,
            prefix="",
            suffix="",
            size=len(remark),
            remark=remark,
            expired_at=expired_at,
            expired_count=expired_count,
            used_count=used_count,
            is_multi_file=False,
            expire_style=expire_style,
            expire_value=expire_value,
            owner_token=owner_token,
            access_code_hash=access_code_hash,
        )
        await ip_limit["upload"].add_ip(ip)
        return APIResponse(detail={"code": code, "owner_token": owner_token})

    if not upload_files:
        raise HTTPException(status_code=400, detail="未选择文件")

    # 多文件数量限制
    max_count = getattr(settings, "maxSendFiles", 20)
    if len(upload_files) > max_count:
        raise HTTPException(status_code=400, detail=f"文件数量超过限制，最多{max_count}个")

    expired_at, expired_count, used_count, code = await get_expire_info(
        expire_value, expire_style
    )

    # 单文件模式（向后兼容）
    if len(upload_files) == 1:
        f = upload_files[0]
        _validate_file_type(f.filename or "")
        file_size = await validate_file_size(f, settings.uploadSize)
        path, suffix, prefix, uuid_file_name, save_path = await get_file_path_name(f)
        await file_storage.save_file(f, save_path)
        # 落盘后过安全扫描（未启用则直接返回），命中则拒绝并删除已落盘文件
        await _scan_guard(save_path)
        # 落盘后按实际内容算哈希：接收端据此校验完整性，秒传也复用它。
        # 与分片上传一致，哈希一律由服务端算，不采信客户端声明值。
        file_hash = await _sha256_saved_file(save_path)
        owner_token = _new_owner_token()
        file_code = await FileCodes.create(
            code=code,
            prefix=prefix,
            suffix=suffix,
            uuid_file_name=uuid_file_name,
            file_path=path,
            size=file_size,
            file_hash=file_hash,
            remark=remark or None,
            expired_at=expired_at,
            expired_count=expired_count,
            used_count=used_count,
            is_multi_file=False,
            expire_style=expire_style,
            expire_value=expire_value,
            owner_token=owner_token,
            access_code_hash=access_code_hash,
        )
        # 为单文件也创建 FileItem 记录
        await FileItem.create(
            file_code=file_code,
            file_name=f"{prefix}{suffix}",
            file_size=file_size,
            storage_path=path,
            uuid_file_name=uuid_file_name,
            sort_order=0,
        )
        await ip_limit["upload"].add_ip(ip)
        await record("upload", target_type="filecode", target_id=code,
                     result="success", detail=f.filename, ip=ip)
        return APIResponse(detail={"code": code, "name": f.filename, "owner_token": owner_token})

    # 多文件模式
    total_size = 0
    file_items_data = []
    today = datetime.datetime.now()
    storage_path_prefix = settings.storage_path.strip("/")
    file_uuid = uuid.uuid4().hex
    base_path = f"share/data/{today.strftime('%Y/%m/%d')}/{file_uuid}"
    path = f"{storage_path_prefix}/{base_path}" if storage_path_prefix else base_path

    # 先验证所有文件大小与类型
    validated_files = []
    for idx, f in enumerate(upload_files):
        _validate_file_type(f.filename or "")
        file_size = await validate_file_size(f, settings.uploadSize)
        total_size += file_size
        if total_size > settings.uploadSize:
            raise HTTPException(status_code=403, detail="文件总大小超过限制")
        filename = await sanitize_filename(unquote(f.filename or ""))
        prefix, suffix = os.path.splitext(filename)
        save_path = f"{path}/{filename}"
        validated_files.append((f, file_size, filename, prefix, suffix, save_path, idx))

    # 验证通过后再保存文件
    saved_paths = []

    async def _rollback_saved() -> None:
        """回滚本次已保存的文件（保存失败或被安全扫描拦截时）。"""
        for sp in saved_paths:
            try:
                temp_code = FileCodes(file_path=path, uuid_file_name=os.path.basename(sp))
                await file_storage.delete_file(temp_code)
            except Exception:
                pass

    try:
        for f, file_size, filename, prefix, suffix, save_path, idx in validated_files:
            await file_storage.save_file(f, save_path)
            saved_paths.append(save_path)
            # 逐个过安全扫描（未启用则直接返回）。命中会抛 403，
            # 由下面 except HTTPException 原样透传并回滚，避免被误转成 500
            await _scan_guard(save_path)
            file_items_data.append({
                "file_name": f"{prefix}{suffix}",
                "file_size": file_size,
                "storage_path": path,
                "uuid_file_name": filename,
                "sort_order": idx,
            })
    except HTTPException:
        # 扫描拦截等带明确状态码的失败：原样抛出（不要吞成 500）
        await _rollback_saved()
        raise
    except Exception as e:
        await _rollback_saved()
        raise HTTPException(status_code=500, detail="文件保存失败，请重试")

    owner_token = _new_owner_token()
    file_code = await FileCodes.create(
        code=code,
        prefix="",
        suffix="",
        uuid_file_name=None,
        file_path=path,
        size=total_size,
        remark=remark or None,
        expired_at=expired_at,
        expired_count=expired_count,
        used_count=used_count,
        is_multi_file=True,
        expire_style=expire_style,
        expire_value=expire_value,
        owner_token=owner_token,
        access_code_hash=access_code_hash,
    )

    for item_data in file_items_data:
        await FileItem.create(file_code=file_code, **item_data)

    await ip_limit["upload"].add_ip(ip)
    await record("upload", target_type="filecode", target_id=code,
                 result="success", detail=f"{len(upload_files)}个文件", ip=ip)
    return APIResponse(detail={
        "code": code,
        "name": f"{len(upload_files)}个文件",
        "is_multi_file": True,
        "owner_token": owner_token,
    })


async def get_code_file_by_code(
    code: str, check: bool = True
) -> Tuple[bool, Union[FileCodes, str]]:
    file_code = await FileCodes.filter(code=code).first()
    if not file_code:
        return False, "文件不存在"
    if await file_code.is_expired() and check:
        return False, "文件已过期"
    return True, file_code


async def update_file_usage(file_code: FileCodes) -> bool:
    """原子消费一次取件额度（并发安全）。

    count 模式：用带 expired_count__gt=0 条件的 F 表达式扣减——多个并发请求同时到达时，
    只有额度未尽的请求能扣减成功（updated > 0），扣减失败的请求判定为已过期，
    避免"检查时未过期、扣减时已耗尽"的 check-then-act 竞态（此前并发取件可突破次数上限）。
    时间/永久模式：仅累加 used_count，恒返回 True（时间过期由 is_expired 单独检查）。
    """
    if file_code.expired_count > 0:
        updated = await FileCodes.filter(id=file_code.id, expired_count__gt=0).update(
            expired_count=F("expired_count") - 1,
            used_count=F("used_count") + 1,
        )
        return updated > 0
    await FileCodes.filter(id=file_code.id).update(
        used_count=F("used_count") + 1
    )
    return True


@share_api.get("/check_code/")
async def check_code_type(code: str, ip: str = Depends(ip_limit["error"])):
    """检查码类型：file / collection / delivery / retrieve / unknown"""
    code = _validate_code(code)
    # 先检查是否为普通文件码
    has, file_code = await get_code_file_by_code(code)
    if has:
        return APIResponse(detail={"type": "file", "code": code})

    # 检查是否为收件箱管理码
    box = await CollectionBox.filter(collection_code=code).first()
    if box:
        if await box.is_expired():
            return APIResponse(detail={"type": "collection", "code": code, "expired": True, "title": box.title})
        return APIResponse(detail={"type": "collection", "code": code, "title": box.title})

    # 检查是否为取件码
    box = await CollectionBox.filter(retrieve_code=code).first()
    if box:
        if await box.is_expired() or await box.is_retrieve_expired():
            return APIResponse(detail={"type": "retrieve", "code": code, "expired": True, "title": box.title})
        return APIResponse(detail={"type": "retrieve", "code": code, "title": box.title})

    # 检查是否为投递码
    box = await CollectionBox.filter(delivery_code=code).first()
    if box:
        if await box.is_expired() or await box.is_delivery_expired():
            return APIResponse(detail={"type": "delivery", "code": code, "expired": True, "title": box.title})
        return APIResponse(detail={"type": "delivery", "code": code, "title": box.title})

    # 检查是否为实时房间码（统一为直连快传房间）
    room = await Room.filter(room_code=code).first()
    if room:
        if await room.is_expired():
            return APIResponse(detail={"type": "direct", "code": code, "expired": True, "title": room.title})
        return APIResponse(detail={"type": "direct", "code": code, "title": room.title})

    # 未命中任何码：计入失败限流，防枚举
    await ip_limit["error"].add_ip(ip)
    return APIResponse(code=404, detail={"type": "unknown", "code": code})


@share_api.get("/select/")
async def get_code_file(code: str, access_code: str = "", ip: str = Depends(ip_limit["error"])):
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    has, file_code = await get_code_file_by_code(code)
    if not has:
        await ip_limit["error"].add_ip(ip)
        return APIResponse(code=404, detail=file_code)

    assert isinstance(file_code, FileCodes)

    # 二次口令：未通过则不进入任何"消耗次数/返回内容"的分支
    await _require_access_code(file_code, access_code, ip)
    protected = bool(file_code.access_code_hash or "")
    access_key = await get_select_token(file_code.code) if protected else ""

    # 多文件模式：返回文件列表
    if file_code.is_multi_file:
        if not await update_file_usage(file_code):
            return APIResponse(code=410, detail="文件已过期")
        items = await FileItem.filter(file_code=file_code).order_by("sort_order")
        return APIResponse(detail={
            "code": file_code.code,
            "name": f"{len(items)}个文件",
            "size": file_code.size,
            "remark": file_code.remark or None,
            "is_multi_file": True,
            "access_key": access_key,
            "items": [
                {
                    "id": item.id,
                    "file_name": item.file_name,
                    "file_size": item.file_size,
                }
                for item in items
            ],
        })

    if not await update_file_usage(file_code):
        return APIResponse(code=410, detail="文件已过期")
    return await file_storage.get_file_response(file_code)


@share_api.get("/select/info/")
async def get_file_info(
    code: str,
    access_code: str = "",
    owner_token: str = "",
    ip: str = Depends(ip_limit["error"]),
):
    """只读查询文件分享详情（不消耗取件次数）：
    返回与 select_file 相同结构 + expired_count（实时剩余次数），供记录查看刷新；
    已过期返回 410，不存在返回 404。

    设了二次口令的分享：只有发起者本人（持 owner_token）或提供正确口令时才回详情，
    否则只回"需要口令"与过期状态 —— 否则知道取件码就能把文件名/大小探出来。
    """
    code = _validate_code(code)
    has, file_code = await get_code_file_by_code(code)
    if not has:
        await ip_limit["error"].add_ip(ip)
        return APIResponse(code=404, detail=file_code)

    assert isinstance(file_code, FileCodes)

    protected = bool(file_code.access_code_hash or "")
    is_owner = bool(
        protected
        and owner_token
        and file_code.owner_token
        and hmac.compare_digest(owner_token, file_code.owner_token)
    )
    access_ok = (
        not protected
        or is_owner
        or await _verify_access_code(file_code, access_code)
    )

    # 次数模式次数用尽 / 时间型已过期
    if file_code.expired_count == 0 or (
        file_code.expired_count < 0
        and file_code.expired_at is not None
        and file_code.expired_at < await get_now()
    ):
        return APIResponse(code=410, detail="文件已过期")

    if not access_ok:
        # 只回"需要口令"与过期状态：文件名、大小、哈希、子项一律不下发
        return APIResponse(
            detail={
                "code": file_code.code,
                "name": "",
                "size": 0,
                "text": "",
                "remark": None,
                "require_access_code": True,
                "file_hash": "",
                "created_at": file_code.created_at.isoformat() if file_code.created_at else None,
                "expired_at": file_code.expired_at.isoformat() if file_code.expired_at else None,
                "expire_style": file_code.expire_style,
                "expire_value": file_code.expire_value,
                "expired_count": file_code.expired_count,
                "is_multi_file": False,
                "items": [],
            }
        )

    if file_code.is_multi_file:
        items = await FileItem.filter(file_code=file_code).order_by("sort_order")
        return APIResponse(
            detail={
                "code": file_code.code,
                "name": f"{len(items)}个文件",
                "size": file_code.size,
                "text": "",
                "remark": file_code.remark or None,
                "require_access_code": protected,
                "is_multi_file": True,
                "created_at": file_code.created_at.isoformat() if file_code.created_at else None,
                "expired_at": file_code.expired_at.isoformat() if file_code.expired_at else None,
                "expire_style": file_code.expire_style,
                "expire_value": file_code.expire_value,
                "expired_count": file_code.expired_count,
                "items": [
                    {
                        "id": item.id,
                        "file_name": item.file_name,
                        "file_size": item.file_size,
                    }
                    for item in items
                ],
            }
        )

    file_storage: FileStorageInterface = storages[settings.file_storage]()
    text_content = file_code.remark if (not file_code.file_path and file_code.remark) else await file_storage.get_file_url(file_code)
    return APIResponse(
        detail={
            "code": file_code.code,
            "name": file_code.prefix + file_code.suffix,
            "size": file_code.size,
            "text": text_content,
            "remark": file_code.remark or None,
            "require_access_code": protected,
            # 与 select 一致：下发权威哈希供接收端校验（多文件 zip 不下发）
            "file_hash": file_code.file_hash or "",
            "created_at": file_code.created_at.isoformat() if file_code.created_at else None,
            "expired_at": file_code.expired_at.isoformat() if file_code.expired_at else None,
            "expire_style": file_code.expire_style,
            "expire_value": file_code.expire_value,
            "expired_count": file_code.expired_count,
        }
    )


@share_api.post("/select/")
async def select_file(data: SelectFileModel, ip: str = Depends(ip_limit["error"])):
    data.code = _validate_code(data.code)
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    has, file_code = await get_code_file_by_code(data.code)
    if not has:
        await ip_limit["error"].add_ip(ip)
        return APIResponse(code=404, detail=file_code)

    assert isinstance(file_code, FileCodes)

    # 二次口令：口令未通过时不扣取件次数、也不下发任何文件信息
    await _require_access_code(file_code, data.access_code, ip)
    protected = bool(file_code.access_code_hash or "")
    # 多文件下载走 /download/item 与 /zip，这两个入口只认取件码；
    # 口令通过后下发短期 token，前端带上它才允许下载（否则知道码即可绕过口令）
    access_key = await get_select_token(file_code.code) if protected else ""

    # 多文件模式：返回文件列表信息
    if file_code.is_multi_file:
        if not await update_file_usage(file_code):
            return APIResponse(code=410, detail="文件已过期")
        await record("retrieve", target_type="filecode", target_id=data.code,
                     detail=f"多文件共 {file_code.size} 字节", ip=ip)
        items = await FileItem.filter(file_code=file_code).order_by("sort_order")
        return APIResponse(
            detail={
                "code": file_code.code,
                "name": f"{len(items)}个文件",
                "size": file_code.size,
                "text": "",
                "remark": file_code.remark or None,
                "is_multi_file": True,
                "access_key": access_key,
                "created_at": file_code.created_at.isoformat() if file_code.created_at else None,
                "expired_at": file_code.expired_at.isoformat() if file_code.expired_at else None,
                "expire_style": file_code.expire_style,
                "expire_value": file_code.expire_value,
                "expired_count": file_code.expired_count,
                "items": [
                    {
                        "id": item.id,
                        "file_name": item.file_name,
                        "file_size": item.file_size,
                    }
                    for item in items
                ],
            }
        )

    if not await update_file_usage(file_code):
        return APIResponse(code=410, detail="文件已过期")
    await record("retrieve", target_type="filecode", target_id=data.code,
                 detail=file_code.prefix + file_code.suffix, ip=ip)
    # 文本分享（内容在 remark，无文件路径）→ text 返回 remark 内容；否则返回文件下载 URL
    text_content = file_code.remark if (not file_code.file_path and file_code.remark) else await file_storage.get_file_url(file_code)
    return APIResponse(
        detail={
            "code": file_code.code,
            "name": file_code.prefix + file_code.suffix,
            "size": file_code.size,
            "text": text_content,
            "remark": file_code.remark or None,
            # 服务端落盘时流式算出的权威哈希，供接收端下载后校验完整性。
            # 多文件分支不下发：打包 zip 的内容哈希不固定，无法作为校验基准。
            "file_hash": file_code.file_hash or "",
            "created_at": file_code.created_at.isoformat() if file_code.created_at else None,
            "expired_at": file_code.expired_at.isoformat() if file_code.expired_at else None,
            "expire_style": file_code.expire_style,
            "expire_value": file_code.expire_value,
            "expired_count": file_code.expired_count,
        }
    )


@share_api.post("/revoke")
async def revoke_file(data: RevokeFileModel, ip: str = Depends(ip_limit["error"])):
    """提前作废自己发起的分享（"我的分享"里的撤销）。

    无账号体系，凭证就是上传时下发的 owner_token：仅知道取件码的人无法撤销。
    删除前检查物理文件是否仍被其它记录引用（秒传会让多条记录共享同一份文件）。
    """
    code = _validate_code(data.code)
    file_code = await FileCodes.filter(code=code).first()
    if not file_code:
        raise HTTPException(status_code=404, detail="文件不存在")

    token = (data.owner_token or "").strip()
    stored = file_code.owner_token or ""
    # 常量时间比较，避免通过响应时间逐字节猜凭证
    if not stored or not token or not hmac.compare_digest(stored, token):
        await ip_limit["error"].add_ip(ip)
        await record("revoke", target_type="filecode", target_id=code,
                     result="denied", detail="发起者凭证不匹配", ip=ip)
        raise HTTPException(status_code=403, detail="凭证无效，无法撤销该分享")

    file_storage: FileStorageInterface = storages[settings.file_storage]()
    if not await is_file_referenced_elsewhere(file_code.file_path, file_code.uuid_file_name, file_code.id):
        try:
            await file_storage.delete_file(file_code)
        except Exception as e:
            logger.error(f"撤销分享时删除文件失败 code={code}: {e}")
    else:
        logger.info(f"撤销分享：文件仍被其它记录引用，仅删除记录 code={code}")

    await FileItem.filter(file_code=file_code).delete()
    await file_code.delete()

    await record("revoke", target_type="filecode", target_id=code,
                 result="success", detail=file_code.prefix + file_code.suffix, ip=ip)
    return APIResponse()


@share_api.get("/download")
async def download_file(key: str, code: str, ip: str = Depends(ip_limit["error"])):
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    if not await verify_select_token(key, code):
        await ip_limit["error"].add_ip(ip)
        await record("download", target_type="filecode", target_id=code,
                     result="denied", detail="下载鉴权失败", ip=ip)
        raise HTTPException(status_code=403, detail="下载鉴权失败")
    # 取件次数（count 模式）在 select 阶段已扣减，此处不重复扣减；
    # token 自带有效期（SELECT_TOKEN_TTL），超窗后需重新取件。故此处仅校验：
    # - 次数型（count）：次数已用尽（expired_count <= 0）即使 token 未过期也拒绝
    # - 时间型（day/hour/minute）：expired_at 到期拒绝
    has, file_code = await get_code_file_by_code(code, False)
    if not has:
        return APIResponse(code=404, detail="文件不存在")
    assert isinstance(file_code, FileCodes)
    if file_code.expired_count >= 0 and file_code.expired_count <= 0:
        raise HTTPException(status_code=410, detail="文件已过期")
    if (
        file_code.expired_at is not None
        and file_code.expired_count < 0
        and file_code.expired_at < await get_now()
    ):
        raise HTTPException(status_code=410, detail="文件已过期")
    # 文本分享（内容在 remark，无文件路径）→ 直接返回文本内容；否则走文件响应
    if not file_code.file_path and file_code.remark:
        from fastapi.responses import PlainTextResponse
        await record("download", target_type="filecode", target_id=code,
                     detail="文本内容", ip=ip)
        return PlainTextResponse(file_code.remark, media_type="text/plain; charset=utf-8")
    await record("download", target_type="filecode", target_id=code,
                 detail=file_code.prefix + file_code.suffix, ip=ip)
    return await file_storage.get_file_response(file_code)


chunk_api = APIRouter(prefix="/chunk", tags=["切片"])


@chunk_api.post("/upload/init/", dependencies=[Depends(share_required_login)])
async def init_chunk_upload(data: InitChunkUploadModel, ip: str = Depends(ip_limit["upload"])):
    _validate_file_type(data.file_name)
    # 限制未完成会话总数，防止资源耗尽（对比 presign 的 50 个上限）
    total_active = await UploadChunk.filter(chunk_index=-1).count()
    if total_active > 100:
        raise HTTPException(429, "正在进行中的上传过多，请稍后再试")

    # 服务端校验：根据 total_chunks * chunk_size 计算理论最大上传量
    total_chunks = (data.file_size + data.chunk_size - 1) // data.chunk_size
    max_possible_size = total_chunks * data.chunk_size
    if max_possible_size > settings.uploadSize:
        max_size_mb = settings.uploadSize / (1024 * 1024)
        raise HTTPException(
            status_code=403, detail=f"文件大小超过限制，最大为 {max_size_mb:.2f} MB"
        )

    # 断点续传：检查是否存在相同文件的未完成上传会话
    existing_session = await UploadChunk.filter(
        chunk_hash=data.file_hash,
        chunk_index=-1,
        file_size=data.file_size,
        file_name=data.file_name,
    ).first()

    if existing_session:
        if not existing_session.save_path:
            await UploadChunk.filter(upload_id=existing_session.upload_id).delete()
        else:
            uploaded_chunks = await UploadChunk.filter(
                upload_id=existing_session.upload_id, completed=True
            ).values_list("chunk_index", flat=True)
            await ip_limit["upload"].add_ip(ip)
            return APIResponse(
                detail={
                    "existed": False,
                    "upload_id": existing_session.upload_id,
                    "chunk_size": existing_session.chunk_size,
                    "total_chunks": existing_session.total_chunks,
                    "uploaded_chunks": list(uploaded_chunks),
                }
            )

    # 创建新的上传会话
    upload_id = uuid.uuid4().hex
    _, _, _, _, save_path = await get_chunk_file_path_name(data.file_name, upload_id)
    await UploadChunk.create(
        upload_id=upload_id,
        chunk_index=-1,
        total_chunks=total_chunks,
        file_size=data.file_size,
        chunk_size=data.chunk_size,
        chunk_hash=data.file_hash,
        file_name=data.file_name,
        save_path=save_path,
    )
    await ip_limit["upload"].add_ip(ip)
    return APIResponse(
        detail={
            "existed": False,
            "upload_id": upload_id,
            "chunk_size": data.chunk_size,
            "total_chunks": total_chunks,
            "uploaded_chunks": [],
        }
    )


@chunk_api.post(
    "/upload/chunk/{upload_id}/{chunk_index}",
    dependencies=[Depends(share_required_login)],
)
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    chunk: UploadFile = File(...),
):
    # 获取上传会话信息
    chunk_info = await UploadChunk.filter(upload_id=upload_id, chunk_index=-1).first()
    if not chunk_info:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="上传会话不存在")

    # 检查分片索引有效性
    if chunk_index < 0 or chunk_index >= chunk_info.total_chunks:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="无效的分片索引")

    # 检查是否已上传（支持断点续传）
    existing_chunk = await UploadChunk.filter(
        upload_id=upload_id, chunk_index=chunk_index, completed=True
    ).first()
    if existing_chunk:
        return APIResponse(
            detail={"chunk_hash": existing_chunk.chunk_hash, "skipped": True}
        )

    # 读取分片数据：按声明 chunk_size 上限流式读取（超限即截断拒绝），
    # 防止客户端声明超大 chunk_size 后整包读入内存打爆服务
    chunk_data = b""
    max_read = chunk_info.chunk_size + 1024
    while len(chunk_data) < max_read:
        part = await chunk.read(min(256 * 1024, max_read - len(chunk_data)))
        if not part:
            break
        chunk_data += part
    chunk_size = len(chunk_data)

    # 校验分片大小不超过声明的 chunk_size
    if chunk_size > chunk_info.chunk_size:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"分片大小超过声明值: 最大 {chunk_info.chunk_size}, 实际 {chunk_size}",
        )

    # 计算已上传分片数，校验累计大小不超限（用分片数 * chunk_size 估算）
    uploaded_count = await UploadChunk.filter(
        upload_id=upload_id, completed=True
    ).count()
    # 限制单个会话的分片数量，防止资源耗尽（允许多 5 个重传分片）
    if uploaded_count > chunk_info.total_chunks + 5:
        raise HTTPException(
            status_code=400, detail="分片上传数量已超过限制"
        )
    # 已上传分片的最大可能大小 + 当前分片
    max_uploaded_size = uploaded_count * chunk_info.chunk_size + chunk_size
    if max_uploaded_size > settings.uploadSize:
        max_size_mb = settings.uploadSize / (1024 * 1024)
        raise HTTPException(
            status_code=403, detail=f"累计上传大小超过限制，最大为 {max_size_mb:.2f} MB"
        )

    chunk_hash = hashlib.sha256(chunk_data).hexdigest()

    save_path = chunk_info.save_path

    # 保存分片到存储
    storage = storages[settings.file_storage]()
    try:
        await storage.save_chunk(
            upload_id, chunk_index, chunk_data, chunk_hash, save_path
        )
    except Exception as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail="分片保存失败，请重试"
        )

    # 更新或创建分片记录（保存成功后再记录）
    await UploadChunk.update_or_create(
        upload_id=upload_id,
        chunk_index=chunk_index,
        defaults={
            "chunk_hash": chunk_hash,
            "completed": True,
            "file_size": chunk_info.file_size,
            "total_chunks": chunk_info.total_chunks,
            "chunk_size": chunk_info.chunk_size,
            "file_name": chunk_info.file_name,
            "save_path": chunk_info.save_path,
        },
    )
    return APIResponse(detail={"chunk_hash": chunk_hash})


@chunk_api.delete("/upload/{upload_id}", dependencies=[Depends(share_required_login)])
async def cancel_upload(upload_id: str):
    """取消上传并清理临时文件"""
    chunk_info = await UploadChunk.filter(upload_id=upload_id, chunk_index=-1).first()
    if not chunk_info:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="上传会话不存在")

    save_path = chunk_info.save_path

    # 清理存储中的临时文件
    storage = storages[settings.file_storage]()
    if save_path:
        try:
            await storage.clean_chunks(upload_id, save_path)
        except Exception as e:
            pass

    # 清理数据库记录
    await UploadChunk.filter(upload_id=upload_id).delete()

    return APIResponse(detail={"message": "上传已取消"})


@chunk_api.get(
    "/upload/status/{upload_id}", dependencies=[Depends(share_required_login)]
)
async def get_upload_status(upload_id: str):
    """获取上传状态"""
    chunk_info = await UploadChunk.filter(upload_id=upload_id, chunk_index=-1).first()
    if not chunk_info:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="上传会话不存在")

    # 获取已上传的分片列表
    uploaded_chunks = await UploadChunk.filter(
        upload_id=upload_id, completed=True
    ).values_list("chunk_index", flat=True)

    return APIResponse(
        detail={
            "upload_id": upload_id,
            "file_name": chunk_info.file_name,
            "file_size": chunk_info.file_size,
            "chunk_size": chunk_info.chunk_size,
            "total_chunks": chunk_info.total_chunks,
            "uploaded_chunks": list(uploaded_chunks),
            "progress": len(uploaded_chunks) / (chunk_info.total_chunks or 1) * 100,
        }
    )


@chunk_api.post(
    "/upload/complete/{upload_id}", dependencies=[Depends(share_required_login)]
)
async def complete_upload(
    upload_id: str, data: CompleteUploadModel, ip: str = Depends(ip_limit["upload"])
):
    # 校验过期时间类型
    if data.expire_style not in settings.expireStyle:
        raise HTTPException(status_code=400, detail="过期时间类型错误")

    # 校验文件备注长度（与文本分享 maxTextLength 一致）
    _validate_remark(data.remark)

    # 可选二次口令（分片上传在"完成"时给出，无需在上传会话里落库）
    access_code_hash = _hash_access_code(data.access_code)

    # 获取上传基本信息
    chunk_info = await UploadChunk.filter(upload_id=upload_id, chunk_index=-1).first()
    if not chunk_info:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="上传会话不存在")

    storage = storages[settings.file_storage]()
    # 验证所有分片
    completed_chunks_list = await UploadChunk.filter(
        upload_id=upload_id, completed=True
    ).all()
    if len(completed_chunks_list) != chunk_info.total_chunks:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="分片不完整")

    # 用分片数 * chunk_size 校验最大可能大小
    max_total_size = len(completed_chunks_list) * chunk_info.chunk_size
    if max_total_size > settings.uploadSize:
        save_path = chunk_info.save_path
        if save_path:
            try:
                await storage.clean_chunks(upload_id, save_path)
            except Exception:
                pass
        await UploadChunk.filter(upload_id=upload_id).delete()
        max_size_mb = settings.uploadSize / (1024 * 1024)
        raise HTTPException(
            status_code=403, detail=f"实际上传大小超过限制，最大为 {max_size_mb:.2f} MB"
        )

    save_path = chunk_info.save_path
    path = os.path.dirname(save_path) if save_path else ""
    prefix, suffix = os.path.splitext(chunk_info.file_name)

    try:
        # 合并文件并计算哈希（服务端权威值）
        _, file_hash = await storage.merge_chunks(upload_id, chunk_info, save_path)

        # ===== 秒传：同内容只存一份 =====
        # 用服务端算出的哈希查已有未过期文件，命中则复用其落盘、丢弃本次合并结果。
        # 之后删除记录前必须做引用检查（见 core/file_dedup.is_file_referenced_elsewhere），
        # 否则删掉任一条记录都会把共享文件一起删掉。
        dedup = await find_duplicate_file(file_hash, chunk_info.file_size)
        if dedup is not None and (dedup.file_path, dedup.uuid_file_name) != (
            path, f"{prefix}{suffix}"
        ):
            try:
                await storage.delete_file_by_path(path, f"{prefix}{suffix}")
            except Exception as e:
                logger.warning(f"秒传丢弃重复文件失败（忽略）: {e}")
            reuse_path, reuse_name = dedup.file_path, dedup.uuid_file_name
            logger.info(f"文件柜秒传命中: hash={file_hash[:12]} 复用 {reuse_path}/{reuse_name}")
        else:
            reuse_path, reuse_name = path, f"{prefix}{suffix}"

        # 创建文件记录
        expired_at, expired_count, used_count, code = await get_expire_info(
            data.expire_value, data.expire_style
        )
        # 落盘后过安全扫描：秒传命中时复用的是共享文件，拦截也不能删它
        if (reuse_path, reuse_name) == (path, f"{prefix}{suffix}"):
            await _scan_guard(save_path)
        else:
            await _scan_guard(f"{reuse_path}/{reuse_name}", allow_delete=False)
        owner_token = _new_owner_token()
        await FileCodes.create(
            code=code,
            file_hash=file_hash,  # 使用合并后计算的哈希
            is_chunked=True,
            upload_id=upload_id,
            size=chunk_info.file_size,
            remark=data.remark or None,
            expired_at=expired_at,
            expired_count=expired_count,
            used_count=used_count,
            file_path=reuse_path,
            uuid_file_name=reuse_name,
            prefix=prefix,
            suffix=suffix,
            expire_style=data.expire_style,
            expire_value=data.expire_value,
            owner_token=owner_token,
            access_code_hash=access_code_hash,
        )
        # 清理临时文件
        await storage.clean_chunks(upload_id, save_path)
        # 清理数据库中的分片记录
        await UploadChunk.filter(upload_id=upload_id).delete()
        await ip_limit["upload"].add_ip(ip)
        await record("upload", target_type="filecode", target_id=code,
                     result="success", detail=chunk_info.file_name, ip=ip)
        return APIResponse(detail={"code": code, "name": chunk_info.file_name, "owner_token": owner_token})
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        # 合并失败时清理临时文件
        try:
            await storage.clean_chunks(upload_id, save_path)
        except Exception:
            pass
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail="文件合并失败，请重试"
        )


# ============ 预签名上传API ============
presign_api = APIRouter(prefix="/presign", tags=["预签名上传"])

PRESIGN_SESSION_EXPIRES = 900  # 15分钟


async def _get_valid_session(
    upload_id: str, expected_mode: Optional[str] = None
) -> PresignUploadSession:
    """获取并验证会话"""
    session = await PresignUploadSession.filter(upload_id=upload_id).first()
    if not session:
        raise HTTPException(404, "上传会话不存在")
    if await session.is_expired():
        await session.delete()
        raise HTTPException(404, "上传会话已过期")
    if expected_mode and session.mode != expected_mode:
        raise HTTPException(400, f"此会话不支持{expected_mode}模式")
    return session


@presign_api.post("/upload/init", dependencies=[Depends(share_required_login)])
async def presign_upload_init(
    data: PresignUploadInitRequest, ip: str = Depends(ip_limit["upload"])
):
    """初始化预签名上传，S3返回直传URL，其他存储返回代理URL"""
    _validate_file_type(data.file_name)
    if data.file_size > settings.uploadSize:
        raise HTTPException(
            403,
            f"文件大小超过限制，最大为 {settings.uploadSize / (1024 * 1024):.2f} MB",
        )
    if data.expire_style not in settings.expireStyle:
        raise HTTPException(400, "过期时间类型错误")

    # 校验文件备注长度（与文本分享 maxTextLength 一致）
    _validate_remark(data.remark)

    # 限制未完成会话总数，防止资源耗尽
    total_active = await PresignUploadSession.filter(
        expires_at__gt=await get_now(),
    ).count()
    if total_active > 50:
        raise HTTPException(429, "正在进行中的上传过多，请稍后再试")

    upload_id = uuid.uuid4().hex
    path, _, _, filename, save_path = await FileUploadService.generate_file_path(
        data.file_name, upload_id
    )

    storage: FileStorageInterface = storages[settings.file_storage]()
    presigned_url = await storage.generate_presigned_upload_url(
        save_path, PRESIGN_SESSION_EXPIRES
    )

    mode = "direct" if presigned_url else "proxy"
    upload_url = presigned_url or f"/presign/upload/proxy/{upload_id}"

    await PresignUploadSession.create(
        upload_id=upload_id,
        file_name=filename,
        file_size=data.file_size,
        save_path=save_path,
        mode=mode,
        expire_value=data.expire_value,
        expire_style=data.expire_style,
        remark=data.remark or None,
        expires_at=await get_now() + timedelta(seconds=PRESIGN_SESSION_EXPIRES),
    )

    await ip_limit["upload"].add_ip(ip)
    return APIResponse(
        detail={
            "upload_id": upload_id,
            "upload_url": upload_url,
            "mode": mode,
            "expires_in": PRESIGN_SESSION_EXPIRES,
        }
    )


@presign_api.put(
    "/upload/proxy/{upload_id}", dependencies=[Depends(share_required_login)]
)
async def presign_upload_proxy(
    upload_id: str,
    file: UploadFile = File(...),
    access_code: str = Form(default=""),
    ip: str = Depends(ip_limit["upload"]),
):
    """代理模式上传，服务器转存到存储后端"""
    session = await _get_valid_session(upload_id, expected_mode="proxy")

    file_size = await validate_file_size(file, settings.uploadSize)
    if abs(file_size - session.file_size) > 1024:
        raise HTTPException(400, "文件大小与声明不符")

    storage: FileStorageInterface = storages[settings.file_storage]()
    try:
        await storage.save_file(file, session.save_path)
    except Exception as e:
        raise HTTPException(500, "文件保存失败，请重试")

    code = await FileUploadService.create_file_record(
        session.file_name,
        file_size,
        os.path.dirname(session.save_path),
        session.expire_value,
        session.expire_style,
        remark=session.remark or None,
        access_code_hash=_hash_access_code(access_code),
    )

    await session.delete()
    await ip_limit["upload"].add_ip(ip)
    return APIResponse(detail={"code": code, "name": session.file_name})


@presign_api.post(
    "/upload/confirm/{upload_id}", dependencies=[Depends(share_required_login)]
)
async def presign_upload_confirm(
    upload_id: str,
    data: Optional[PresignConfirmRequest] = None,
    ip: str = Depends(ip_limit["upload"]),
):
    """直传确认，客户端完成S3直传后调用获取分享码"""
    session = await _get_valid_session(upload_id, expected_mode="direct")

    storage: FileStorageInterface = storages[settings.file_storage]()
    if not await storage.file_exists(session.save_path):
        raise HTTPException(404, "文件未上传或上传失败")

    # 校验上传文件大小是否与声明一致
    if hasattr(storage, 'get_file_size'):
        actual_size = await storage.get_file_size(session.save_path)
        if actual_size is not None and abs(actual_size - session.file_size) > 1024:
            # 文件大小差异超过1KB，可能被篡改
            try:
                # 删除不匹配的文件
                temp_file_code = FileCodes(
                    file_path=os.path.dirname(session.save_path),
                    uuid_file_name=os.path.basename(session.save_path),
                )
                await storage.delete_file(temp_file_code)
            except Exception:
                pass
            raise HTTPException(400, "上传文件大小与声明不符")

    code = await FileUploadService.create_file_record(
        session.file_name,
        session.file_size,
        os.path.dirname(session.save_path),
        session.expire_value,
        session.expire_style,
        remark=session.remark or None,
        access_code_hash=_hash_access_code((data.access_code if data else "")),
    )

    await session.delete()
    await ip_limit["upload"].add_ip(ip)
    return APIResponse(detail={"code": code, "name": session.file_name})


@presign_api.get(
    "/upload/status/{upload_id}", dependencies=[Depends(share_required_login)]
)
async def presign_upload_status(upload_id: str):
    """查询上传会话状态"""
    session = await PresignUploadSession.filter(upload_id=upload_id).first()
    if not session:
        raise HTTPException(404, "上传会话不存在")

    return APIResponse(
        detail={
            "upload_id": session.upload_id,
            "file_name": session.file_name,
            "file_size": session.file_size,
            "mode": session.mode,
            "created_at": session.created_at.isoformat(),
            "expires_at": session.expires_at.isoformat(),
            "is_expired": await session.is_expired(),
        }
    )


@presign_api.delete("/upload/{upload_id}", dependencies=[Depends(share_required_login)])
async def presign_upload_cancel(upload_id: str):
    """取消上传会话"""
    session = await PresignUploadSession.filter(upload_id=upload_id).first()
    if not session:
        raise HTTPException(404, "上传会话不存在")

    if session.mode == "direct":
        storage: FileStorageInterface = storages[settings.file_storage]()
        try:
            if await storage.file_exists(session.save_path):
                temp_file_code = FileCodes(
                    file_path=os.path.dirname(session.save_path),
                    uuid_file_name=os.path.basename(session.save_path),
                )
                await storage.delete_file(temp_file_code)
        except Exception:
            pass

    await session.delete()
    return APIResponse(detail={"message": "上传会话已取消"})


# ============ 多文件下载API ============


@share_api.get("/download/item/{item_id}")
async def download_single_file(item_id: int, code: str, key: str = "", ip: str = Depends(ip_limit["error"])):
    """下载多文件分享中的单个文件"""
    file_item = await FileItem.filter(id=item_id).first()
    if not file_item:
        raise HTTPException(404, "文件不存在")

    # 验证取件码权限
    file_code = await FileCodes.filter(id=file_item.file_code_id).first()
    if not file_code or file_code.code != code:
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(403, "取件码不匹配")

    # 二次口令保护的分享：本入口只认取件码，必须再校验 select 阶段下发的 token
    if file_code.access_code_hash and not await verify_select_token(key, code):
        await ip_limit["error"].add_ip(ip)
        await record("download", target_type="filecode", target_id=code,
                     result="denied", detail="缺少提取口令 token", ip=ip)
        raise HTTPException(403, "需要提取口令")

    if await file_code.is_expired():
        raise HTTPException(404, "文件已过期")

    # 下载时原子扣减次数（并发安全；扣减失败 = 次数已尽）
    if not await update_file_usage(file_code):
        raise HTTPException(410, "文件已过期")

    file_storage: FileStorageInterface = storages[settings.file_storage]()
    full_path = f"{file_item.storage_path}/{file_item.uuid_file_name}"

    # 本地存储直接返回文件
    if settings.file_storage == "local":
        from fastapi.responses import FileResponse
        from urllib.parse import quote
        from core.settings import data_root

        file_path = data_root / full_path
        if not file_path.exists():
            raise HTTPException(404, "文件已过期删除")
        filename = file_item.file_name
        encoded_filename = quote(filename, safe='')
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }
        try:
            content_length = file_path.stat().st_size
            headers["Content-Length"] = str(content_length)
        except Exception:
            pass
        return FileResponse(
            file_path,
            media_type="application/octet-stream",
            headers=headers,
            filename=filename,
        )

    # 其他存储：使用流式响应
    async def stream_generator():
        async for chunk in file_storage.get_file_stream(full_path):
            yield chunk

    from urllib.parse import quote
    filename = file_item.file_name
    encoded_filename = quote(filename, safe='')
    return StreamingResponse(
        stream_generator(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@share_api.get("/zip/{code}")
async def download_zip(code: str, key: str = "", ip: str = Depends(ip_limit["error"])):
    """流式打包下载文件（单文件分享同样支持打包）"""
    has, file_code = await get_code_file_by_code(code)
    if not has:
        await ip_limit["error"].add_ip(ip)
        # 区分"不存在"和"已过期"
        if isinstance(file_code, str) and "过期" in file_code:
            raise HTTPException(410, file_code)
        raise HTTPException(404, "文件不存在")

    assert isinstance(file_code, FileCodes)

    # 二次口令保护的分享：打包入口同样要 token，否则知道码即可整包拖走
    if file_code.access_code_hash and not await verify_select_token(key, file_code.code):
        await ip_limit["error"].add_ip(ip)
        await record("download", target_type="filecode", target_id=file_code.code,
                     result="denied", detail="打包下载缺少提取口令 token", ip=ip)
        raise HTTPException(403, "需要提取口令")

    # 单文件分享也创建了 FileItem 记录，因此同样可以打包下载
    items = await FileItem.filter(file_code=file_code).order_by("sort_order")
    if not items:
        raise HTTPException(404, "没有文件可下载")

    return StreamingResponse(
        create_zip_stream(items),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(code)}.zip"
        },
    )


# ============ 收件箱API ============
collection_api = APIRouter(prefix="/collection", tags=["收件箱"])


def _generate_code(length: int = 6) -> str:
    """生成密码学安全的随机码"""
    import secrets
    import string
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


import re

_CODE_PATTERN = re.compile(r"^[A-Z0-9]{4,12}$")


def _validate_code(code: str) -> str:
    """验证码格式：4-12位大写字母+数字，自动转大写。范围校验以兼容调整码位数前的旧码。"""
    if not code:
        raise HTTPException(status_code=400, detail="码不能为空")
    code = code.strip().upper()
    if not _CODE_PATTERN.match(code):
        raise HTTPException(status_code=400, detail="码格式错误，必须为4-12位大写字母和数字")
    return code


_ACCESS_CODE_MIN_LEN = 2
_ACCESS_CODE_MAX_LEN = 32


def _hash_access_code(raw: str) -> Optional[str]:
    """把可选的提取口令转成待入库哈希；空串表示该分享不启用二次口令。

    存 PBKDF2 哈希而非明文：库被拖走时口令不可逆推。迭代次数沿用管理员口令那套
    参数，等于给在线爆破加了"每次尝试都要算一次 KDF"的成本。
    """
    code = (raw or "").strip()
    if not code:
        return None
    if not (_ACCESS_CODE_MIN_LEN <= len(code) <= _ACCESS_CODE_MAX_LEN):
        raise HTTPException(
            status_code=400,
            detail=f"提取口令长度需为{_ACCESS_CODE_MIN_LEN}-{_ACCESS_CODE_MAX_LEN}位",
        )
    return hash_password(code)


async def _verify_access_code(file_code: FileCodes, raw: str) -> bool:
    """校验提取口令。未设置口令（存量分享）恒为 True。"""
    stored = file_code.access_code_hash or ""
    if not stored:
        return True
    provided = (raw or "").strip()
    if not provided:
        return False
    # PBKDF2 是同步 CPU 计算（约几十毫秒），放线程池避免阻塞事件循环
    return await asyncio.to_thread(verify_password, provided, stored)


async def _require_access_code(file_code: FileCodes, raw: str, ip: str) -> None:
    """口令校验失败时拒绝，并计入错误限流。

    刻意**不**消耗取件次数：否则攻击者猜错几次就能把别人的分享额度烧光，
    变成拒绝服务手段。次数只在口令校验通过后由 update_file_usage 扣减。
    """
    if await _verify_access_code(file_code, raw):
        return
    await ip_limit["error"].add_ip(ip)
    provided = bool((raw or "").strip())
    await record(
        "retrieve",
        target_type="filecode",
        target_id=file_code.code,
        result="denied",
        detail="提取口令错误" if provided else "缺少提取口令",
        ip=ip,
    )
    raise HTTPException(
        status_code=403, detail="提取口令错误，请重新输入" if provided else "该分享需要提取口令"
    )


def _expire_to_seconds(expire_style: str, expire_value: int) -> int:
    """将过期时间转换为秒数（count 模式返回 0，不限制时间）"""
    if expire_style == "day":
        return expire_value * 86400
    elif expire_style == "hour":
        return expire_value * 3600
    elif expire_style == "minute":
        return expire_value * 60
    elif expire_style == "count":
        return 0  # count 模式仅限制使用次数，不限制时间
    return 0


def _expire_bound_seconds(expire_style: str, expire_value: int) -> Optional[int]:
    """计算过期配置的绝对时间上限（秒），用于跨单位比较投件码/取件码是否超出收件箱。
    forever 返回 None（无限）；count 返回 None（按次数无时间维度，不与收件箱时间比较，直接放行）；
    day/hour/minute 按秒。"""
    if expire_style in ("forever", "count"):
        return None
    return _expire_to_seconds(expire_style, expire_value)


def _validate_remark(remark: str) -> str:
    """校验文件分享备注长度（与文本分享 maxTextLength 一致，按字符数），超限拒绝。"""
    if remark:
        max_text_len = getattr(settings, "maxTextLength", 200000)
        if len(remark) > max_text_len:
            raise HTTPException(status_code=403, detail="备注过长")
    return remark


def _validate_file_type(filename: str) -> None:
    """按后台配置的上传文件类型白名单校验（空 = 不限制）。"""
    whitelist = str(getattr(settings, "fileTypeWhitelist", "") or "").strip()
    if not whitelist:
        return
    allowed = {
        ext.strip().lower().lstrip(".")
        for ext in whitelist.split(",")
        if ext.strip()
    }
    if not allowed:
        return
    ext = os.path.splitext(filename or "")[1].lstrip(".").lower()
    if ext not in allowed:
        raise HTTPException(status_code=403, detail="该文件类型不允许上传")


def _calculate_expired_at(expire_style: str, expire_value: int, base_time: datetime.datetime) -> Optional[datetime.datetime]:
    """统一计算过期时间（count 模式给 365 天，forever 返回 None）"""
    if expire_style == "forever":
        return None
    delta_map = {
        "day": timedelta(days=1),
        "hour": timedelta(hours=1),
        "minute": timedelta(minutes=1),
        "count": timedelta(days=365),  # count 模式给较长过期时间
    }
    delta = delta_map.get(expire_style, timedelta(days=7))
    return base_time + delta * expire_value


async def _get_unique_code(model_class, field_name: str, length: int = 0) -> str:
    """确保码唯一，最多重试 20 次；长度默认取后台配置 codeLength（4~12）"""
    if not length:
        length = int(getattr(settings, "codeLength", 6) or 6)
    length = max(4, min(length, 12))
    for _ in range(20):
        code = _generate_code(length)
        if not await model_class.filter(**{field_name: code}).exists():
            return code
    raise HTTPException(500, "生成唯一码失败，请重试")


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
    if data.expire_style not in settings.expireStyle:
        raise HTTPException(400, "过期时间类型错误")
    # 收件箱（整箱）过期按次数不符合逻辑：收件箱存活期应有时间维度，禁用 count
    if data.expire_style == "count":
        raise HTTPException(400, "收件箱过期时间不支持按次数")
    if data.delivery_expire_style not in settings.expireStyle:
        raise HTTPException(400, "投件码过期时间类型错误")
    if data.retrieve_expire_style not in settings.expireStyle:
        raise HTTPException(400, "取件码过期时间类型错误")

    # 校验投件码和取件码过期时间不超过收件箱过期时间
    # 跨单位统一比较：day/hour/minute 按秒；子码为 forever 或 count（按次数，无时间维度）时不与
    # 收件箱时间比较直接放行（count 次数用尽即过期，不受收件箱时间约束）
    collection_bound = _expire_bound_seconds(data.expire_style, data.expire_value)
    if collection_bound is not None:
        for sub_style, sub_value, label in (
            (data.delivery_expire_style, data.delivery_expire_value, "投件码"),
            (data.retrieve_expire_style, data.retrieve_expire_value, "取件码"),
        ):
            if sub_style == "count":
                # 按次数：无时间维度，次数用尽即过期，不与收件箱时间比较，直接放行
                continue
            sub_bound = _expire_bound_seconds(sub_style, sub_value)
            if sub_bound is None or sub_bound > collection_bound:
                raise HTTPException(400, f"{label}过期时间不能超过收件箱过期时间")

    # 向后兼容：旧字段 expire_days 自动转换
    if data.expire_days is not None and data.expire_style == "day" and data.expire_value == 7:
        expire_value = data.expire_days
        expire_style = "day"
    else:
        expire_value = data.expire_value
        expire_style = data.expire_style

    # 统一计算过期时间
    expired_at = _calculate_expired_at(expire_style, expire_value, now)
    delivery_expired_at = _calculate_expired_at(data.delivery_expire_style, data.delivery_expire_value, now)
    retrieve_expired_at = _calculate_expired_at(data.retrieve_expire_style, data.retrieve_expire_value, now)

    box = await CollectionBox.create(
        collection_code=collection_code,
        delivery_code=delivery_code,
        retrieve_code=retrieve_code,
        title=data.title,
        description=data.description,
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

    await ip_limit["upload"].add_ip(ip)

    # 触发事件
    await CollectionEvents.emit("collection.created", {
        "collection_code": box.collection_code,
        "delivery_code": box.delivery_code,
        "title": box.title,
        "creator_ip": ip,
    })

    return APIResponse(detail=await _build_collection_response(box, include_files=False))


# ============ 投件API（使用 delivery_code）============
delivery_api = APIRouter(prefix="/delivery", tags=["投递"])


@delivery_api.get("/{delivery_code}")
async def get_delivery_page(delivery_code: str, ip: str = Depends(ip_limit["error"])):
    """获取投递页面信息（仅上传表单，不暴露文件列表）"""
    delivery_code = _validate_code(delivery_code)
    box = await CollectionBox.filter(delivery_code=delivery_code).first()
    if not box:
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(404, "投件码不存在")
    if await box.is_expired():
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(410, "收件箱已过期")
    if await box.is_delivery_expired():
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(410, "投件码已过期")

    file_count = await CollectionFile.filter(collection=box).count()
    return APIResponse(detail={
        "delivery_code": box.delivery_code,
        "title": box.title,
        "description": box.description,
        "max_files": box.max_files,
        "file_count": file_count,
        "delivery_count": box.delivery_count,
        "delivery_used_count": box.delivery_used_count,
        "created_at": box.created_at.isoformat() if box.created_at else None,
        # 收件箱过期配置
        "expire_style": box.expire_style,
        "expire_value": box.expire_value,
        "expired_at": box.expired_at.isoformat() if box.expired_at else None,
        # 投件码过期配置
        "delivery_expire_style": box.delivery_expire_style,
        "delivery_expire_value": box.delivery_expire_value,
        "delivery_expired_at": box.delivery_expired_at.isoformat() if box.delivery_expired_at else None,
    })


# ============ 投递分片上传（复用 /chunk 分片接口，本模块负责投递记录与合并收尾） ============


async def _get_valid_delivery_box(delivery_code: str) -> CollectionBox:
    """校验投件码并返回有效收件箱（不存在/过期均抛错）"""
    delivery_code = _validate_code(delivery_code)
    box = await CollectionBox.filter(delivery_code=delivery_code).first()
    if not box:
        raise HTTPException(404, "投件码不存在")
    if await box.is_expired():
        raise HTTPException(410, "收件箱已过期")
    if await box.is_delivery_expired():
        raise HTTPException(410, "投件码已过期或投递次数已用完")
    return box


async def _ensure_collection_capacity(box: CollectionBox) -> None:
    """事务内校验收件箱容量（投递前调用，防止并发超满）"""
    current_count = await CollectionFile.filter(collection=box).count()
    if current_count >= box.max_files:
        raise HTTPException(403, "收件箱已满")


async def _reserve_delivery_slot(box: CollectionBox) -> bool:
    """原子占位投递次数（防止并发超限）；返回是否已占位（失败抛 403）"""
    if box.delivery_count <= 0:
        return False
    updated = await CollectionBox.filter(
        id=box.id, delivery_used_count__lt=box.delivery_count
    ).update(delivery_used_count=F("delivery_used_count") + 1)
    if updated == 0:
        raise HTTPException(403, "投递次数已用完")
    return True


async def _release_delivery_slot(box: CollectionBox, reserved: bool) -> None:
    """上传失败/取消时回滚占位的投递次数"""
    if not reserved:
        return
    await CollectionBox.filter(id=box.id, delivery_used_count__gt=0).update(
        delivery_used_count=F("delivery_used_count") - 1
    )


def _delivery_storage_path(box: CollectionBox) -> Tuple[str, str]:
    """生成投递文件的存储目录路径（collection/{code}/{date}/{uuid}），返回 (path, file_uuid)"""
    today = datetime.datetime.now()
    storage_path_prefix = settings.storage_path.strip("/")
    file_uuid = uuid.uuid4().hex
    base_path = f"collection/{box.collection_code}/{today.strftime('%Y/%m/%d')}/{file_uuid}"
    path = f"{storage_path_prefix}/{base_path}" if storage_path_prefix else base_path
    return path, file_uuid


async def _finalize_delivery_upload(
    box: CollectionBox,
    collection_file: CollectionFile,
    filename: str,
    path: str,
    file_size: int,
    uploader_name: str,
    ip: str,
) -> FileCodes:
    """创建 FileCode 记录、回填投递记录、广播完成事件（直传与分片 complete 共用）"""
    prefix, suffix = os.path.splitext(filename)
    file_code = await FileCodes.create(
        code=f"col_{collection_file.id}_{_generate_code(4)}",
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
    collection_file.file_code = file_code
    collection_file.status = "completed"
    await collection_file.save()

    await ws_manager.broadcast(box.collection_code, {
        "type": "file_completed",
        "file_id": collection_file.id,
        "filename": filename,
        "file_size": file_size,
        "uploader": uploader_name or "匿名",
    })
    await CollectionEvents.emit("file.delivered", {
        "file_id": collection_file.id,
        "collection_code": box.collection_code,
        "filename": filename,
        "file_size": file_size,
        "uploader": uploader_name or "匿名",
    })
    await ip_limit["upload"].add_ip(ip)
    return file_code


@delivery_api.post("/chunk/init/", dependencies=[Depends(share_required_login)])
async def init_delivery_chunk_upload(
    data: InitDeliveryChunkUploadRequest,
    ip: str = Depends(ip_limit["upload"]),
):
    """投递分片上传初始化：占位投递次数、创建上传会话与投递记录"""
    _validate_file_type(data.file_name)
    box = await _get_valid_delivery_box(data.delivery_code)
    uploader_name = (data.uploader_name or "").strip()[:50]

    # 服务端校验：根据 total_chunks * chunk_size 计算理论最大上传量
    total_chunks = (data.file_size + data.chunk_size - 1) // data.chunk_size
    max_possible_size = total_chunks * data.chunk_size
    if max_possible_size > settings.uploadSize:
        max_size_mb = settings.uploadSize / (1024 * 1024)
        raise HTTPException(
            status_code=403, detail=f"文件大小超过限制，最大为 {max_size_mb:.2f} MB"
        )

    reserved = await _reserve_delivery_slot(box)
    try:
        # 事务内校验收件箱容量并创建投递记录（防止并发超满）
        async with in_transaction():
            await _ensure_collection_capacity(box)
            upload_id = uuid.uuid4().hex
            path, _ = _delivery_storage_path(box)
            filename = await sanitize_filename(unquote(data.file_name))
            save_path = f"{path}/{filename}"
            collection_file = await CollectionFile.create(
                collection=box,
                uploader_name=uploader_name,
                uploader_ip=ip,
                status="uploading",
                upload_id=upload_id,
                file_name=filename,
                file_size=data.file_size,
                chunk_size=data.chunk_size,
                total_chunks=total_chunks,
            )
            await UploadChunk.create(
                upload_id=upload_id,
                chunk_index=-1,
                total_chunks=total_chunks,
                file_size=data.file_size,
                chunk_size=data.chunk_size,
                chunk_hash=data.file_hash,
                file_name=filename,
                save_path=save_path,
            )

        await ws_manager.broadcast(box.collection_code, {
            "type": "file_uploading",
            "file_id": collection_file.id,
            "filename": filename,
            "progress": 0,
            "uploader": uploader_name or "匿名",
        })
        await ip_limit["upload"].add_ip(ip)
        return APIResponse(detail={
            "upload_id": upload_id,
            "file_id": collection_file.id,
            "chunk_size": data.chunk_size,
            "total_chunks": total_chunks,
            "uploaded_chunks": [],
        })
    except HTTPException:
        if reserved:
            await _release_delivery_slot(box, True)
        raise
    except Exception:
        if reserved:
            await _release_delivery_slot(box, True)
        raise


@delivery_api.post("/chunk/cancel/", dependencies=[Depends(share_required_login)])
async def cancel_delivery_chunk_upload(
    delivery_code: str = Form(...),
    upload_id: str = Form(...),
    ip: str = Depends(ip_limit["upload"]),
):
    """取消投递分片上传：清理分片文件、删除投递记录并回滚投递次数"""
    box = await _get_valid_delivery_box(delivery_code)
    collection_file = await CollectionFile.filter(
        collection=box, upload_id=upload_id
    ).first()
    chunk_info = await UploadChunk.filter(upload_id=upload_id, chunk_index=-1).first()
    if chunk_info and chunk_info.save_path:
        storage = storages[settings.file_storage]()
        try:
            await storage.clean_chunks(upload_id, chunk_info.save_path)
        except Exception:
            pass
        await UploadChunk.filter(upload_id=upload_id).delete()
    if collection_file and collection_file.status == "uploading":
        await collection_file.delete()
        await _release_delivery_slot(box, True)
    return APIResponse(detail={"message": "上传已取消"})


@delivery_api.post("/upload/", dependencies=[Depends(share_required_login)])
async def delivery_upload(
    delivery_code: str = Form(...),
    file: UploadFile = File(...),
    uploader_name: str = Form(default=""),
    ip: str = Depends(ip_limit["upload"]),
):
    """投递文件到收件箱，使用 delivery_code 认证（非分片模式）"""
    _validate_file_type(file.filename or "")
    box = await _get_valid_delivery_box(delivery_code)
    uploader_name = (uploader_name or "").strip()[:50]

    reserved = await _reserve_delivery_slot(box)
    collection_file = None
    try:
        file_size = await validate_file_size(file, settings.uploadSize)

        # 事务内校验收件箱容量并创建投递记录（防止并发超满）
        async with in_transaction():
            await _ensure_collection_capacity(box)
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

        # 保存文件并创建 FileCode 记录
        file_storage: FileStorageInterface = storages[settings.file_storage]()
        path, _ = _delivery_storage_path(box)
        filename = await sanitize_filename(unquote(file.filename or ""))
        save_path = f"{path}/{filename}"

        await file_storage.save_file(file, save_path)
        await _finalize_delivery_upload(
            box, collection_file, filename, path, file_size, uploader_name, ip,
        )
        return APIResponse(detail={
            "id": collection_file.id,
            "filename": filename,
            "file_size": file_size,
            "status": "completed",
        })

    except HTTPException:
        # 校验类失败：回滚占位的投递次数
        if reserved:
            await _release_delivery_slot(box, True)
        raise
    except Exception as e:
        # 上传失败：回滚占位的投递次数
        logger.exception("投递文件上传失败: %s", str(e))
        if reserved:
            await _release_delivery_slot(box, True)
        if collection_file is not None:
            collection_file.status = "failed"
            await collection_file.save()
            # 不向订阅者暴露内部异常细节，仅记录服务端日志
            await ws_manager.broadcast(box.collection_code, {
                "type": "file_delivery_failed",
                "file_id": collection_file.id,
                "filename": file.filename or "",
                "error": "文件上传失败，请重试",
            })
            # 触发事件
            await CollectionEvents.emit("file.delivery_failed", {
                "file_id": collection_file.id,
                "collection_code": box.collection_code,
                "filename": file.filename or "",
                "error": "文件上传失败，请重试",
            })
        raise HTTPException(500, "文件上传失败，请重试")


@delivery_api.post("/chunk/complete/", dependencies=[Depends(share_required_login)])
async def complete_delivery_chunk_upload(
    delivery_code: str = Form(...),
    upload_id: str = Form(...),
    uploader_name: str = Form(default=""),
    ip: str = Depends(ip_limit["upload"]),
):
    """投递分片上传完成：合并分片并创建投递文件记录"""
    box = await _get_valid_delivery_box(delivery_code)
    uploader_name = (uploader_name or "").strip()[:50]

    collection_file = await CollectionFile.filter(
        collection=box, upload_id=upload_id
    ).first()
    if not collection_file or collection_file.status != "uploading":
        raise HTTPException(404, "上传会话不存在")

    chunk_info = await UploadChunk.filter(upload_id=upload_id, chunk_index=-1).first()
    if not chunk_info:
        # 会话丢失（如后台清理）：删除投递记录，分片文件由后台任务兜底清理
        await collection_file.delete()
        raise HTTPException(404, "上传会话不存在")

    storage = storages[settings.file_storage]()
    # 验证所有分片
    completed_chunks_list = await UploadChunk.filter(
        upload_id=upload_id, completed=True
    ).all()
    if len(completed_chunks_list) != chunk_info.total_chunks:
        raise HTTPException(400, "分片不完整")

    save_path = chunk_info.save_path
    path = os.path.dirname(save_path) if save_path else ""
    prefix, suffix = os.path.splitext(chunk_info.file_name)

    try:
        _, _ = await storage.merge_chunks(upload_id, chunk_info, save_path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        # 合并失败：清理临时分片
        try:
            await storage.clean_chunks(upload_id, save_path)
        except Exception:
            pass
        raise HTTPException(500, "文件合并失败，请重试")

    try:
        await _finalize_delivery_upload(
            box, collection_file, chunk_info.file_name, path,
            chunk_info.file_size, uploader_name, ip,
        )
        # 清理临时分片文件与记录
        try:
            await storage.clean_chunks(upload_id, save_path)
        except Exception:
            pass
        await UploadChunk.filter(upload_id=upload_id).delete()
        return APIResponse(detail={
            "id": collection_file.id,
            "filename": chunk_info.file_name,
            "file_size": chunk_info.file_size,
            "status": "completed",
        })
    except Exception:
        # 记录创建失败：回滚投递记录（分片由后台任务清理）
        try:
            await collection_file.delete()
        except Exception:
            pass
        raise HTTPException(500, "文件上传失败，请重试")


# ============ 收件箱管理API（使用 collection_code）============


@collection_api.get("/manage/{collection_code}")
async def get_collection_manage(collection_code: str, ip: str = Depends(ip_limit["error"])):
    """收件人管理页面 - 获取投递文件列表"""
    collection_code = _validate_code(collection_code)
    box = await CollectionBox.filter(collection_code=collection_code).first()
    if not box:
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(404, "收件箱不存在")
    if await box.is_expired():
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(410, "收件箱已过期")

    return APIResponse(detail=await _build_collection_response(box))


@collection_api.get("/retrieve/{retrieve_code}")
async def get_collection_retrieve(retrieve_code: str, ip: str = Depends(ip_limit["error"])):
    """取件码访问 - 只读查看文件列表（不可删除/管理，不暴露管理码与投件码）"""
    retrieve_code = _validate_code(retrieve_code)
    box = await CollectionBox.filter(retrieve_code=retrieve_code).first()
    if not box:
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(404, "取件码不存在")
    if await box.is_expired():
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(410, "收件箱已过期")
    if await box.is_retrieve_expired():
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(410, "取件码已过期")

    return APIResponse(detail=await _build_retrieve_response(box))


@collection_api.get("/manage-by-delivery/{delivery_code}")
async def get_collection_manage_by_delivery(delivery_code: str, ip: str = Depends(ip_limit["error"])):
    """通过投件码获取收件箱文件列表（投件码持有者可查看已投递的文件，不暴露管理码与取件码）"""
    delivery_code = _validate_code(delivery_code)
    box = await CollectionBox.filter(delivery_code=delivery_code).first()
    if not box:
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(404, "投件码不存在")
    if await box.is_expired():
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(410, "收件箱已过期")
    # 注意：不检查投件码是否过期，已投递的文件应始终可查看

    return APIResponse(detail=await _build_delivery_view_response(box))


async def _build_file_list(box: CollectionBox) -> list:
    """构建收件箱文件列表（使用 prefetch_related 消除 N+1 查询）"""
    files = await CollectionFile.filter(collection=box).prefetch_related('file_code').order_by("created_at")
    return [
        {
            "id": f.id,
            "file_name": f"{f.file_code.prefix}{f.file_code.suffix}" if f.file_code else "未知",
            "file_size": f.file_code.size if f.file_code else 0,
            "uploader_name": f.uploader_name or "",
            "status": f.status,
            "created_at": f.created_at.isoformat(),
        }
        for f in files
    ]


def _box_expire_fields(box: CollectionBox, prefix: str = "") -> dict:
    """收件箱子码过期字段（prefix: delivery_ / retrieve_；整箱 prefix 为空取自身过期配置）"""
    return {
        f"{prefix}expire_style": getattr(box, f"{prefix}expire_style"),
        f"{prefix}expire_value": getattr(box, f"{prefix}expire_value"),
        f"{prefix}expired_at": getattr(box, f"{prefix}expired_at").isoformat()
        if getattr(box, f"{prefix}expired_at") else None,
    }


def _build_collection_base(box: CollectionBox, file_list: list) -> dict:
    """收件箱三视角响应的公共字段（不含任何码，避免误暴露权限）。

    各视角函数在基字段上追加自己的码与子码过期字段：
    - 管理码视角：三码全部
    - 取件码视角：仅 retrieve_code
    - 投件码视角：仅 delivery_code
    """
    return {
        "title": box.title,
        "description": box.description,
        "max_files": box.max_files,
        "created_at": box.created_at.isoformat() if box.created_at else None,
        **_box_expire_fields(box),
        "file_count": len(file_list),
        "total_size": sum(f["file_size"] for f in file_list),
        "files": file_list,
    }


async def _build_collection_response(box: CollectionBox, include_files: bool = True) -> dict:
    """管理码视角的收件箱响应（含三码，供收件人管理）"""
    file_list = await _build_file_list(box) if include_files else []
    return {
        **_build_collection_base(box, file_list),
        "collection_code": box.collection_code,
        "delivery_code": box.delivery_code,
        "retrieve_code": box.retrieve_code,
        **_box_expire_fields(box, "delivery_"),
        **_box_expire_fields(box, "retrieve_"),
    }


async def _build_retrieve_response(box: CollectionBox) -> dict:
    """取件码视角的收件箱响应（不暴露管理码与投件码，仅取件所需信息）"""
    file_list = await _build_file_list(box)
    return {
        **_build_collection_base(box, file_list),
        "retrieve_code": box.retrieve_code,
        **_box_expire_fields(box, "retrieve_"),
    }


async def _build_delivery_view_response(box: CollectionBox) -> dict:
    """投件码视角的收件箱响应（不暴露管理码与取件码）"""
    file_list = await _build_file_list(box)
    return {
        **_build_collection_base(box, file_list),
        "delivery_code": box.delivery_code,
        **_box_expire_fields(box, "delivery_"),
    }


@collection_api.get("/status/{collection_code}")
async def get_collection_status(collection_code: str, ip: str = Depends(ip_limit["error"])):
    """获取收件箱状态（同管理页面数据）"""
    return await get_collection_manage(collection_code, ip)


@collection_api.patch("/update/{collection_code}")
async def update_collection_config(collection_code: str, data: UpdateCollectionConfigRequest, ip: str = Depends(ip_limit["error"])):
    """更新收件箱配置（收件箱过期时间不可更改，仅投件码/取件码过期时间和最大文件数可修改）"""
    box = await CollectionBox.filter(collection_code=collection_code).first()
    if not box:
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(404, "收件箱不存在")
    if await box.is_expired():
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(410, "收件箱已过期")

    update_data = {}

    # 收件箱绝对过期时间（创建后不可更改）：forever 时为 None（无限制）。
    # 投件码/取件码的过期时间从"当前时间"重新计算，必须不晚于收件箱的绝对过期时间，
    # 否则收件箱已过一段时间后，子码过期时间会超出收件箱（总时长比较无法覆盖此场景）。
    box_expired_at = box.expired_at

    # 投件码有效期（从当前时间重新计算，支持延长已过期的投件码）
    if data.delivery_expire_style is not None and data.delivery_expire_value is not None:
        if data.delivery_expire_style not in settings.expireStyle:
            raise HTTPException(400, "投件码过期时间类型错误")
        if box_expired_at is not None and data.delivery_expire_style != "count":
            # 收件箱非永久（有绝对过期时间）时：子码不可为永久，也不可超出收件箱绝对过期时间。
            # 子码为 count（按次数，无时间维度）时不与收件箱时间比较，直接放行（次数用尽即过期）
            if data.delivery_expire_style == "forever":
                raise HTTPException(400, "投件码过期时间不能超过收件箱过期时间")
            sub_expired_at = _calculate_expired_at(
                data.delivery_expire_style, data.delivery_expire_value, await get_now()
            )
            if sub_expired_at is not None and sub_expired_at > box_expired_at:
                raise HTTPException(400, "投件码过期时间不能超过收件箱过期时间")
        update_data["delivery_expire_style"] = data.delivery_expire_style
        update_data["delivery_expire_value"] = data.delivery_expire_value
        update_data["delivery_expired_at"] = _calculate_expired_at(
            data.delivery_expire_style, data.delivery_expire_value, await get_now()
        )

    # 取件码有效期（从当前时间重新计算，支持延长已过期的取件码）
    if data.retrieve_expire_style is not None and data.retrieve_expire_value is not None:
        if data.retrieve_expire_style not in settings.expireStyle:
            raise HTTPException(400, "取件码过期时间类型错误")
        if box_expired_at is not None and data.retrieve_expire_style != "count":
            # 收件箱非永久（有绝对过期时间）时：子码不可为永久，也不可超出收件箱绝对过期时间。
            # 子码为 count（按次数，无时间维度）时不与收件箱时间比较，直接放行（次数用尽即过期）
            if data.retrieve_expire_style == "forever":
                raise HTTPException(400, "取件码过期时间不能超过收件箱过期时间")
            sub_expired_at = _calculate_expired_at(
                data.retrieve_expire_style, data.retrieve_expire_value, await get_now()
            )
            if sub_expired_at is not None and sub_expired_at > box_expired_at:
                raise HTTPException(400, "取件码过期时间不能超过收件箱过期时间")
        update_data["retrieve_expire_style"] = data.retrieve_expire_style
        update_data["retrieve_expire_value"] = data.retrieve_expire_value
        update_data["retrieve_expired_at"] = _calculate_expired_at(
            data.retrieve_expire_style, data.retrieve_expire_value, await get_now()
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
        await ip_limit["error"].add_ip(ip)
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
async def download_collection_zip(code: str, ip: str = Depends(ip_limit["error"])):
    """打包下载收件箱所有文件（支持管理码和取件码）"""
    box = await CollectionBox.filter(collection_code=code).first()
    if not box:
        # 尝试按取件码查找
        box = await CollectionBox.filter(retrieve_code=code).first()
    if not box:
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(404, "收件箱不存在")
    if await box.is_expired():
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(410, "收件箱已过期")

    # 取件码独立过期检查
    if code == box.retrieve_code and await box.is_retrieve_expired():
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(410, "取件码已过期")

    collection_files = await CollectionFile.filter(collection=box, status="completed").order_by("created_at")
    if not collection_files:
        raise HTTPException(404, "没有文件可下载")

    # 批量获取所有关联 FileCode，避免逐文件查询
    code_ids = [cf.file_code_id for cf in collection_files if cf.file_code_id]
    file_items = []
    if code_ids:
        fc_map = {fc.id: fc for fc in await FileCodes.filter(id__in=code_ids).all()}
        for cid in code_ids:
            fc = fc_map.get(cid)
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
async def delete_collection_file(
    file_id: int, code: str, ip: str = Depends(ip_limit["error"])
):
    """删除收件箱中的文件（收件人操作，需校验管理码或取件码）"""
    collection_file = await CollectionFile.filter(id=file_id).first()
    if not collection_file:
        raise HTTPException(404, "文件不存在")

    # 校验码权限：仅管理码可删除（取件码只读，不允许删除）
    box = await CollectionBox.filter(id=collection_file.collection_id).first()
    if not box:
        raise HTTPException(404, "收件箱不存在")
    if code != box.collection_code:
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(403, "码不匹配，无权删除")

    if await box.is_expired():
        raise HTTPException(410, "收件箱已过期")

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

    collection_code = box.collection_code
    await collection_file.delete()

    # 广播文件删除
    if collection_code:
        await ws_manager.broadcast(collection_code, {
            "type": "file_deleted",
            "file_id": file_id,
        })

    return APIResponse(detail={"message": "文件已删除"})


# ============ WebSocket端点 ============
ws_api = APIRouter(tags=["WebSocket"])

# 收件箱 WebSocket 单房间连接数上限（防滥用：收件箱码可公开传播，避免被无限建连耗尽句柄/内存）
COLLECTION_WS_MAX_CONNECTIONS = 50


@ws_api.websocket("/ws/collection/{code}")
async def websocket_collection(websocket: WebSocket, code: str):
    """收件箱实时推送 WebSocket"""
    # 支持通过 collection_code 或 delivery_code 连接
    collection = await CollectionBox.filter(collection_code=code).first()
    if not collection:
        collection = await CollectionBox.filter(delivery_code=code).first()
    if not collection:
        await websocket.close(code=404, reason="收件箱不存在")
        return

    if await collection.is_expired():
        await websocket.close(code=410, reason="收件箱已过期")
        return

    room_code = collection.collection_code
    # 等待客户端发送第一条消息获取昵称
    await ws_manager.connect(room_code, websocket, nickname="")
    # 连接数上限：超过则拒绝（与房间 max_members 模式一致，connect 后再校验以完成握手）。
    # 用 room_size 而非直接读 active_connections：多实例下人数要含其它实例上的成员，
    # 否则每个实例各算各的，上限被放大 N 倍。
    if ws_manager.room_size(room_code) > COLLECTION_WS_MAX_CONNECTIONS:
        await ws_manager.send_personal(room_code, websocket, {"type": "room_full"})
        await websocket.close(code=1013, reason="房间已满")
        return
    try:
        # 第一条消息作为昵称注册
        first_msg = await websocket.receive_text()
        nickname = ""
        try:
            msg_data = json.loads(first_msg)
            if isinstance(msg_data, dict) and msg_data.get("type") == "join":
                nickname = msg_data.get("nickname", "")
                # 更新昵称（并同步给其它实例，否则远端成员列表全是"匿名"）
                await ws_manager.refresh_presence(
                    room_code, ws_manager.get_client_id(room_code, websocket), nickname
                )
        except (json.JSONDecodeError, ValueError):
            pass

        # 广播用户加入
        online_users = ws_manager.get_online_users(room_code)
        await ws_manager.broadcast(room_code, {
            "type": "user_joined",
            "nickname": nickname,
            "online_users": online_users,
        })

        while True:
            data = await websocket.receive_text()
            # 客户端发送的进度消息，广播给房间内其他连接
            try:
                msg = json.loads(data)
                if isinstance(msg, dict) and msg.get("type") in ("file_uploading", "file_progress"):
                    await ws_manager.broadcast(room_code, msg)
            except (json.JSONDecodeError, ValueError):
                pass  # 忽略非JSON消息（如ping）
    except WebSocketDisconnect:
        left_nickname = await ws_manager.disconnect(room_code, websocket)
        # 广播用户离开
        online_users = ws_manager.get_online_users(room_code)
        await ws_manager.broadcast(room_code, {
            "type": "user_left",
            "nickname": left_nickname,
            "online_users": online_users,
        })



# ============ 跨设备实时房间API（聊天/文件传输/直连快传合并为一） ============
room_api = APIRouter(prefix="/room", tags=["实时房间"])

ROOM_MESSAGE_MAX_LEN = 64 * 1024  # 单条消息上限 64KB
ROOM_RELAY_MAX_FRAME = 4 * 1024 * 1024  # 单帧二进制中继上限 4MB


async def _get_room(code: str) -> Room:
    """获取房间并校验状态"""
    code = _validate_code(code)
    room = await Room.filter(room_code=code).first()
    if not room:
        raise HTTPException(404, "房间不存在")
    if await room.is_expired():
        raise HTTPException(410, "房间已过期")
    return room


async def _build_room_response(room: Room) -> dict:
    return {
        "room_code": room.room_code,
        "title": room.title,
        "max_members": room.max_members,
        "expire_style": room.expire_style,
        "expire_value": room.expire_value,
        "expired_count": room.expired_count,
        "expired_at": room.expired_at.isoformat() if room.expired_at else None,
        "created_at": room.created_at.isoformat() if room.created_at else None,
        "save_messages": bool(room.save_messages),
        "cache_enabled": bool(room.cache_enabled),
    }


async def _create_room(data: CreateRoomRequest, ip: str) -> Room:
    """创建房间（统一为直连快传：聊天 + 文件 + P2P 媒体，服务器中转兜底）"""
    if data.expire_style not in settings.expireStyle:
        raise HTTPException(status_code=400, detail="过期时间类型错误")

    title = (data.title or "").strip()[:100]
    room_code = await _get_unique_code(Room, "room_code")
    is_count = data.expire_style == "count"
    # 人员上限：不超后台配置的 defaultMaxMembers（0/缺省用后台默认）
    default_max = max(1, int(getattr(settings, "defaultMaxMembers", 10) or 10))
    max_members = data.max_members or default_max
    max_members = max(1, min(int(max_members), default_max))
    room = await Room.create(
        room_code=room_code,
        title=title,
        mode="direct",
        creator_ip=ip,
        max_members=max_members,
        expire_style=data.expire_style,
        expire_value=data.expire_value,
        # count 模式：不限制时间，按"房间从有人变为无人"计次（expire_value 次）
        expired_at=None if is_count else _calculate_expired_at(data.expire_style, data.expire_value, await get_now()),
        expired_count=data.expire_value if is_count else -1,
        save_messages=1 if data.save_messages else 0,
        cache_enabled=1 if data.cache_enabled else 0,
    )
    await ip_limit["upload"].add_ip(ip)
    return room


@room_api.post("/create/", dependencies=[Depends(share_required_login)])
async def create_room(data: CreateRoomRequest, ip: str = Depends(ip_limit["upload"])):
    """创建房间（chat/transfer/direct 由 mode 决定），返回房间码"""
    room = await _create_room(data, ip)
    return APIResponse(detail=await _build_room_response(room))


@room_api.get("/info/{room_code}")
async def get_room_info(room_code: str, ip: str = Depends(ip_limit["error"])):
    """获取房间信息（加入页展示）"""
    room = await _get_room(room_code)
    return APIResponse(detail=await _build_room_response(room))


@room_api.get("/history/{room_code}")
async def get_room_history(room_code: str, limit: int = 100, before_id: int = 0):
    """房间历史记录：save_messages=1 房间返回聊天消息与传输过文件的元信息（只读展示），
    另附缓存文件列表（cache_enabled=1 房间，含在线可收取来源），供"历史文件"查看/收取。"""
    room = await _get_room(room_code)
    limit = max(1, min(int(limit), 200))

    messages = []
    if room.save_messages:
        # 最近 limit 条（按 id 倒序取再反转，保证时间正序）
        q = RoomMessage.filter(room_code=room.room_code)
        if before_id > 0:
            # 上滑加载更早的消息：只取比 before_id 更旧的一批（微信式分页）
            q = q.filter(id__lt=before_id)
        rows = await q.order_by("-id").limit(limit)
        for m in reversed(rows):
            # 文件消息自带缓存信息：storage_key 非空即可直接从服务器下载
            download_url = ""
            if (m.msg_type or "text") == "file" and m.storage_key:
                download_url = (
                    f"/room/{room.room_code}/file/{m.content}/download?"
                    f"{build_download_query(room.room_code, m.content)}"
                )
            messages.append({
                "id": m.id,
                "sender": m.sender or "匿名",
                "content": m.content or "",
                "msg_type": m.msg_type or "text",
                "file_name": m.file_name or "",
                "file_size": m.file_size or 0,
                # 服务器是否已缓存（历史文件可直接下载，无需发送方在线）
                "server_cached": bool(m.storage_key),
                "expires_at": m.expires_at.isoformat() if m.expires_at else None,
                "download_url": download_url,
                "ts": m.created_at.isoformat() if m.created_at else None,
            })

    files = []
    cached = await RoomCachedFile.filter(room_code=room.room_code).order_by("-id").limit(limit)
    for c in cached:
        # server_cached / download_url 必须一并下发：少了它，前端只能去问在线成员，
        # 服务器明明有缓存也会误报"暂无可用的在线缓存来源"
        has_storage = bool(c.storage_key)
        files.append({
            "transfer_id": c.transfer_id,
            "file_name": c.file_name or "",
            "file_size": c.file_size or 0,
            "file_hash": c.file_hash or "",
            "holders": [h for h in (c.cache_holder_ids or "").split(",") if h],
            "holder_names": [n for n in (c.holder_names or "").split(",") if n],
            "server_cached": has_storage,
            "expires_at": c.expires_at.isoformat() if c.expires_at else None,
            "download_url": (
                f"/room/{room.room_code}/file/{c.transfer_id}/download?{build_download_query(room.room_code, c.transfer_id)}"
                if has_storage else ""
            ),
        })

    return APIResponse(detail={
        "room_code": room.room_code,
        "save_messages": bool(room.save_messages),
        "cache_enabled": bool(room.cache_enabled),
        "messages": messages,
        "files": files,
    })


def _check_file_type_allowed(file_name: str) -> None:
    """校验扩展名是否在后台文件类型白名单内（空白名单 = 不限制）。

    房间缓存上传与文件柜上传走不同路径，此前不受 fileTypeWhitelist 约束，
    此处复用同一份配置，避免绕过白名单上传到服务器。
    """
    whitelist = str(getattr(settings, "fileTypeWhitelist", "") or "").strip()
    if not whitelist:
        return

    allowed = {e.strip().lower().lstrip(".") for e in whitelist.split(",") if e.strip()}
    name = file_name or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in allowed:
        raise HTTPException(status_code=415, detail=f"不支持的文件类型：{ext or '无扩展名'}")


# 房间级串行锁：同房间的缓存上传串行化，杜绝"先读后写"的配额/去重校验被并发绕过。
# 走集群锁而不是进程内 asyncio.Lock：多实例时每个实例各锁各的，配额会被同时读到旧值。
# 未配置 Redis（单实例）时集群锁自动退化为进程内锁，行为与改造前一致。
def _room_cache_lock(code: str):
    """取房间锁上下文（不存在则创建）。房间数有限，不回收也可接受。"""
    return cluster.hub.lock(f"room-cache:{code}", ttl_ms=30000, wait_ms=20000)


# 跨实例中继不可用提示的节流表：key -> 上次提示时刻（monotonic）。
# 中继按帧循环，不节流会把提示刷成风暴。
_RELAY_HINTED: dict[str, float] = {}
RELAY_HINT_COOLDOWN = 60.0


def _relay_cross_instance_hint(room_code: str, client_id: str) -> bool:
    now = time.monotonic()
    key = f"{room_code}:{client_id}"
    last = _RELAY_HINTED.get(key)
    if last is not None and now - last < RELAY_HINT_COOLDOWN:
        return False
    if len(_RELAY_HINTED) > 4096:
        _RELAY_HINTED.clear()
    _RELAY_HINTED[key] = now
    return True


# 定向发送（file_offer 的 targets）目标数上限：服务端按目标逐个 send_to_client，需收口
MAX_OFFER_TARGETS = 32


def _is_room_member(room_code: str, client_id: str, member_token: str = "") -> bool:
    """判断调用者是否为该房间成员。

    两条通道任一成立即可：
    1. client_id 当前保持本房间 WebSocket 连接（client_id 由服务端签发，无法伪造）
    2. 持有有效的短期成员令牌（welcome 时下发）

    第 2 条是为断线场景兜底：只认"在线"时，用户切后台或弱网重连期间
    就无法删除/管理自己刚上传的文件。令牌短期有效，过期需重新加入房间。
    """
    if not room_code or not client_id:
        return False
    if ws_manager.is_member_online(room_code, client_id):
        return True
    return verify_member_token(room_code, client_id, member_token)


async def _scan_guard(save_path: str, allow_delete: bool = True) -> None:
    """落盘后过一遍病毒扫描钩子；未启用或已通过则直接返回。

    命中恶意内容时抛 403；扫描"没得出结论"时按 virusScanFailOpen 决定
    放行还是拒绝（默认放行并告警）。

    allow_delete=False 用于**秒传复用的共享文件**：那条物理文件还被别的记录
    引用着，本次上传被拦截可以拒绝，但绝不能把它删掉。
    """
    if settings.file_storage != "local" or not save_path:
        # 外部存储拿不到本地文件路径，跳过
        return

    from core.scan import scan_file, should_block_when_scan_failed

    full = str(data_root / save_path)
    clean, detail, ran = await scan_file(full)
    if not ran:
        return
    if clean and not detail:
        return
    def _discard() -> None:
        # 拦截时必须删除已落盘文件，否则被判定为恶意的文件会一直留在磁盘上
        if not allow_delete:
            # 共享文件：不删（别的记录还在引用），只拒绝本次上传
            logger.warning(f"安全扫描命中但文件被多条记录共享，保留文件: {full}")
            return
        try:
            os.remove(full)
        except OSError as e:
            logger.error(f"删除被拦截的上传文件失败 {full}: {e}")

    if not clean:
        logger.warning(f"上传文件被安全扫描拦截: {detail}")
        await asyncio.to_thread(_discard)
        raise HTTPException(status_code=403, detail=f"文件未通过安全扫描：{detail}")
    if should_block_when_scan_failed(detail):
        await asyncio.to_thread(_discard)
        raise HTTPException(status_code=403, detail=f"文件安全扫描未完成：{detail}")


def _new_owner_token() -> str:
    """生成发起者凭证。

    无账号体系下用它标识"这条分享是我发起的"：随取件码一并返回给发起者，
    凭它可提前作废。仅知道取件码的人拿不到，也无法撤销。
    """
    return secrets.token_urlsafe(24)


async def _sha256_saved_file(save_path: str) -> str:
    """计算已落盘文件的 SHA-256（权威值，供接收端校验与秒传复用）。

    分片上传的哈希由 merge_chunks 在合并时流式算出；单文件上传没有这条路径，
    导致 FileCodes.file_hash 为空、接收端拿不到校验基准。这里补上。

    仅在本地存储下计算：外部存储（S3/WebDAV 等）拿不到本地文件路径，
    返回空串即可（前端会跳过校验，不影响功能）。
    """
    if settings.file_storage != "local" or not save_path:
        return ""

    def _calc(full: str) -> str:
        h = hashlib.sha256()
        with open(full, "rb") as fp:
            for chunk in iter(lambda: fp.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    try:
        full = str(data_root / save_path)
        if not os.path.exists(full):
            return ""
        # 同步磁盘 I/O：放进线程避免阻塞事件循环
        return await asyncio.to_thread(_calc, full)
    except Exception as e:
        logger.warning(f"计算文件哈希失败（不影响上传）: {e}")
        return ""


async def _room_cached_bytes(room_code: str, exclude_tid: str = "") -> int:
    """统计房间已落盘缓存的字节数。

    原实现是"取出全部行再逐行累加"，房间文件多时会把整表拉进内存再算一遍；
    配额判断只需要一个总数，交给 SQL 聚合即可。

    exclude_tid 用于排除本次重传的条目本身（重传不重复计入占用）。
    """
    try:
        qs = RoomCachedFile.filter(room_code=room_code).exclude(storage_key="")
        if exclude_tid:
            qs = qs.exclude(transfer_id=exclude_tid)
        rows = await qs.annotate(total=Sum("file_size")).values("total")
        return int(rows[0]["total"] or 0) if rows else 0
    except Exception as e:
        # 聚合失败不能让上传直接 500：退回逐行累加（结果一致，只是慢）
        logger.warning(f"统计房间缓存占用失败，回退逐行累加: {e}")
        used = 0
        for row in await RoomCachedFile.filter(room_code=room_code).all():
            if row.storage_key and row.transfer_id != exclude_tid:
                used += row.file_size or 0
        return used


def _safe_int(value, default: int = 0, low: int = 0, high: int | None = None) -> int:
    """把 WS 消息里的数值字段安全转成 int（并做上下界收敛）。

    消息字段完全由客户端填写，直接 int() 遇到非数字会抛 ValueError；
    而 WS 处理循环只捕获 WebSocketDisconnect，异常会直接中断该连接的处理循环。
    """
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    if num < low:
        return low
    if high is not None and num > high:
        return high
    return num


def _cache_entry_payload(c: RoomCachedFile, room_code: str = "") -> dict:
    """缓存文件统一输出结构（列表/上传响应/广播复用同一份字段）。"""
    return {
        "transfer_id": c.transfer_id,
        "file_name": c.file_name or "",
        "file_size": c.file_size or 0,
        # 服务端落盘时算出的内容哈希：客户端合并多源分片后据此校验完整性
        "file_hash": c.file_hash or "",
        "uploader_name": c.uploader_name or "",
        "owner_id": c.owner_id or "",
        "holders": [h for h in (c.cache_holder_ids or "").split(",") if h],
        "holder_names": [n for n in (c.holder_names or "").split(",") if n],
        # 是否已真实落盘到服务器（为 true 时可点击直接下载，无需 P2P、发送方无需在线）
        "server_cached": bool(c.storage_key),
        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
        "cached_at": c.cached_at.isoformat() if c.cached_at else None,
        # 仅落盘文件下发下载直链，且带短期签名（默认 30 分钟），避免链接被转发后长期可访问
        "download_url": (
            f"/room/{room_code}/file/{c.transfer_id}/download?{build_download_query(room_code, c.transfer_id)}"
            if c.storage_key and room_code
            else ""
        ),
    }


@room_api.get("/{room_code}/files")
async def list_room_cache_files(room_code: str):
    """房间缓存文件列表：含服务器落盘文件（可直接下载）与仅 P2P 持有的文件。"""
    room = await _get_room(room_code)
    cached = await RoomCachedFile.filter(room_code=room.room_code).order_by("-id").limit(200)
    return APIResponse(detail={
        "cache_enabled": bool(room.cache_enabled),
        "server_cache_enabled": bool(getattr(settings, "roomCacheEnabled", 1)),
        "files": [_cache_entry_payload(c, room.room_code) for c in cached],
    })


@room_api.post("/{room_code}/file/upload")
async def upload_room_cache_file(
    room_code: str,
    file: UploadFile = File(...),
    transfer_id: str = Form(""),
    client_id: str = Form(""),
    uploader_name: str = Form(""),
    cache_hours: int = Form(0),
    file_hash: str = Form(""),
    member_token: str = Form(""),
    ip: str = Depends(ip_limit["upload"]),
):
    """上传文件到房间服务器缓存（双通道中的"落盘"通道）。

    cache_hours=0 表示跟随房间生命周期（随房间过期一起清理）；>0 表示指定小时数后过期。
    默认通道仍是 P2P 直连，只有发送方主动选择"缓存到服务器"时才走这里。
    """
    room = await _get_room(room_code)

    if not getattr(settings, "roomCacheEnabled", 1):
        raise HTTPException(status_code=403, detail="服务器文件缓存未开启")
    if not room.cache_enabled:
        raise HTTPException(status_code=403, detail="该房间未启用文件缓存")

    # 复用文件柜的文件类型白名单（房间缓存此前走独立路径，不受该限制约束）
    _check_file_type_allowed(file.filename or "")
    # 鉴权：必须是本房间成员（当前在线，或持有有效的短期成员令牌）。
    # client_id 由服务端签发、令牌用应用密钥签名，未加入房间者都无法取得。
    # 此前无任何校验，任何拿到房间码的人都能往服务器磁盘写文件并耗尽配额。
    uploader_id = (client_id or "").strip()[:64]
    if not _is_room_member(room.room_code, uploader_id, (member_token or "").strip()):
        raise HTTPException(status_code=403, detail="请先加入房间再上传")

    # 同房间的配额校验 + 去重判定 + 落盘 + 登记必须在同一把锁内完成，
    # 否则并发上传会各自读到旧的 used 值，配额上限形同虚设。
    async with _room_cache_lock(room.room_code):

        tid = (transfer_id or "").strip()[:64] or uuid.uuid4().hex[:16]
        now = await get_now()
        # 客户端声明的 file_hash 仅作参考日志，不再参与去重判定：
        # 该值可被伪造，会让内容不同的文件被判为重复（缓存投毒）。
        # 下方一律使用落盘时流式算出的服务端权威哈希。
        declared_hash = (file_hash or "").strip().lower()[:64]

        limit_mb = int(getattr(settings, "roomCacheMaxSize", 0) or 0)
        max_bytes = limit_mb * 1024 * 1024 if limit_mb > 0 else 0

        # ===== 房间总容量配额：写前按声明大小预估，落盘后再按实际大小复核 =====
        quota_mb = int(getattr(settings, "roomCacheRoomQuota", 0) or 0)
        used = await _room_cached_bytes(room.room_code, exclude_tid=tid)
        if quota_mb > 0:
            declared_size = int(file.size or 0)
            if used + declared_size > quota_mb * 1024 * 1024:
                logger.warning(
                    f"房间缓存配额不足(预估): code={room.room_code} used={used} "
                    f"incoming={declared_size} quota={quota_mb}MB"
                )
                raise HTTPException(status_code=413, detail="房间缓存空间已满，请先删除部分文件")

        # ===== 先落临时文件，写入过程中流式算出内容哈希 =====
        tmp_key = "_incoming/" + build_storage_key(room.room_code, tid, file.filename or "file")
        incoming_size, digest = await save_room_file(tmp_key, file, max_bytes)
        if declared_hash and declared_hash != digest:
            logger.warning(
                f"客户端声明哈希与内容不符，以服务端为准: code={room.room_code} "
                f"declared={declared_hash[:12]} actual={digest[:12]}"
            )

        # ===== 去重：按服务端权威哈希查同房间已落盘且未过期的文件 =====
        # 文件消息本身就是缓存记录（room_message.msg_type=file），因此用它做查重即可。
        reused: RoomMessage | None = None
        if digest:
            candidate = (
                await RoomMessage.filter(
                    room_code=room.room_code, msg_type="file", file_hash=digest
                )
                .exclude(storage_key="")
                .order_by("-id")
                .first()
            )
            if candidate and candidate.storage_key:
                not_expired = candidate.expires_at is None or candidate.expires_at > now
                # 元数据声称已落盘，磁盘上却可能已被删除（如后台强制清理）：
                # 只有文件真实存在才允许复用，否则视为未命中、走正常落盘，
                # 避免返回一个指向空路径的"可下载"条目
                on_disk = resolve_path(candidate.storage_key)
                if not_expired and on_disk is not None and on_disk.is_file():
                    reused = candidate
                elif not_expired:
                    logger.warning(
                        f"房间缓存去重候选已丢失，改为重新落盘: "
                        f"code={room.room_code} key={candidate.storage_key}"
                    )

        if reused is not None:
            # 命中已有缓存：弃用刚写的临时文件，复用既有落盘、不额外占盘
            await delete_room_file(tmp_key)
            storage_key = reused.storage_key
            size = reused.file_size or 0
            deduped = True
            logger.info(f"房间文件去重命中: code={room.room_code} hash={digest[:12]} 复用 {storage_key}")
        else:
            # 复核配额（按实际写入大小，声明大小可能缺失或被低估）
            if quota_mb > 0 and used + incoming_size > quota_mb * 1024 * 1024:
                await delete_room_file(tmp_key)
                logger.warning(
                    f"房间缓存配额不足(实际): code={room.room_code} used={used} "
                    f"incoming={incoming_size} quota={quota_mb}MB"
                )
                raise HTTPException(status_code=413, detail="房间缓存空间已满，请先删除部分文件")
            storage_key = build_storage_key(room.room_code, tid, file.filename or "file")
            await move_room_file(tmp_key, storage_key)
            size = incoming_size
            deduped = False

        expires_at = None
        if cache_hours and int(cache_hours) > 0:
            hours = max(1, min(int(cache_hours), 24 * 30))
            expires_at = now + timedelta(hours=hours)

        if reused is not None:
            # 复用场景：过期时间"取较晚的"（续期），不缩短已有缓存的有效期
            if expires_at and (reused.expires_at is None or expires_at > reused.expires_at):
                reused.expires_at = expires_at
                await reused.save(update_fields=["expires_at"])
            expires_at = reused.expires_at

        entry = await RoomCachedFile.filter(room_code=room.room_code, transfer_id=tid).first()
        # 上传者自己也持有该文件（他本地就有原始文件）：一并登记为缓存来源，
        # 否则 holders 永远为空 —— 发送方明明在线，别人却被告知"暂无可用的在线缓存来源"。
        holders = {h for h in ((entry.cache_holder_ids or "").split(",") if entry else []) if h}
        holder_names = {n for n in ((entry.holder_names or "").split(",") if entry else []) if n}
        if client_id:
            holders.add(client_id)
            holder_names.add((uploader_name or "昵称")[:64])

        if entry:
            # 覆盖前回收旧落盘文件：storage_key 由 (room, tid, filename) 决定，同 tid
            # 换名重传会落到新路径，旧文件将无人引用（forever 房间下永久泄漏）。
            #
            # 但去重会让多个条目共用同一份落盘（不同 tid、相同内容 → 同一 storage_key），
            # 因此删除前必须确认没有别的条目还在引用它，否则会把别人的缓存文件删掉，
            # 让那些条目的下载变成 404「缓存文件已丢失」。
            old_key = (entry.storage_key or "").strip()
            if old_key and old_key != storage_key:
                others = await RoomCachedFile.filter(
                    room_code=room.room_code, storage_key=old_key
                ).exclude(id=entry.id).exists()
                if not others:
                    others = await RoomMessage.filter(
                        room_code=room.room_code, storage_key=old_key
                    ).exclude(content=tid).exists()
                if not others:
                    await delete_room_file(old_key)
                else:
                    logger.info(
                        f"旧落盘文件仍被其他条目引用，保留: code={room.room_code} key={old_key}"
                    )
            entry.storage_key = storage_key
            entry.file_name = (file.filename or "")[:255]
            entry.file_size = size
            entry.file_hash = digest
            entry.uploader_name = (uploader_name or "")[:64]
            entry.owner_id = (client_id or "")[:64]
            entry.cached_at = now
            entry.expires_at = expires_at
            entry.cache_holder_ids = ",".join(sorted(holders))
            entry.holder_names = ",".join(sorted(holder_names))
            await entry.save()
        else:
            try:
                entry = await RoomCachedFile.create(
                    room_code=room.room_code,
                    transfer_id=tid,
                    owner_id=(client_id or "")[:64],
                    file_name=(file.filename or "")[:255],
                    file_size=size,
                    file_hash=digest,
                    uploader_name=(uploader_name or "")[:64],
                    storage_key=storage_key,
                    cached_at=now,
                    expires_at=expires_at,
                    cache_holder_ids=",".join(sorted(holders)),
                    holder_names=",".join(sorted(holder_names)),
                )
            except Exception as e:
                # 唯一索引兜底：(room_code, transfer_id) 已被并发创建（多进程部署）
                # 时退回取用既有记录，避免直接 500
                logger.warning(f"房间缓存记录创建冲突，回退取用: code={room.room_code} tid={tid} {e}")
                entry = await RoomCachedFile.filter(
                    room_code=room.room_code, transfer_id=tid
                ).first()
                if entry is None:
                    raise HTTPException(status_code=409, detail="缓存记录创建冲突，请重试")

        # ===== 消息与文件消息合一 =====
        # 文件在 room_message 里同样占一条记录（msg_type=file），并直接挂上缓存元数据
        # （storage_key / expires_at / file_hash）。历史展示只读这一张表，
        # 不需要再把 room_cached_file 关联进来（那张表只留作 P2P 多源持有者索引）。
        file_msg = await RoomMessage.filter(
            room_code=room.room_code, msg_type="file", content=tid
        ).first()
        if file_msg is None:
            file_msg = await RoomMessage.create(
                room_code=room.room_code,
                sender=(uploader_name or "匿名")[:64],
                content=tid,
                msg_type="file",
                file_name=(file.filename or "")[:255],
                file_size=size,
                file_hash=digest,
                storage_key=storage_key,
                expires_at=expires_at,
            )
        else:
            file_msg.sender = (uploader_name or file_msg.sender or "匿名")[:64]
            file_msg.file_name = (file.filename or "")[:255]
            file_msg.file_size = size
            if digest:
                file_msg.file_hash = digest
            file_msg.storage_key = storage_key
            file_msg.expires_at = expires_at
            await file_msg.save()

        payload = _cache_entry_payload(entry, room.room_code)
        payload["deduped"] = deduped
        await record("room_cache_upload", target_type="room", target_id=room.room_code,
                     result="success", detail=entry.file_name or "", ip=ip)
        # 广播给房间成员，实时更新"缓存文件"入口列表
        await ws_manager.broadcast(room.room_code, {"type": "room_cache_added", "file": payload})
        return APIResponse(detail=payload)


@room_api.get("/{room_code}/file/{transfer_id}/link")
async def get_room_cache_download_link(room_code: str, transfer_id: str):
    """换取一条新的下载直链。

    列表里下发的链接签名只有 30 分钟有效期，页面停留久了再点下载就会报
    "下载链接无效或已过期"。点击下载时先调本接口换新链接即可避免。
    """
    room = await _get_room(room_code)
    entry = await RoomCachedFile.filter(
        room_code=room.room_code, transfer_id=transfer_id
    ).first()
    if not entry or not entry.storage_key:
        raise HTTPException(status_code=404, detail="文件未缓存到服务器")
    if entry.expires_at and entry.expires_at < await get_now():
        raise HTTPException(status_code=410, detail="缓存已过期")

    return APIResponse(detail={
        "download_url": (
            f"/room/{room.room_code}/file/{transfer_id}/download?"
            f"{build_download_query(room.room_code, transfer_id)}"
        ),
    })


@room_api.get("/{room_code}/file/{transfer_id}/download")
async def download_room_cache_file(
    request: Request, room_code: str, transfer_id: str, exp: str = "", sig: str = ""
):
    """直接下载房间服务器缓存文件（无需 P2P、发送方无需在线）。

    链接带短期签名（exp/sig，由列表接口下发，默认 30 分钟有效）；签名无效或过期即拒绝，
    避免仅凭"房间码 + transfer_id"被永久访问或转发。
    """
    if not verify_download(room_code, transfer_id, exp, sig):
        raise HTTPException(status_code=403, detail="下载链接无效或已过期，请重新打开缓存文件列表")

    room = await _get_room(room_code)
    entry = await RoomCachedFile.filter(
        room_code=room.room_code, transfer_id=transfer_id
    ).first()
    if not entry or not entry.storage_key:
        raise HTTPException(status_code=404, detail="文件未缓存到服务器")

    if entry.expires_at and entry.expires_at < await get_now():
        raise HTTPException(status_code=410, detail="缓存已过期")

    path = resolve_path(entry.storage_key)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="缓存文件已丢失")

    file_size = path.stat().st_size
    quoted = quote(entry.file_name or "file")
    base_headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        "Accept-Ranges": "bytes",
    }

    # 断点续传：支持 HTTP Range（单区间）。大文件下载中断后可从断点继续，
    # 不必重头开始（浏览器/下载工具的中断续传依赖此能力）。
    rng = parse_range_header(request.headers.get("range") if request else None, file_size)
    if rng:
        start, end = rng
        return StreamingResponse(
            iter_file_range(path, start, end),
            status_code=206,
            media_type="application/octet-stream",
            headers={
                **base_headers,
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(end - start + 1),
            },
        )

    return StreamingResponse(
        iter_file_range(path, 0, max(0, file_size - 1)),
        media_type="application/octet-stream",
        headers={**base_headers, "Content-Length": str(file_size)},
    )


@room_api.delete("/{room_code}/file/{transfer_id}")
async def delete_room_cache_file(room_code: str, transfer_id: str, client_id: str = "", member_token: str = ""):
    """删除房间缓存文件：落盘的会删除磁盘数据，同时移除 P2P 持有者记录。

    鉴权：client_id 必填，且必须是本房间成员（当前在线 或 持有有效成员令牌）。
    client_id 由服务端在 WS join 时签发，未加入房间者无从得知。
    此前 client_id 可缺省，校验被整段跳过 —— 任何匿名请求都能
    删掉任意房间的缓存文件（含服务器磁盘数据）。
    """
    room = await _get_room(room_code)
    client_id = (client_id or "").strip()[:64]
    if not _is_room_member(room.room_code, client_id, (member_token or "").strip()):
        raise HTTPException(status_code=403, detail="请先加入房间再操作")

    entry = await RoomCachedFile.filter(
        room_code=room.room_code, transfer_id=transfer_id
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="文件不存在")

    if entry.owner_id and entry.owner_id != client_id:
        raise HTTPException(status_code=403, detail="只有上传者可以删除该文件")

    if entry.storage_key:
        await delete_room_file(entry.storage_key)
    await entry.delete()

    # 同步清空历史里同名文件消息的落盘标记：否则历史仍显示"可下载"，点击却 404
    await RoomMessage.filter(
        room_code=room.room_code, msg_type="file", content=transfer_id
    ).update(storage_key="")

    await ws_manager.broadcast(room.room_code, {
        "type": "room_cache_removed",
        "transfer_id": transfer_id,
    })
    await record("room_cache_delete", target_type="room", target_id=room.room_code,
                 result="success", detail=transfer_id)
    return APIResponse()


# ============ 实时房间WebSocket端点（统一） ============
# /ws/room 与旧路径 /ws/chat、/ws/transfer、/ws/direct 等价（兼容已分享链接）
# 聊天消息不落库、无历史接口：文本仅在内存中实时转发（服务器不保存记录）


@ws_api.websocket("/ws/room/{room_code}")
@ws_api.websocket("/ws/direct/{room_code}")
@ws_api.websocket("/ws/transfer/{room_code}")
async def websocket_room(websocket: WebSocket, room_code: str):
    """实时房间 WebSocket：聊天 + 文件传输 + P2P 媒体统一（服务器中转兜底，文本/媒体不落盘）"""
    room = await Room.filter(room_code=_validate_code(room_code)).first()
    if not room:
        await websocket.close(code=404, reason="房间不存在")
        return
    if await room.is_expired():
        await websocket.close(code=410, reason="房间已过期")
        return

    # 接受连接并加入房间：client_id 由服务端签发（客户端自报的 id 仅在未被占用时允许沿用，
    # 用于断线重连复用身份；占用中则重新签发，防止冒用他人 id）
    client_id = await ws_manager.connect(room.room_code, websocket, nickname="")
    # connect 阶段签发的 id：客户端若沿用持久 id，需要把这条临时身份从集群里撤销
    issued_id = client_id
    ws_conn = next(
        (c for c in ws_manager.active_connections.get(room.room_code, []) if c["ws"] is websocket),
        None,
    )

    try:
        # 第一条消息作为昵称注册
        first_msg = await websocket.receive_text()
        nickname = ""
        try:
            msg_data = json.loads(first_msg)
            if isinstance(msg_data, dict) and msg_data.get("type") == "join":
                nickname = (msg_data.get("nickname") or "")[:50]
                # 客户端请求沿用上次签发的 client_id（断线重连复用同一身份）：
                # 仅在房间内未被占用时允许（connect 已保证唯一性），且由服务端重新绑定
                persisted_id = str(msg_data.get("client_id") or "").strip()[:64]
                if ws_conn and persisted_id and ws_manager._is_client_id_available(room.room_code, persisted_id):
                    ws_conn["client_id"] = persisted_id
                    client_id = persisted_id
                if ws_conn:
                    ws_conn["nickname"] = nickname
                # 同 id 旧连接（断线残留/多标签页/其它实例上的同名连接）→ 顶替并关闭
                await ws_manager.replace_client(room.room_code, client_id, keep_ws=websocket)
                # 沿用了持久 id 时，connect 阶段签发的临时 id 已广播出去，必须撤销
                if client_id != issued_id:
                    await ws_manager.drop_presence(room.room_code, issued_id)
                # 昵称此时才确定，同步给其它实例（否则远端成员显示"匿名"）
                await ws_manager.refresh_presence(room.room_code, client_id, nickname)
        except (json.JSONDecodeError, ValueError):
            pass

        # 房间人数上限校验：超过 max_members 拒绝加入（人数含其它实例上的成员，
        # 否则多实例下每个实例各自放行 max_members 个，上限被放大 N 倍）
        if room.max_members > 0 and ws_manager.room_size(room.room_code) > room.max_members:
            await ws_manager.send_personal(room.room_code, websocket, {"type": "room_full"})
            await websocket.close(code=1013, reason="房间已满")
            return

        # 消息发送限频状态（每连接独立：5 秒滑窗内最多 10 条；typing 1.5 秒最多 1 条）
        msg_times = deque()
        last_typing_ts = 0.0

        # 先下发完整 TURN 凭据（仅房间成员可见；公共配置接口已剥离 username/credential），
        # 再发 welcome —— 前端在收到 welcome 后立即触发 P2P 建连（maybeInitP2P），
        # 保证建连时 rtcConfig 已包含完整 TURN 凭据（避免使用旧配置发起协商）
        turn_servers = getattr(settings, "directTurnServers", []) or []
        if turn_servers:
            await ws_manager.send_personal(room.room_code, websocket, {
                "type": "turn_servers",
                "servers": [
                    {
                        "urls": s.get("urls"),
                        "username": s.get("username", ""),
                        "credential": s.get("credential", ""),
                    }
                    for s in turn_servers
                    if isinstance(s, dict) and s.get("urls")
                ],
            })

        # 告知本连接其 client_id（用于文件确认/媒体协商的定向回复）
        # member_token：短期成员凭证，供 HTTP 接口（缓存上传/删除）在断线后仍能证明成员身份
        await ws_manager.send_personal(room.room_code, websocket, {
            "type": "welcome",
            "client_id": client_id,
            "member_token": issue_member_token(room.room_code, client_id),
        })

        # 房间启用文件缓存：新加入者收到已缓存文件列表（有缓存持有者的可收取）
        if room.cache_enabled:
            try:
                cached = await RoomCachedFile.filter(room_code=room.room_code).all()
                cached_list = [
                    {
                        "transfer_id": c.transfer_id,
                        "file_name": c.file_name,
                        "file_size": c.file_size,
                        "file_hash": c.file_hash or "",
                        "holders": [h for h in (c.cache_holder_ids or "").split(",") if h],
                        "holder_names": [n for n in (c.holder_names or "").split(",") if n],
                        # 服务器已落盘 → 前端可直接下载，不必依赖在线成员
                        "server_cached": bool(c.storage_key),
                        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                        "download_url": (
                            f"/room/{room.room_code}/file/{c.transfer_id}/download?"
                            f"{build_download_query(room.room_code, c.transfer_id)}"
                            if c.storage_key else ""
                        ),
                    }
                    for c in cached
                ]
                await ws_manager.send_personal(room.room_code, websocket, {
                    "type": "file_cache_list",
                    "files": cached_list,
                })
            except Exception:
                pass

        # 广播用户加入（含成员列表，供多人场景识别彼此）
        await ws_manager.broadcast(room.room_code, {
            "type": "user_joined",
            "nickname": nickname,
            "online_users": ws_manager.get_online_users(room.room_code),
            "members": ws_manager.get_members(room.room_code),
        })

        # 当前正在中继的传输 id：二进制帧本身不带 id（省帧开销），
        # 由紧随其前的 file_chunk 文本帧告知，这里记下来供"跨实例无法中继"提示使用
        active_transfer_id = ""
        while True:
            message = await websocket.receive()
            mtype = message.get("type")
            if mtype == "websocket.disconnect":
                break
            if "bytes" in message:
                # 二进制分片中继（不落盘）：转发给发送者的所有目标接收者
                # （文件传输单目标；媒体中转广播：一个共享者可被多个查看者订阅）
                data = message["bytes"]
                if len(data) > ROOM_RELAY_MAX_FRAME:
                    continue
                target_ids = ws_manager.get_file_targets(room.room_code, client_id)
                if target_ids:
                    delivered = False
                    for target_id in target_ids:
                        if await ws_manager.send_bytes_to_client(room.room_code, target_id, data):
                            delivered = True
                    if not delivered and getattr(cluster.hub, "distributed", False):
                        # 多实例下接收方可能挂在别的进程。二进制帧刻意不走 pub/sub
                        # （单帧最大 256KB，base64 后吞吐放大 1.4 倍、延迟不可控），
                        # 正确做法是接入层按房间做会话亲和。这里明确告知发送方，
                        # 而不是让文件传到一半静默卡住。
                        if _relay_cross_instance_hint(room.room_code, client_id):
                            await ws_manager.send_to_client(room.room_code, client_id, {
                                "type": "relay_failed",
                                "transfer_id": active_transfer_id,
                                "reason": "接收方不在当前实例，多实例部署需按房间做会话亲和",
                            })
                    # 中转限速（KB/s，0 表示不限速）
                    relay_limit = getattr(settings, "directRelaySpeedLimit", 0) or 0
                    if relay_limit > 0:
                        await asyncio.sleep(len(data) / (relay_limit * 1024))
                continue
            if "text" not in message:
                continue
            try:
                msg = json.loads(message["text"])
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")

            # ============ 聊天 ============
            if mtype == "chat_message":
                content = (msg.get("content") or "").strip()
                if not content:
                    continue
                if len(content.encode("utf-8")) > ROOM_MESSAGE_MAX_LEN:
                    await ws_manager.send_personal(room.room_code, websocket, {
                        "type": "chat_message_error",
                        "message": "消息过长，请分段发送",
                    })
                    continue
                # 发送限频：5 秒滑窗内最多 10 条，超限提示并丢弃
                now_ts = time.time()
                while msg_times and now_ts - msg_times[0] > 5:
                    msg_times.popleft()
                if len(msg_times) >= 10:
                    await ws_manager.send_personal(room.room_code, websocket, {
                        "type": "chat_message_error",
                        "message": "发送过于频繁，请稍后再试",
                        "client_id": msg.get("client_id") or "",
                    })
                    continue
                msg_times.append(now_ts)
                # save_messages=1：聊天消息落库（供房间过期后清理；实时仍只经内存广播）
                if room.save_messages:
                    try:
                        await RoomMessage.create(
                            room_code=room.room_code,
                            sender=nickname or "匿名",
                            content=content,
                        )
                    except Exception:
                        pass
                # 聊天消息广播给所有连接（含发送者，回显按 client_id 匹配）
                await ws_manager.broadcast(room.room_code, {
                    "type": "chat_message",
                    "sender": nickname or "匿名",
                    "content": content,
                    "ts": int(time.time() * 1000),
                    "client_id": msg.get("client_id") or "",
                })
            elif mtype == "typing":
                # typing 节流：每连接 1.5 秒最多广播 1 条，防止刷屏
                now_ts = time.time()
                if now_ts - last_typing_ts < 1.5:
                    continue
                last_typing_ts = now_ts
                await ws_manager.broadcast(room.room_code, {
                    "type": "typing",
                    "nickname": nickname or "匿名",
                    "is_typing": bool(msg.get("is_typing", False)),
                })

            # ============ 文件传输 / 媒体信令 ============
            if mtype == "file_offer":
                # 文件发送请求 → 广播给其他所有成员（附上发送者 client_id 供对方回复）
                # save_messages=1：把传输文件元信息落库，供房间历史文件展示（msg_type=file）
                if room.save_messages and msg.get("transfer_id"):
                    try:
                        await RoomMessage.create(
                            room_code=room.room_code,
                            sender=nickname or "匿名",
                            content=str(msg.get("transfer_id") or "")[:64],
                            msg_type="file",
                            file_name=str(msg.get("file_name") or "")[:255],
                            file_size=int(msg.get("file_size") or 0),
                        )
                    except Exception:
                        pass
                offer_payload = {
                    "type": "file_offer",
                    "transfer_id": msg.get("transfer_id") or "",
                    "file_name": msg.get("file_name") or "",
                    "file_size": msg.get("file_size") or 0,
                    "sender": nickname or "匿名",
                    "from_id": client_id,
                }
                # 上限收口：服务端按 targets 逐个 send_to_client，不设限可被一条消息放大
                targets = [
                    t for t in (msg.get("targets") or []) if isinstance(t, str) and t
                ][:MAX_OFFER_TARGETS]
                if targets:
                    # 定向发送：只投递给指定成员。房间内无关成员不再收到文件卡片——
                    # 既省带宽，也避免把文件名暴露给未参与传输的成员。
                    for target_id in targets:
                        await ws_manager.send_to_client(room.room_code, target_id, offer_payload)
                else:
                    # 未指定 targets（旧客户端兼容 / 群发语义）→ 广播给其他所有成员
                    await ws_manager.broadcast_exclude(room.room_code, websocket, offer_payload)
            # WebRTC 信令（P2P 直连协商）→ 定向转发给目标接收者（附上发送者 client_id）
            # crypto_setup：中继分片端到端加密的 ECDH 公钥交换，同样定向转发（密文与密钥均不经过服务端）
            # media_offer/media_answer：传屏幕/传视频的媒体协商信令，接收者主动拉流 → 定向转发给共享者
            # media_subscribe：查看者请求服务器中转观看（P2P 不可用时）→ 定向转发给共享者
            elif mtype in ("rtc_offer", "rtc_answer", "rtc_ice", "crypto_setup",
                           "media_offer", "media_answer", "media_subscribe"):
                target = msg.get("target") or ""
                if target:
                    await ws_manager.send_to_client(room.room_code, target, {
                        **msg,
                        "from_id": client_id,
                    })
            # media_available：共享者广播"正在共享屏幕/视频"（通知其他成员，供其决定是否查看）
            # media_cancel：共享者广播"共享已结束"（通知其他成员清理）
            elif mtype in ("media_available", "media_cancel"):
                await ws_manager.broadcast_exclude(room.room_code, websocket, {
                    **msg,
                    "from_id": client_id,
                })
            # media_unsubscribe：查看者退出中转观看 → 通知共享者，并从房间转发目标中移除该查看者，
            # 保证其再次订阅时转发目标干净重建（否则共享者仍向已退出的查看者发分片，且重订后目标集可能残留）
            elif mtype == "media_unsubscribe":
                target = msg.get("target") or ""
                if target:
                    await ws_manager.send_to_client(room.room_code, target, {
                        "type": "media_unsubscribe",
                        "from_id": client_id,
                    })
                    ws_manager.remove_file_target_receiver(room.room_code, client_id)
            elif mtype == "file_response":
                # 对方接受/拒绝 → 定向回复给发送者
                target = msg.get("target") or ""
                if target:
                    await ws_manager.send_to_client(room.room_code, target, {
                        "type": "file_response",
                        "transfer_id": msg.get("transfer_id") or "",
                        "accept": bool(msg.get("accept", False)),
                        "from_id": client_id,
                        "nickname": nickname or "匿名",
                    })
            elif mtype == "file_start":
                # 开始传输 → 记录定向目标并通知该接收者（中转受后台开关限制）
                target = msg.get("target") or ""
                if target:
                    if getattr(settings, "directRelayEnabled", 1) == 0:
                        await ws_manager.send_personal(room.room_code, websocket, {
                            "type": "file_cancel",
                            "transfer_id": msg.get("transfer_id") or "",
                            "message": "relay_disabled",
                        })
                        continue
                    # 媒体中转（mode=media-relay）：多目标追加（一个共享者可被多个查看者订阅）
                    is_media = (msg.get("mode") or "") == "media-relay"
                    if is_media:
                        ws_manager.add_file_target(room.room_code, client_id, target)
                    else:
                        ws_manager.set_file_target(room.room_code, client_id, target)
                    await ws_manager.send_to_client(room.room_code, target, {
                        "type": "file_start",
                        "transfer_id": msg.get("transfer_id") or "",
                        "file_name": msg.get("file_name") or "",
                        "file_size": msg.get("file_size") or 0,
                        "sender": nickname or "匿名",
                        "mode": msg.get("mode") or "relay",
                        "media_mime": msg.get("media_mime") or "",
                        # 媒体中转多摄像头：透传摄像头索引（接收端据此路由到对应 MediaSource 分路）
                        "camera_idx": msg.get("camera_idx") or 0,
                        "from_id": client_id,
                    })
            elif mtype == "file_chunk":
                # 分片头（接收端据此把紧随的二进制帧路由到对应传输任务）
                # 媒体中转：广播给该发送者的全部目标；文件传输：单目标
                active_transfer_id = msg.get("transfer_id") or active_transfer_id
                for target in ws_manager.get_file_targets(room.room_code, client_id):
                    await ws_manager.send_to_client(room.room_code, target, {
                        "type": "file_chunk",
                        "transfer_id": msg.get("transfer_id") or "",
                        "index": msg.get("index") or 0,
                    })
            elif mtype in ("file_end", "file_cancel"):
                # 结束/取消传输 → 清除定向目标并通知接收者（file_end 附带完整性校验哈希）
                target = msg.get("target") or ""
                if target:
                    ws_manager.clear_file_target(room.room_code, client_id)
                    await ws_manager.send_to_client(room.room_code, target, {
                        "type": mtype,
                        "transfer_id": msg.get("transfer_id") or "",
                        "file_hash": msg.get("file_hash") or "",
                        "mode": msg.get("mode") or "",
                        # 媒体中转多摄像头：透传摄像头索引（该路结束后只关闭对应 MediaSource 分路）
                        "camera_idx": msg.get("camera_idx") or 0,
                        "from_id": client_id,
                    })
                else:
                    # 无 target（媒体中转共享者停共享）：清除该发送者的全部目标并逐个通知
                    targets = ws_manager.get_file_targets(room.room_code, client_id)
                    ws_manager.clear_file_target(room.room_code, client_id)
                    for t in targets:
                        await ws_manager.send_to_client(room.room_code, t, {
                            "type": mtype,
                            "transfer_id": msg.get("transfer_id") or "",
                            "file_hash": msg.get("file_hash") or "",
                            "mode": msg.get("mode") or "",
                            "camera_idx": msg.get("camera_idx") or 0,
                            "from_id": client_id,
                        })
            elif mtype == "file_error":
                # 接收侧错误通知（如完整性校验失败）→ 定向转发给发送者
                target = msg.get("target") or ""
                if target:
                    await ws_manager.send_to_client(room.room_code, target, {
                        "type": "file_error",
                        "transfer_id": msg.get("transfer_id") or "",
                        "reason": msg.get("reason") or "",
                    })

            # ============ 房间文件缓存（cache_enabled） ============
            elif mtype == "file_cache_ready":
                # 接收者完成接收并持有了文件 → 记录缓存（该 client_id 加入持有者集合，并记昵称）
                if room.cache_enabled:
                    transfer_id = (msg.get("transfer_id") or "")[:64]
                    if transfer_id:
                        cached = await RoomCachedFile.filter(
                            room_code=room.room_code, transfer_id=transfer_id
                        ).first()
                        holders = set(
                            h for h in ((cached.cache_holder_ids or "").split(",") if cached else []) if h
                        )
                        holder_names = set(
                            n for n in ((cached.holder_names or "").split(",") if cached else []) if n
                        )
                        holders.add(client_id)
                        holder_names.add(nickname or "匿名")
                        if cached:
                            cached.cache_holder_ids = ",".join(holders)
                            cached.holder_names = ",".join(holder_names)
                            await cached.save(update_fields=["cache_holder_ids", "holder_names"])
                        else:
                            await RoomCachedFile.create(
                                room_code=room.room_code,
                                transfer_id=transfer_id,
                                owner_id=(msg.get("owner_id") or "")[:64],
                                file_name=(msg.get("file_name") or "")[:255],
                                file_size=int(msg.get("file_size") or 0),
                                cache_holder_ids=",".join(holders),
                                holder_names=",".join(holder_names),
                            )
                        # 广播缓存列表更新（其他成员可从此缓存源收取）
                        await ws_manager.broadcast(room.room_code, {
                            "type": "file_cache_update",
                            "transfer_id": transfer_id,
                            "file_name": (msg.get("file_name") or "")[:255],
                            "file_size": int(msg.get("file_size") or 0),
                            "holder_id": client_id,
                            "holder_nickname": nickname or "匿名",
                        })
            elif mtype == "file_cache_request":
                # 成员请求从某个缓存持有者收取文件 → 通知持有者（发起 file_offer 给请求者）
                # 支持多源加速分片：part_id + range_start/end 时，持有者只需重发该字节区间
                if room.cache_enabled:
                    holder = (msg.get("holder_id") or "")[:64]
                    if holder:
                        part_id = (msg.get("part_id") or "")[:64]
                        r_start = _safe_int(msg.get("range_start"), 0, 0)
                        r_end = _safe_int(msg.get("range_end"), 0, 0)
                        # 分片区间非法（含 start > end 的退化区间，如 (0,0)）→ 直接忽略。
                        # 放行会让持有端退化成"以分片 id 发送整个文件"：
                        # 合并必然失败，且可被反复触发造成带宽放大。
                        if part_id and r_end < r_start:
                            logger.warning(
                                f"房间缓存分片区间非法，忽略请求: code={room.room_code} "
                                f"part={part_id} [{r_start}, {r_end}]"
                            )
                        else:
                            await ws_manager.send_to_client(room.room_code, holder, {
                                "type": "file_cache_request",
                                "transfer_id": (msg.get("transfer_id") or "")[:64],
                                "file_name": (msg.get("file_name") or "")[:255],
                                "file_size": _safe_int(msg.get("file_size"), 0, 0),
                                "requester_id": client_id,
                                "requester_nickname": nickname or "匿名",
                                # 多源分片（可选）：part_id 为该分片传输 id，range 为请求的字节区间
                                "part_id": part_id,
                                "range_start": r_start,
                                "range_end": r_end,
                            })
    except WebSocketDisconnect:
        pass
    finally:
        left_nickname = await ws_manager.disconnect(room.room_code, websocket)
        # 广播用户离开（附 from_id，供其余成员清理该离开者的共享/媒体）
        await ws_manager.broadcast(room.room_code, {
            "type": "user_left",
            "nickname": left_nickname,
            "online_users": ws_manager.get_online_users(room.room_code),
            "members": ws_manager.get_members(room.room_code),
            "from_id": client_id,
        })
        # 该用户若正在共享屏幕/视频，连接断开（刷新页面/断网/异常退出）时广播"共享已结束"，
        # 否则接收方收不到 media_cancel，仍残留该共享者的视频窗口/中转媒体源。
        # 接收方 handleMediaCancel 对无共享的用户是幂等清理，广播无副作用。
        if client_id:
            await ws_manager.broadcast_exclude(room.room_code, websocket, {
                "type": "media_cancel",
                "from_id": client_id,
            })
        # 次数模式：房间从"有人"变为"无人"计一次会话；次数扣尽后房间过期。
        # 判定用 room_size（含其它实例成员），否则多实例下每个实例都以为"最后一个人走了"，
        # 一次会话被扣 N 次。
        if room.expire_style == "count" and ws_manager.room_size(room.room_code) == 0:
            await Room.filter(id=room.id, expired_count__gt=0).update(
                expired_count=F("expired_count") - 1
            )


# ============ 兼容旧链接的 REST 入口（内部统一到 Room 表） ============
compat_room_api = APIRouter(tags=["实时房间(兼容)"])


@compat_room_api.post("/transfer/create/", dependencies=[Depends(share_required_login)])
async def compat_create_transfer(data: CreateRoomRequest, ip: str = Depends(ip_limit["upload"])):
    """旧传输房间入口：创建直连快传房间（统一模式，兼容旧链接）"""
    room = await _create_room(data, ip)
    return APIResponse(detail=await _build_room_response(room))


@compat_room_api.post("/direct/create/", dependencies=[Depends(share_required_login)])
async def compat_create_direct(data: CreateRoomRequest, ip: str = Depends(ip_limit["upload"])):
    """旧直连快传入口：创建直连快传房间（统一模式）"""
    room = await _create_room(data, ip)
    return APIResponse(detail=await _build_room_response(room))


@compat_room_api.get("/transfer/info/{room_code}")
async def compat_transfer_info(room_code: str):
    room = await _get_room(room_code)
    return APIResponse(detail=await _build_room_response(room))


@compat_room_api.get("/direct/info/{room_code}")
async def compat_direct_info(room_code: str):
    room = await _get_room(room_code)
    return APIResponse(detail=await _build_room_response(room))
