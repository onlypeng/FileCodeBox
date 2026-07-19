import datetime
import os
import re
import secrets
import string
import uuid
from typing import Optional, Tuple, Union, List
from urllib.parse import quote, unquote

from fastapi import APIRouter, Form, UploadFile, File, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette import status

from apps.admin.dependencies import share_required_login
from apps.base.models import FileCodes, UploadChunk, PresignUploadSession, FileItem, CollectionBox, CollectionFile
from apps.base.schemas import SelectFileModel
from apps.base.services.expire import validate_expire_config, expire_to_seconds, calculate_expired_at
from apps.base.utils import get_expire_info, get_file_path_name, ip_limit
from core.response import APIResponse
from core.settings import settings
from core.storage import storages, FileStorageInterface, create_zip_stream
from core.utils import get_select_token, get_now, sanitize_filename, verify_select_token, hash_password, escape_html_text

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
        # 流式上传未提供 Content-Length，使用分块读取累加，限制最大读取量
        max_read = max_size + 1024 * 1024  # 允许超过限制 1MB 用于报错
        size = 0
        chunk_size = 64 * 1024  # 64KB 分块
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            if size > max_read:
                break
        await file.seek(0)
    if size > max_size:
        max_size_mb = max_size / (1024 * 1024)
        raise HTTPException(
            status_code=403, detail=f"大小超过限制,最大为{max_size_mb:.2f} MB"
        )
    return size


async def create_file_code(code, **kwargs):
    return await FileCodes.create(code=code, **kwargs)


async def get_code_file_by_code(
    code: str, check: bool = True
) -> Tuple[bool, Union[FileCodes, str]]:
    file_code = await FileCodes.filter(code=code).first()
    if not file_code:
        return False, "文件不存在"
    if await file_code.is_expired() and check:
        return False, "文件已过期"
    return True, file_code


async def update_file_usage(file_code: FileCodes) -> None:
    """原子更新文件使用次数"""
    file_code.used_count += 1
    if file_code.expired_count > 0:
        # 使用原子操作扣减过期次数，防止并发竞态
        result = await FileCodes.filter(id=file_code.id, expired_count__gt=0).update(
            expired_count=file_code.expired_count - 1,
            used_count=file_code.used_count,
        )
        if result == 0:
            # 并发导致次数已为0，重新获取最新状态
            await file_code.refresh_from_db()
            return
        file_code.expired_count -= 1
    else:
        await file_code.save()


# ============ 共享工具函数 ============

_CODE_PATTERN = re.compile(r"^[A-Z0-9]{5,15}$")


def _validate_code(code: str) -> str:
    """验证码格式：大写字母+数字，5-15位，自动转大写"""
    if not code:
        raise HTTPException(status_code=400, detail="码不能为空")
    code = code.strip().upper()
    if not _CODE_PATTERN.match(code):
        raise HTTPException(status_code=400, detail="码格式错误，必须为大写字母和数字")
    return code


def _generate_code(length: int = None) -> str:
    """生成密码学安全的随机码，位数由后台配置决定"""
    if length is None:
        length = getattr(settings, 'codeDigitCount', 8)
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


async def _get_unique_code(model_class, field_name: str, length: int = None) -> str:
    """确保码唯一，最多重试 20 次"""
    if length is None:
        length = getattr(settings, 'codeDigitCount', 8)
    for _ in range(20):
        code = _generate_code(length)
        if not await model_class.filter(**{field_name: code}).exists():
            return code
    raise HTTPException(500, "生成唯一码失败，请重试")


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


