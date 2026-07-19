import os
import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException

from apps.admin.dependencies import share_required_login
from apps.base.models import FileCodes, PresignUploadSession
from apps.base.schemas import PresignUploadInitRequest
from apps.base.services.expire import validate_expire_config
from apps.base.utils import ip_limit
from core.response import APIResponse
from core.settings import settings
from core.storage import storages, FileStorageInterface
from core.utils import get_now

from .share import FileUploadService, validate_file_size

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
    if data.file_size > settings.uploadSize:
        raise HTTPException(
            403,
            f"文件大小超过限制，最大为 {settings.uploadSize / (1024 * 1024):.2f} MB",
        )
    validate_expire_config(data.expire_style, data.expire_value)

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
    upload_url = presigned_url or f"/api/presign/upload/proxy/{upload_id}"

    await PresignUploadSession.create(
        upload_id=upload_id,
        file_name=filename,
        file_size=data.file_size,
        save_path=save_path,
        mode=mode,
        expire_value=data.expire_value,
        expire_style=data.expire_style,
        expires_at=await get_now() + timedelta(seconds=PRESIGN_SESSION_EXPIRES),
    )

    ip_limit["upload"].add_ip(ip)
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
    )

    await session.delete()
    ip_limit["upload"].add_ip(ip)
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
    )

    await session.delete()
    ip_limit["upload"].add_ip(ip)
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