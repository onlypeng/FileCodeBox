"""房间文件服务器缓存：本地磁盘落盘、读取与删除。

与 core/storage.py 的分享文件存储相互独立：
- 分享文件走 FileCodes 表 + 可插拔后端（本地/S3/OneDrive/WebDAV）；
- 房间缓存文件生命周期短、随房间回收，直接落在 data/room_cache 下，
  不占用 FileCodes，也不要求管理员配置任何对象存储。
"""
import hashlib
import hmac
import os
import re
import time
from pathlib import Path

import aiofiles
from fastapi import HTTPException, UploadFile

from core.logger import logger
from core.settings import data_root

CACHE_ROOT = data_root / "room_cache"

# 文件名中的非法字符（含路径分隔符）统一替换为下划线
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_file_name(file_name: str) -> str:
    """清洗文件名：去掉路径分隔符与控制字符，防止目录穿越与非法文件名。"""
    name = _UNSAFE_CHARS.sub("_", (file_name or "").strip())
    # 去掉前导点，避免写成隐藏文件或纯 "." / ".."
    name = name.lstrip(".") or "file"
    return name[:120]


def build_storage_key(room_code: str, transfer_id: str, file_name: str) -> str:
    """构造落盘相对路径（相对 CACHE_ROOT）。"""
    safe_room = _SAFE_TOKEN.sub("_", room_code or "")[:16] or "room"
    safe_tid = _SAFE_TOKEN.sub("_", transfer_id or "")[:64] or "file"
    return f"{safe_room}/{safe_tid}_{sanitize_file_name(file_name)}"


def resolve_path(storage_key: str) -> Path | None:
    """把 storage_key 解析为绝对路径；越界（目录穿越）返回 None。"""
    if not storage_key:
        return None
    root = CACHE_ROOT.resolve()
    target = (root / storage_key).resolve()
    if target != root and root not in target.parents:
        logger.warning(f"房间缓存文件路径越界，已拒绝访问: {storage_key}")
        return None
    return target