async def _build_collection_response(box: CollectionBox, include_files: bool = True) -> dict:
    """统一的收件箱响应构建（所有端点复用）"""
    file_list = await _build_file_list(box) if include_files else []
    total_size = sum(f["file_size"] for f in file_list) if include_files else 0

    return {
        "collection_code": box.collection_code,
        "delivery_code": box.delivery_code,
        "retrieve_code": box.retrieve_code,
        "title": box.title,
        "description": box.description,
        "max_files": box.max_files,
        "created_at": box.created_at.isoformat() if box.created_at else None,
        # 收件箱过期配置
        "expire_style": box.expire_style,
        "expire_value": box.expire_value,
        "expired_at": box.expired_at.isoformat() if box.expired_at else None,
        # 投件码过期配置
        "delivery_expire_style": box.delivery_expire_style,
        "delivery_expire_value": box.delivery_expire_value,
        "delivery_expired_at": box.delivery_expired_at.isoformat() if box.delivery_expired_at else None,
        # 取件码过期配置
        "retrieve_expire_style": box.retrieve_expire_style,
        "retrieve_expire_value": box.retrieve_expire_value,
        "retrieve_expired_at": box.retrieve_expired_at.isoformat() if box.retrieve_expired_at else None,
        # 文件信息
        "file_count": len(file_list),
        "total_size": total_size,
        "files": file_list,
    }


async def _build_retrieve_response(box: CollectionBox, include_files: bool = True) -> dict:
    """取件码访问的响应构建（不返回管理码和投件码）"""
    file_list = await _build_file_list(box) if include_files else []
    total_size = sum(f["file_size"] for f in file_list) if include_files else 0

    return {
        "retrieve_code": box.retrieve_code,
        "title": box.title,
        "description": box.description,
        "max_files": box.max_files,
        "created_at": box.created_at.isoformat() if box.created_at else None,
        # 收件箱过期配置
        "expire_style": box.expire_style,
        "expire_value": box.expire_value,
        "expired_at": box.expired_at.isoformat() if box.expired_at else None,
        # 取件码过期配置
        "retrieve_expire_style": box.retrieve_expire_style,
        "retrieve_expire_value": box.retrieve_expire_value,
        "retrieve_expired_at": box.retrieve_expired_at.isoformat() if box.retrieve_expired_at else None,
        # 文件信息
        "file_count": len(file_list),
        "total_size": total_size,
        "files": file_list,
    }


async def _build_delivery_response(box: CollectionBox) -> dict:
    """投件码访问的响应构建（不返回管理码、取件码及取件码过期配置）"""
    file_count = await CollectionFile.filter(collection=box).count()
    return {
        "delivery_code": box.delivery_code,
        "title": box.title,
        "description": box.description,
        "max_files": box.max_files,
        "file_count": file_count,
        "delivery_count": box.delivery_count,
        "delivery_used_count": box.delivery_used_count,
        "created_at": box.created_at.isoformat() if box.created_at else None,
        # 收件箱过期配置（投件人需要知道收件箱是否过期）
        "expire_style": box.expire_style,
        "expire_value": box.expire_value,
        "expired_at": box.expired_at.isoformat() if box.expired_at else None,
        # 投件码过期配置
        "delivery_expire_style": box.delivery_expire_style,
        "delivery_expire_value": box.delivery_expire_value,
        "delivery_expired_at": box.delivery_expired_at.isoformat() if box.delivery_expired_at else None,
        # 不返回取件码过期配置和取件码
    }


# ============ 分享API ============


@share_api.post("/text/", dependencies=[Depends(share_required_login)])
async def share_text(
    text: str = Form(...),
    expire_value: int = Form(default=1, gt=0),
    expire_style: str = Form(default="day"),
    ip: str = Depends(ip_limit["upload"]),
):
    validate_expire_config(expire_style, expire_value)

    text_size = len(text.encode("utf-8"))
    max_txt_size = 222 * 1024
    if text_size > max_txt_size:
        raise HTTPException(status_code=403, detail="内容过多,建议采用文件形式")

    expired_at, expired_count, used_count, code = await get_expire_info(
        expire_value, expire_style
    )
    # 对文本内容进行 HTML 转义，防止 XSS 攻击
    safe_text = escape_html_text(text)
    await create_file_code(
        code=code,
        text=safe_text,
        expired_at=expired_at,
        expired_count=expired_count,
        used_count=used_count,
        size=len(text),
        prefix="Text",
        expire_style=expire_style,
        expire_value=expire_value,
    )
    ip_limit["upload"].add_ip(ip)
    return APIResponse(detail={"code": code})


