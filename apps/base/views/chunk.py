import hashlib
import os
import uuid

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from starlette import status

from apps.admin.dependencies import share_required_login
from apps.base.models import FileCodes, UploadChunk
from apps.base.schemas import InitChunkUploadModel, CompleteUploadModel
from apps.base.services.expire import validate_expire_config
from apps.base.utils import get_expire_info, get_chunk_file_path_name, ip_limit
from core.response import APIResponse
from core.settings import settings
from core.storage import storages, FileStorageInterface

chunk_api = APIRouter(prefix="/chunk", tags=["切片"])


@chunk_api.post("/upload/init/", dependencies=[Depends(share_required_login)])
async def init_chunk_upload(data: InitChunkUploadModel):
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

    # 读取分片数据并计算哈希
    chunk_data = await chunk.read()
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
            "progress": len(uploaded_chunks) / chunk_info.total_chunks * 100,
        }
    )


@chunk_api.post(
    "/upload/complete/{upload_id}", dependencies=[Depends(share_required_login)]
)
async def complete_upload(
    upload_id: str, data: CompleteUploadModel, ip: str = Depends(ip_limit["upload"])
):
    validate_expire_config(data.expire_style, data.expire_value)

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
        ip_limit["upload"].add_ip(ip)
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