async def save_room_file(storage_key: str, upload: UploadFile, max_bytes: int = 0) -> tuple[int, str]:
    """把上传文件写入缓存目录，返回 (实际写入字节数, 内容 SHA-256 十六进制)。

    哈希在写入过程中用 hashlib 流式计算，是服务端权威值。
    不再采信客户端声明的 file_hash：该值可被伪造，会让"内容不同的文件被判定为
    重复"，进而把 A 的内容当成 B 的缓存返回给其他人（缓存投毒）。

    max_bytes > 0 时做大小上限校验，超出立即中断并删除半截文件。
    """
    target = resolve_path(storage_key)
    if target is None:
        raise HTTPException(status_code=400, detail="非法的缓存路径")

    target.parent.mkdir(parents=True, exist_ok=True)
    # 先写 .part 临时文件再原子改名：并发或中断都不会在最终路径留下半截文件
    tmp_path = target.with_name(target.name + ".part")

    hasher = hashlib.sha256()
    written = 0
    try:
        async with aiofiles.open(tmp_path, "wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if max_bytes > 0 and written > max_bytes:
                    raise HTTPException(status_code=413, detail="文件超过房间缓存大小上限")
                hasher.update(chunk)
                await out.write(chunk)
        os.replace(tmp_path, target)
    except HTTPException:
        await delete_room_file(storage_key)
        _unlink_quiet(tmp_path)
        raise
    except Exception as e:
        await delete_room_file(storage_key)
        _unlink_quiet(tmp_path)
        logger.error(f"房间缓存文件写入失败: {e}")
        raise HTTPException(status_code=500, detail="文件写入失败")

    return written, hasher.hexdigest()


async def move_room_file(src_key: str, dst_key: str) -> bool:
    """把已落盘文件从 src_key 改名为 dst_key（同分区原子操作）。"""
    src = resolve_path(src_key)
    dst = resolve_path(dst_key)
    if src is None or dst is None or not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    return True


def sweep_incoming(max_age_seconds: int = 3600) -> int:
    """清理 CACHE_ROOT/_incoming 下的陈旧临时文件。

    上传采用"先写临时文件、算完哈希再决定去留"的流程，若进程在两步之间崩溃，
    临时文件将无人回收。按修改时间兜底清理，避免磁盘被静默占满。
    """
    import time as _time

    incoming = resolve_path("_incoming")
    if incoming is None or not incoming.is_dir():
        return 0
    deadline = _time.time() - max(60, int(max_age_seconds))
    removed = 0
    for file_path in incoming.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            if file_path.stat().st_mtime < deadline:
                file_path.unlink()
                removed += 1
        except Exception as e:
            logger.warning(f"房间缓存临时文件清理失败: {file_path} {e}")
    return removed


def _unlink_quiet(path: Path) -> None:
    """尽力删除临时文件，失败不影响主流程。"""
    try:
        path.unlink()
    except Exception:
        pass


async def delete_room_file(storage_key: str) -> bool:
    """删除缓存文件，并尝试清理空的房间目录。"""
    target = resolve_path(storage_key)
    if target is None:
        return False
    try:
        if target.is_file():
            target.unlink()
    except Exception as e:
        logger.warning(f"房间缓存文件删除失败: {storage_key} {e}")
        return False

    try:
        parent = target.parent
        if parent != CACHE_ROOT.resolve() and not any(parent.iterdir()):
            parent.rmdir()
    except Exception:
        pass
    return True


def iter_room_dir(room_code: str) -> Path | None:
    """取房间缓存目录（用于房间整体清理）。"""
    safe_room = _SAFE_TOKEN.sub("_", room_code or "")[:16] or "room"
    target = (CACHE_ROOT / safe_room).resolve()
    root = CACHE_ROOT.resolve()
    if target != root and root not in target.parents:
        return None
    return target


def delete_room_dir(room_code: str) -> int:
    """删除某房间的全部缓存文件，返回删除文件数。"""
    room_dir = iter_room_dir(room_code)
    if room_dir is None or not room_dir.is_dir():
        return 0

    removed = 0
    for file_path in room_dir.rglob("*"):
        if file_path.is_file():
            try:
                file_path.unlink()
                removed += 1
            except Exception as e:
                logger.warning(f"房间缓存清理失败: {file_path} {e}")
    try:
        room_dir.rmdir()
    except Exception:
        pass
    return removed


async def delete_room_file_if_unreferenced(
    room_code: str,
    storage_key: str,
    *,
    exclude_cached_id: int | None = None,
    exclude_transfer_id: str = "",
) -> bool:
    """仅当没有其它缓存条目/历史消息引用这份落盘文件时才删除。

    房间缓存去重会让多条 RoomCachedFile（不同 transfer_id、相同内容）共用同一个
    storage_key。删除单条时若直接 unlink，其它条目的下载会集体 404。
    上传侧覆盖旧文件、成员删除、后台删除、过期清理四处都必须走这里收口。

    排除项语义：被删条目自身（exclude_cached_id）以及随本次操作一起清空
    storage_key 标记的同名消息（exclude_transfer_id）不算"仍被引用"。
    """
    # 延迟导入：core 层不直接依赖 apps.base.models，避免模块级循环
    from apps.base.models import RoomCachedFile, RoomMessage

    key = (storage_key or "").strip()
    if not key:
        return False

    cached_qs = RoomCachedFile.filter(room_code=room_code, storage_key=key)
    if exclude_cached_id is not None:
        cached_qs = cached_qs.exclude(id=exclude_cached_id)
    if await cached_qs.exists():
        logger.info(f"房间缓存落盘仍被其它条目引用，保留: room={room_code} key={key}")
        return False

    message_qs = RoomMessage.filter(room_code=room_code, storage_key=key)
    if exclude_transfer_id:
        message_qs = message_qs.exclude(content=exclude_transfer_id)
    if await message_qs.exists():
        logger.info(f"房间缓存落盘仍被其它历史消息引用，保留: room={room_code} key={key}")
        return False

    return await delete_room_file(key)


# ============ 下载签名 ============
# 缓存文件下载此前仅凭"房间码 + transfer_id"即可永久访问。加短期签名后，
# 链接默认 30 分钟有效，即便被转发/泄露也会很快失效。
DOWNLOAD_TTL = 30 * 60


def app_secret() -> str:
    """应用级 HMAC 密钥（下载签名与房间成员令牌共用）。

    单独暴露是为了让 core/room_token.py 复用同一份密钥策略，
    避免两处各自实现"环境变量 / 持久化随机密钥"的回退逻辑。
    """
    return _sign_secret()


_SECRET_CACHE = ""


def _sign_secret() -> str:
    """下载签名密钥。

    优先级：环境变量 JWT_SECRET > data 目录下持久化的随机密钥（首次调用自动生成）。

    不再回退到 settings.admin_token：其默认值 "FileCodeBox2023" 是公开已知常量，
    部署一旦未配 JWT_SECRET，攻击者就能自行算出合法签名、下载任意房间缓存文件
    （签名保护形同虚设）。改口令还会让所有已下发的链接立即失效，同样不可接受。
    """
    env_secret = os.getenv("JWT_SECRET")
    if env_secret:
        return env_secret

    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE

    secret_file = data_root / ".room_cache_download_secret"
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        if secret_file.is_file():
            cached = secret_file.read_text(encoding="utf-8").strip()
        else:
            cached = os.urandom(32).hex()
            secret_file.write_text(cached, encoding="utf-8")
            try:
                os.chmod(secret_file, 0o600)
            except Exception:
                pass
    except Exception as e:
        # 密钥不可得时拒绝签发/校验，绝不降级为公开默认值
        logger.error(f"房间缓存下载签名密钥不可用: {e}")
        raise RuntimeError("下载签名密钥不可用")

    if not cached:
        raise RuntimeError("下载签名密钥为空")
    _SECRET_CACHE = cached
    return _SECRET_CACHE


def sign_download(room_code: str, transfer_id: str, expires_ts: int) -> str:
    """签名串绑定房间码：避免同一 transfer_id 的签名跨房间复用。"""
    raw = f"{room_code}:{transfer_id}:{expires_ts}".encode()
    return hmac.new(_sign_secret().encode(), raw, hashlib.sha256).hexdigest()[:32]


def build_download_query(room_code: str, transfer_id: str, ttl: int = DOWNLOAD_TTL) -> str:
    """生成带过期时间的下载查询串：exp=<unix 秒>&sig=<hmac>"""
    exp = int(time.time()) + max(60, int(ttl))
    return f"exp={exp}&sig={sign_download(room_code, transfer_id, exp)}"


def verify_download(room_code: str, transfer_id: str, exp: str | None, sig: str | None) -> bool:
    """校验下载签名：房间码一致、签名一致且未过期。"""
    if not exp or not sig:
        return False
    try:
        exp_ts = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_ts < int(time.time()):
        return False
    return hmac.compare_digest(sign_download(room_code, transfer_id, exp_ts), sig)


def parse_range_header(range_header: str | None, size: int) -> tuple[int, int] | None:
    """解析 HTTP Range 头（仅支持单区间 bytes=start-end），返回闭区间 (start, end)。

    解析失败或区间非法时返回 None，调用方按完整下载（200）处理。
    """
    if not range_header or size <= 0:
        return None
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    try:
        if start_s == "":
            # bytes=-N：最后 N 字节
            if not end_s:
                return None
            start = max(0, size - int(end_s))
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None

    end = min(end, size - 1)
    if start > end or start >= size:
        return None
    return start, end


async def iter_file_range(path: Path, start: int, end: int, chunk_size: int = 1024 * 1024):
    """按字节区间异步迭代文件内容，供 HTTP 206 断点续传使用。"""
    async with aiofiles.open(path, "rb") as f:
        await f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = await f.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