@share_api.post("/file/", dependencies=[Depends(share_required_login)])
async def share_file(
    expire_value: int = Form(default=1, gt=0),
    expire_style: str = Form(default="day"),
    file: Optional[UploadFile] = File(None),
    files: List[UploadFile] = File(default=[]),
    ip: str = Depends(ip_limit["upload"]),
):
    validate_expire_config(expire_style, expire_value)

    file_storage: FileStorageInterface = storages[settings.file_storage]()

    # 兼容单文件模式
    upload_files = list(files) if files else []
    if file and not upload_files:
        upload_files = [file]

    if not upload_files:
        raise HTTPException(status_code=400, detail="未选择文件")

    # 多文件数量限制
    max_count = getattr(settings, "maxSendFiles", 20)
    if len(upload_files) > max_count:
        raise HTTPException(status_code=400, detail=f"文件数量超过限制，最多{max_count}个")

    # 限流窗口内文件数量限制
    upload_count = getattr(settings, "uploadCount", 0)
    if upload_count > 0 and len(upload_files) > upload_count:
        raise HTTPException(status_code=400, detail=f"文件数量超过限流限制，最多{upload_count}个")

    expired_at, expired_count, used_count, code = await get_expire_info(
        expire_value, expire_style
    )

    # 单文件模式（向后兼容）
    if len(upload_files) == 1:
        f = upload_files[0]
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
        ip_limit["upload"].add_ip(ip)
        return APIResponse(detail={"code": code, "name": f.filename})

    # 多文件模式
    total_size = 0
    file_items_data = []
    today = datetime.datetime.now()
    storage_path_prefix = settings.storage_path.strip("/")
    file_uuid = uuid.uuid4().hex
    base_path = f"share/data/{today.strftime('%Y/%m/%d')}/{file_uuid}"
    path = f"{storage_path_prefix}/{base_path}" if storage_path_prefix else base_path

    # 先验证所有文件大小
    validated_files = []
    for idx, f in enumerate(upload_files):
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
        expired_at=expired_at,
        expired_count=expired_count,
        used_count=used_count,
        is_multi_file=True,
        expire_style=expire_style,
        expire_value=expire_value,
    )

    for item_data in file_items_data:
        await FileItem.create(file_code=file_code, **item_data)

    ip_limit["upload"].add_ip(ip)
    return APIResponse(detail={"code": code, "name": f"{len(upload_files)}个文件", "is_multi_file": True})


