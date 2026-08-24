import asyncio
import datetime
import hashlib
import json
import logging
import os
import time
import uuid
from collections import deque
from datetime import timedelta
from urllib.parse import quote, unquote

from typing import Optional, Tuple, Union, List

from fastapi import APIRouter, Form, UploadFile, File, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette import status
from tortoise.expressions import F
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
)
from apps.base.schemas import (
    SelectFileModel,
    InitChunkUploadModel,
    CompleteUploadModel,
    PresignUploadInitRequest,
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
from core.settings import settings
from core.storage import storages, FileStorageInterface, create_zip_stream
from core.utils import get_now, sanitize_filename, verify_select_token
from core.websocket import ws_manager
from core.tasks import CollectionEvents

logger = logging.getLogger(__name__)

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
    file: Optional[UploadFile] = File(None),
    files: List[UploadFile] = File(default=[]),
    ip: str = Depends(ip_limit["upload"]),
):
    if expire_style not in settings.expireStyle:
        raise HTTPException(status_code=400, detail="过期时间类型错误")

    # 校验文件备注长度（与文本分享 maxTextLength 一致）
    _validate_remark(remark)

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
        )
        await ip_limit["upload"].add_ip(ip)
        return APIResponse(detail={"code": code})

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
        file_code = await FileCodes.create(
            code=code,
            prefix=prefix,
            suffix=suffix,
            uuid_file_name=uuid_file_name,
            file_path=path,
            size=file_size,
            remark=remark or None,
            expired_at=expired_at,
            expired_count=expired_count,
            used_count=used_count,
            is_multi_file=False,
            expire_style=expire_style,
            expire_value=expire_value,
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
        return APIResponse(detail={"code": code, "name": f.filename})

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
    try:
        for f, file_size, filename, prefix, suffix, save_path, idx in validated_files:
            await file_storage.save_file(f, save_path)
            saved_paths.append(save_path)
            file_items_data.append({
                "file_name": f"{prefix}{suffix}",
                "file_size": file_size,
                "storage_path": path,
                "uuid_file_name": filename,
                "sort_order": idx,
            })
    except Exception as e:
        # 保存失败时回滚已保存的文件
        for sp in saved_paths:
            try:
                # 构造临时 FileCodes 对象用于删除
                temp_code = FileCodes(file_path=path, uuid_file_name=os.path.basename(sp))
                await file_storage.delete_file(temp_code)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail="文件保存失败，请重试")

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
    )

    for item_data in file_items_data:
        await FileItem.create(file_code=file_code, **item_data)

    await ip_limit["upload"].add_ip(ip)
    return APIResponse(detail={"code": code, "name": f"{len(upload_files)}个文件", "is_multi_file": True})


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
async def get_code_file(code: str, ip: str = Depends(ip_limit["error"])):
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    has, file_code = await get_code_file_by_code(code)
    if not has:
        await ip_limit["error"].add_ip(ip)
        return APIResponse(code=404, detail=file_code)

    assert isinstance(file_code, FileCodes)

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
async def get_file_info(code: str, ip: str = Depends(ip_limit["error"])):
    """只读查询文件分享详情（不消耗取件次数）：
    返回与 select_file 相同结构 + expired_count（实时剩余次数），供记录查看刷新；
    已过期返回 410，不存在返回 404。"""
    code = _validate_code(code)
    has, file_code = await get_code_file_by_code(code)
    if not has:
        await ip_limit["error"].add_ip(ip)
        return APIResponse(code=404, detail=file_code)

    assert isinstance(file_code, FileCodes)

    # 次数模式次数用尽 / 时间型已过期
    if file_code.expired_count == 0 or (
        file_code.expired_count < 0
        and file_code.expired_at is not None
        and file_code.expired_at < await get_now()
    ):
        return APIResponse(code=410, detail="文件已过期")

    if file_code.is_multi_file:
        items = await FileItem.filter(file_code=file_code).order_by("sort_order")
        return APIResponse(
            detail={
                "code": file_code.code,
                "name": f"{len(items)}个文件",
                "size": file_code.size,
                "text": "",
                "remark": file_code.remark or None,
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

    # 多文件模式：返回文件列表信息
    if file_code.is_multi_file:
        if not await update_file_usage(file_code):
            return APIResponse(code=410, detail="文件已过期")
        items = await FileItem.filter(file_code=file_code).order_by("sort_order")
        return APIResponse(
            detail={
                "code": file_code.code,
                "name": f"{len(items)}个文件",
                "size": file_code.size,
                "text": "",
                "remark": file_code.remark or None,
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

    if not await update_file_usage(file_code):
        return APIResponse(code=410, detail="文件已过期")
    # 文本分享（内容在 remark，无文件路径）→ text 返回 remark 内容；否则返回文件下载 URL
    text_content = file_code.remark if (not file_code.file_path and file_code.remark) else await file_storage.get_file_url(file_code)
    return APIResponse(
        detail={
            "code": file_code.code,
            "name": file_code.prefix + file_code.suffix,
            "size": file_code.size,
            "text": text_content,
            "remark": file_code.remark or None,
            "created_at": file_code.created_at.isoformat() if file_code.created_at else None,
            "expired_at": file_code.expired_at.isoformat() if file_code.expired_at else None,
            "expire_style": file_code.expire_style,
            "expire_value": file_code.expire_value,
            "expired_count": file_code.expired_count,
        }
    )


@share_api.get("/download")
async def download_file(key: str, code: str, ip: str = Depends(ip_limit["error"])):
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    if not await verify_select_token(key, code):
        await ip_limit["error"].add_ip(ip)
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
        return PlainTextResponse(file_code.remark, media_type="text/plain; charset=utf-8")
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
        # 合并文件并计算哈希
        _, file_hash = await storage.merge_chunks(upload_id, chunk_info, save_path)
        # 创建文件记录
        expired_at, expired_count, used_count, code = await get_expire_info(
            data.expire_value, data.expire_style
        )
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
            file_path=path,
            uuid_file_name=f"{prefix}{suffix}",
            prefix=prefix,
            suffix=suffix,
            expire_style=data.expire_style,
            expire_value=data.expire_value,
        )
        # 清理临时文件
        await storage.clean_chunks(upload_id, save_path)
        # 清理数据库中的分片记录
        await UploadChunk.filter(upload_id=upload_id).delete()
        await ip_limit["upload"].add_ip(ip)
        return APIResponse(detail={"code": code, "name": chunk_info.file_name})
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
    upload_id: str, file: UploadFile = File(...), ip: str = Depends(ip_limit["upload"])
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
    )

    await session.delete()
    await ip_limit["upload"].add_ip(ip)
    return APIResponse(detail={"code": code, "name": session.file_name})


@presign_api.post(
    "/upload/confirm/{upload_id}", dependencies=[Depends(share_required_login)]
)
async def presign_upload_confirm(upload_id: str, ip: str = Depends(ip_limit["upload"])):
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
async def download_single_file(item_id: int, code: str, ip: str = Depends(ip_limit["error"])):
    """下载多文件分享中的单个文件"""
    file_item = await FileItem.filter(id=item_id).first()
    if not file_item:
        raise HTTPException(404, "文件不存在")

    # 验证取件码权限
    file_code = await FileCodes.filter(id=file_item.file_code_id).first()
    if not file_code or file_code.code != code:
        await ip_limit["error"].add_ip(ip)
        raise HTTPException(403, "取件码不匹配")

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
async def download_zip(code: str, ip: str = Depends(ip_limit["error"])):
    """流式打包下载文件（单文件分享同样支持打包）"""
    has, file_code = await get_code_file_by_code(code)
    if not has:
        await ip_limit["error"].add_ip(ip)
        # 区分"不存在"和"已过期"
        if isinstance(file_code, str) and "过期" in file_code:
            raise HTTPException(410, file_code)
        raise HTTPException(404, "文件不存在")

    assert isinstance(file_code, FileCodes)

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
    # 连接数上限：超过则拒绝（与房间 max_members 模式一致，connect 后再校验以完成握手）
    if len(ws_manager.active_connections.get(room_code, [])) > COLLECTION_WS_MAX_CONNECTIONS:
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
                # 更新昵称
                for conn in ws_manager.active_connections.get(room_code, []):
                    if conn["ws"] is websocket:
                        conn["nickname"] = nickname
                        break
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
        left_nickname = ws_manager.disconnect(room_code, websocket)
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


# ============ 实时房间WebSocket端点（统一） ============
# /ws/room 与旧路径 /ws/chat、/ws/transfer、/ws/direct 等价（兼容已分享链接）
# 聊天消息不落库、无历史接口：文本仅在内存中实时转发（服务器不保存记录）


@ws_api.websocket("/ws/room/{room_code}")
@ws_api.websocket("/ws/direct/{room_code}")
@ws_api.websocket("/ws/chat/{room_code}")
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
                # 同 id 旧连接（断线残留/多标签页）→ 顶替并关闭
                for old in list(ws_manager.active_connections.get(room.room_code, [])):
                    if old is not ws_conn and old.get("client_id") == client_id:
                        try:
                            await old["ws"].close(code=1000, reason="replaced")
                        except Exception:
                            pass
                        ws_manager.active_connections[room.room_code].remove(old)
        except (json.JSONDecodeError, ValueError):
            pass

        # 房间人数上限校验：超过 max_members 拒绝加入
        if room.max_members > 0 and len(ws_manager.active_connections.get(room.room_code, [])) > room.max_members:
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
        await ws_manager.send_personal(room.room_code, websocket, {
            "type": "welcome",
            "client_id": client_id,
        })

        # 广播用户加入（含成员列表，供多人场景识别彼此）
        await ws_manager.broadcast(room.room_code, {
            "type": "user_joined",
            "nickname": nickname,
            "online_users": ws_manager.get_online_users(room.room_code),
            "members": ws_manager.get_members(room.room_code),
        })

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
                    for target_id in target_ids:
                        await ws_manager.send_bytes_to_client(room.room_code, target_id, data)
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
                # 聊天消息不落库：仅实时广播给所有连接（含发送者，回显按 client_id 匹配）
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
                await ws_manager.broadcast_exclude(room.room_code, websocket, {
                    "type": "file_offer",
                    "transfer_id": msg.get("transfer_id") or "",
                    "file_name": msg.get("file_name") or "",
                    "file_size": msg.get("file_size") or 0,
                    "sender": nickname or "匿名",
                    "from_id": client_id,
                })
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
    except WebSocketDisconnect:
        pass
    finally:
        left_nickname = ws_manager.disconnect(room.room_code, websocket)
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
        # 次数模式：房间从"有人"变为"无人"计一次会话；次数扣尽后房间过期
        if room.expire_style == "count" and room.room_code not in ws_manager.active_connections:
            await Room.filter(id=room.id, expired_count__gt=0).update(
                expired_count=F("expired_count") - 1
            )


# ============ 兼容旧链接的 REST 入口（内部统一到 Room 表） ============
compat_room_api = APIRouter(tags=["实时房间(兼容)"])


@compat_room_api.post("/chat/create/", dependencies=[Depends(share_required_login)])
async def compat_create_chat(data: CreateRoomRequest, ip: str = Depends(ip_limit["upload"])):
    """旧聊天房间入口：创建直连快传房间（统一模式，兼容旧链接）"""
    room = await _create_room(data, ip)
    return APIResponse(detail=await _build_room_response(room))


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


@compat_room_api.get("/chat/info/{room_code}")
async def compat_chat_info(room_code: str):
    room = await _get_room(room_code)
    return APIResponse(detail=await _build_room_response(room))


@compat_room_api.get("/transfer/info/{room_code}")
async def compat_transfer_info(room_code: str):
    room = await _get_room(room_code)
    return APIResponse(detail=await _build_room_response(room))


@compat_room_api.get("/direct/info/{room_code}")
async def compat_direct_info(room_code: str):
    room = await _get_room(room_code)
    return APIResponse(detail=await _build_room_response(room))