@share_api.post("/", dependencies=[Depends(share_required_login)])
async def share_unified(
    expire_value: int = Form(default=1, gt=0),
    expire_style: str = Form(default="day"),
    text: Optional[str] = Form(default=None),
    file: Optional[UploadFile] = File(None),
    files: List[UploadFile] = File(default=[]),
    ip: str = Depends(ip_limit["upload"]),
):
    """统一分享接口：支持纯文本、纯文件、文件+文本备注

    - 无文件时：纯文本分享（等同 /share/text/）
    - 有文件时：文件分享，text 作为可选备注
    """
    validate_expire_config(expire_style, expire_value)

    # 收集上传文件
    upload_files = list(files) if files else []
    if file and not upload_files:
        upload_files = [file]
    has_files = len(upload_files) > 0
    has_text = bool(text and text.strip())

    if not has_files and not has_text:
        raise HTTPException(status_code=400, detail="请选择文件或输入文本")

    # 无文件 → 纯文本分享
    if not has_files:
        text_size = len(text.encode("utf-8"))
        max_txt_size = 222 * 1024
        if text_size > max_txt_size:
            raise HTTPException(status_code=403, detail="内容过多,建议采用文件形式")

        expired_at, expired_count, used_count, code = await get_expire_info(
            expire_value, expire_style
        )
        safe_text = escape_html_text(text)
        await create_file_code(
            code=code,
            text=safe_text,
            expired_at=expired_at,
            expired_count=expired_count,
            used_count=used_count,
            size=len(text),
            prefix="Text",
            expire_style=expire_style,
            expire_value=expire_value,
        )
        ip_limit["upload"].add_ip(ip)
        return APIResponse(detail={"code": code})

    # 有文件 → 文件分享（含可选文本备注）
    file_storage: FileStorageInterface = storages[settings.file_storage]()

    # 多文件数量限制
    max_count = getattr(settings, "maxSendFiles", 20)
    if len(upload_files) > max_count:
        raise HTTPException(status_code=400, detail=f"文件数量超过限制，最多{max_count}个")

    upload_count = getattr(settings, "uploadCount", 0)
    if upload_count > 0 and len(upload_files) > upload_count:
        raise HTTPException(status_code=400, detail=f"文件数量超过限流限制，最多{upload_count}个")

    # 处理文本备注
    safe_text = None
    if has_text:
        text_size = len(text.encode("utf-8"))
        max_txt_size = 222 * 1024
        if text_size > max_txt_size:
            raise HTTPException(status_code=403, detail="备注内容过多")
        safe_text = escape_html_text(text)

    expired_at, expired_count, used_count, code = await get_expire_info(
        expire_value, expire_style
    )

    # 单文件模式
    if len(upload_files) == 1:
        f = upload_files[0]
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
            text=safe_text,
            expired_at=expired_at,
            expired_count=expired_count,
            used_count=used_count,
            is_multi_file=False,
            expire_style=expire_style,
            expire_value=expire_value,
        )
        await FileItem.create(
            file_code=file_code,
            file_name=f"{prefix}{suffix}",
            file_size=file_size,
            storage_path=path,
            uuid_file_name=uuid_file_name,
            sort_order=0,
        )
        ip_limit["upload"].add_ip(ip)
        return APIResponse(detail={"code": code, "name": f.filename})

    # 多文件模式
    total_size = 0
    file_items_data = []
    today = datetime.datetime.now()
    storage_path_prefix = settings.storage_path.strip("/")
    file_uuid = uuid.uuid4().hex
    base_path = f"share/data/{today.strftime('%Y/%m/%d')}/{file_uuid}"
    path = f"{storage_path_prefix}/{base_path}" if storage_path_prefix else base_path

    validated_files = []
    for idx, f in enumerate(upload_files):
        file_size = await validate_file_size(f, settings.uploadSize)
        total_size += file_size
        if total_size > settings.uploadSize:
            raise HTTPException(status_code=403, detail="文件总大小超过限制")
        filename = await sanitize_filename(unquote(f.filename or ""))
        prefix, suffix = os.path.splitext(filename)
        save_path = f"{path}/{filename}"
        validated_files.append((f, file_size, filename, prefix, suffix, save_path, idx))

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
    except Exception:
        for sp in saved_paths:
            try:
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
        text=safe_text,
        expired_at=expired_at,
        expired_count=expired_count,
        used_count=used_count,
        is_multi_file=True,
        expire_style=expire_style,
        expire_value=expire_value,
    )

    for item_data in file_items_data:
        await FileItem.create(file_code=file_code, **item_data)

    ip_limit["upload"].add_ip(ip)
    return APIResponse(detail={"code": code, "name": f"{len(upload_files)}个文件", "is_multi_file": True})


@share_api.get("/check_code/")
async def check_code_type(code: str):
    """检查码类型：file / collection / delivery / retrieve / unknown
    返回完整的过期信息以便客户端记录实时刷新
    """
    code = _validate_code(code)
    # 先检查是否为普通文件码
    has, file_code = await get_code_file_by_code(code)
    if has:
        assert isinstance(file_code, FileCodes)
        is_expired = await file_code.is_expired()
        is_multi_file = bool(file_code.is_multi_file)
        file_count = 1
        if is_multi_file:
            file_count = await FileItem.filter(file_code=file_code).count()
        return APIResponse(detail={
            "type": "file",
            "code": code,
            "expired": is_expired,
            "name": f"{file_code.prefix}{file_code.suffix}" if file_code.prefix or file_code.suffix else None,
            "size": file_code.size,
            "is_multi_file": is_multi_file,
            "file_count": file_count,
            "expired_at": file_code.expired_at.isoformat() if file_code.expired_at else None,
            "expire_style": file_code.expire_style,
            "expire_value": file_code.expire_value,
            "used_count": file_code.used_count,
            "expired_count": file_code.expired_count,
            "is_permanent": file_code.expired_at is None and (file_code.expired_count is None or file_code.expired_count < 0),
        })

    # 检查是否为收件箱管理码
    box = await CollectionBox.filter(collection_code=code).first()
    if box:
        is_expired = await box.is_expired()
        file_count = await CollectionFile.filter(collection=box).count()
        return APIResponse(detail={
            "type": "collection",
            "code": code,
            "expired": is_expired,
            "title": box.title,
            "max_files": box.max_files,
            "file_count": file_count,
            "expired_at": box.expired_at.isoformat() if box.expired_at else None,
            "expire_style": box.expire_style,
            "expire_value": box.expire_value,
            "is_permanent": box.expire_style == "forever" or box.expired_at is None,
        })

    # 检查是否为取件码
    box = await CollectionBox.filter(retrieve_code=code).first()
    if box:
        is_expired = await box.is_expired() or await box.is_retrieve_expired()
        file_count = await CollectionFile.filter(collection=box).count()
        return APIResponse(detail={
            "type": "retrieve",
            "code": code,
            "expired": is_expired,
            "title": box.title,
            "max_files": box.max_files,
            "file_count": file_count,
            "expired_at": box.retrieve_expired_at.isoformat() if box.retrieve_expired_at else None,
            "expire_style": box.retrieve_expire_style,
            "expire_value": box.retrieve_expire_value,
            "is_permanent": box.retrieve_expire_style == "forever" or box.retrieve_expired_at is None,
        })

    # 检查是否为投递码
    box = await CollectionBox.filter(delivery_code=code).first()
    if box:
        is_expired = await box.is_expired() or await box.is_delivery_expired()
        file_count = await CollectionFile.filter(collection=box).count()
        return APIResponse(detail={
            "type": "delivery",
            "code": code,
            "expired": is_expired,
            "title": box.title,
            "max_files": box.max_files,
            "file_count": file_count,
            "expired_at": box.delivery_expired_at.isoformat() if box.delivery_expired_at else None,
            "expire_style": box.delivery_expire_style,
            "expire_value": box.delivery_expire_value,
            "is_permanent": box.delivery_expire_style == "forever" or box.delivery_expired_at is None,
        })

    return APIResponse(code=404, detail={"type": "unknown", "code": code})


@share_api.get("/select/")
async def get_code_file(code: str, ip: str = Depends(ip_limit["error"])):
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    has, file_code = await get_code_file_by_code(code)
    if not has:
        ip_limit["error"].add_ip(ip)
        return APIResponse(code=404, detail=file_code)

    assert isinstance(file_code, FileCodes)

    # 多文件模式：返回文件列表
    if file_code.is_multi_file:
        await update_file_usage(file_code)
        items = await FileItem.filter(file_code=file_code).order_by("sort_order")
        return APIResponse(detail={
            "code": file_code.code,
            "name": f"{len(items)}个文件",
            "size": file_code.size,
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

    await update_file_usage(file_code)
    return await file_storage.get_file_response(file_code)


@share_api.post("/select/")
async def select_file(data: SelectFileModel, ip: str = Depends(ip_limit["error"])):
    data.code = _validate_code(data.code)
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    has, file_code = await get_code_file_by_code(data.code)
    if not has:
        ip_limit["error"].add_ip(ip)
        return APIResponse(code=404, detail=file_code)

    assert isinstance(file_code, FileCodes)

    # 多文件模式：返回文件列表信息
    if file_code.is_multi_file:
        await update_file_usage(file_code)
        items = await FileItem.filter(file_code=file_code).order_by("sort_order")
        return APIResponse(
            detail={
                "code": file_code.code,
                "name": f"{len(items)}个文件",
                "size": file_code.size,
                "text": "",
                "text_note": file_code.text or "",
                "is_multi_file": True,
                "created_at": file_code.created_at.isoformat() if file_code.created_at else None,
                "expired_at": file_code.expired_at.isoformat() if file_code.expired_at else None,
                "expire_style": file_code.expire_style,
                "expire_value": file_code.expire_value,
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

    await update_file_usage(file_code)

    # 纯文本分享（prefix == "Text"）
    is_text_only = file_code.prefix == "Text"
    if is_text_only:
        return APIResponse(
            detail={
                "code": file_code.code,
                "name": file_code.prefix + file_code.suffix,
                "size": file_code.size,
                "text": file_code.text or "",
                "text_note": "",
                "created_at": file_code.created_at.isoformat() if file_code.created_at else None,
                "expired_at": file_code.expired_at.isoformat() if file_code.expired_at else None,
                "expire_style": file_code.expire_style,
                "expire_value": file_code.expire_value,
            }
        )

    # 文件分享（可能附带文本备注）
    return APIResponse(
        detail={
            "code": file_code.code,
            "name": file_code.prefix + file_code.suffix,
            "size": file_code.size,
            "text": await file_storage.get_file_url(file_code),
            "text_note": file_code.text or "",
            "created_at": file_code.created_at.isoformat() if file_code.created_at else None,
            "expired_at": file_code.expired_at.isoformat() if file_code.expired_at else None,
            "expire_style": file_code.expire_style,
            "expire_value": file_code.expire_value,
        }
    )


@share_api.get("/download")
async def download_file(key: str, code: str, ip: str = Depends(ip_limit["error"])):
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    if not await verify_select_token(key, code):
        ip_limit["error"].add_ip(ip)
        raise HTTPException(status_code=403, detail="下载鉴权失败")
    has, file_code = await get_code_file_by_code(code, True)
    if not has:
        # 区分"不存在"和"已过期"
        if isinstance(file_code, str) and "过期" in file_code:
            raise HTTPException(status_code=410, detail=file_code)
        return APIResponse(code=404, detail="文件不存在")
    assert isinstance(file_code, FileCodes)
    return (
        APIResponse(detail=file_code.text)
        if file_code.text
        else await file_storage.get_file_response(file_code)
    )


@share_api.get("/download/item/{item_id}")
async def download_single_file(item_id: int, code: str, ip: str = Depends(ip_limit["error"])):
    """下载多文件分享中的单个文件"""
    file_item = await FileItem.filter(id=item_id).first()
    if not file_item:
        raise HTTPException(404, "文件不存在")

    # 验证取件码权限
    file_code = await FileCodes.filter(id=file_item.file_code_id).first()
    if not file_code or file_code.code != code:
        ip_limit["error"].add_ip(ip)
        raise HTTPException(403, "取件码不匹配")

    if await file_code.is_expired():
        raise HTTPException(404, "文件已过期")

    # 下载时扣除次数
    await update_file_usage(file_code)

    file_storage: FileStorageInterface = storages[settings.file_storage]()
    full_path = f"{file_item.storage_path}/{file_item.uuid_file_name}"

    # 本地存储直接返回文件
    if settings.file_storage == "local":
        from fastapi.responses import FileResponse

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

    filename = file_item.file_name
    encoded_filename = quote(filename, safe='')
    return StreamingResponse(
        stream_generator(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@share_api.get("/zip/{code}")
async def download_zip(code: str, ip: str = Depends(ip_limit["error"])):
    """流式打包下载多文件"""
    has, file_code = await get_code_file_by_code(code)
    if not has:
        ip_limit["error"].add_ip(ip)
        # 区分"不存在"和"已过期"
        if isinstance(file_code, str) and "过期" in file_code:
            raise HTTPException(410, file_code)
        raise HTTPException(404, "文件不存在")

    assert isinstance(file_code, FileCodes)

    if not file_code.is_multi_file:
        raise HTTPException(400, "该分享不是多文件分享")

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