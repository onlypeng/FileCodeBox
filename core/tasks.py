# @Time    : 2023/8/15 22:00
# @Author  : Lan
# @File    : tasks.py
# @Software: PyCharm
import asyncio
import datetime
from core.logger import logger
import os
from typing import Optional

from tortoise.expressions import Q

from apps.base.models import FileCodes, UploadChunk, CollectionBox, CollectionFile, Room, RoomMessage, RoomCachedFile
from apps.base.utils import ip_limit, get_chunk_file_path_name
from core.config import refresh_settings
from core.settings import settings, data_root
from core.room_cache import delete_room_dir, delete_room_file, sweep_incoming
from core.file_dedup import is_file_referenced_elsewhere
from core.storage import FileStorageInterface, storages
from core.utils import get_now
from core.websocket import ws_manager


async def _retention_delta() -> Optional[datetime.timedelta]:
    """过期保存时间 → 宽限时长；forever/无效配置返回 None 表示不自动清理。"""
    style = getattr(settings, "expiredRetentionStyle", "forever")
    value = int(getattr(settings, "expiredRetentionValue", 0) or 0)
    if style == "forever" or value <= 0:
        return None
    units = {"day": 86400, "hour": 3600, "minute": 60}
    seconds = value * units.get(style, 0)
    if seconds <= 0:
        return None
    return datetime.timedelta(seconds=seconds)


async def _expired_point(obj) -> Optional[datetime.datetime]:
    """记录/收件箱/房间的过期时间点：时间型取 expired_at；
    次数型（count）无绝对时间时按创建时间兜底。"""
    if getattr(obj, "expired_at", None) is not None:
        return obj.expired_at
    return getattr(obj, "created_at", None)


async def delete_expire_files():
    """清理过期文件/分享记录（含文件实体）

    过期判定：时间型 expired_at 到期；次数型（count）次数用尽。
    过期后再保留"过期保存时间"宽限，到期由本任务删除记录与存储文件。
    """
    while True:
        try:
            await refresh_settings()
            file_storage: FileStorageInterface = storages[settings.file_storage]()
            # 遍历 share目录下的所有文件夹，删除空的文件夹，并判断父目录是否为空，如果为空也删除
            if settings.file_storage == "local":
                for root, dirs, files in os.walk(f"{data_root}/share/data"):
                    if not dirs and not files:
                        os.rmdir(root)
            await ip_limit["error"].remove_expired_ip()
            await ip_limit["upload"].remove_expired_ip()
            retention = await _retention_delta()
            now = await get_now()
            # 已过期的记录：次数型（expired_count >= 0 且耗尽）或时间型（expired_at 到期）。
            # 注意 count（按次）型文件的 expired_at 为创建后 365 天的兜底时间，不得按时间误删——
            # 只有次数真正用尽（expired_count = 0）才算过期。
            expire_data = await FileCodes.filter(
                Q(expired_count=0)
                | (
                    Q(expired_count__lt=0)
                    & Q(expired_at__isnull=False)
                    & Q(expired_at__lt=now)
                )
            ).all()
            for exp in expire_data:
                expired_point = await _expired_point(exp)
                if retention is not None and expired_point is not None and expired_point + retention > now:
                    continue
                try:
                    # 秒传会让多条记录共享同一份物理文件，删除前必须确认
                    # 没有别的记录还在引用它，否则会连带删掉别人的文件
                    if not await is_file_referenced_elsewhere(
                        exp.file_path, exp.uuid_file_name, exp.id
                    ):
                        await file_storage.delete_file(exp)
                    else:
                        logger.info(f"文件仍被其它记录引用，仅删除记录: code={exp.code}")
                except Exception as e:
                    logger.error(f"删除过期文件失败 code={exp.code}: {e}")
                try:
                    await exp.delete()
                except Exception as e:
                    logger.error(f"删除记录失败 code={exp.code}: {e}")
        except Exception as e:
            logger.error(e)
        finally:
            await asyncio.sleep(600)


async def clean_incomplete_uploads():
    while True:
        try:
            await refresh_settings()
            file_storage: FileStorageInterface = storages[settings.file_storage]()
            expire_hours = getattr(settings, "chunk_expire_hours", 24)
            now = await get_now()
            expire_time = now - datetime.timedelta(hours=expire_hours)
            expired_sessions = await UploadChunk.filter(
                chunk_index=-1, created_at__lt=expire_time
            ).all()

            for session in expired_sessions:
                try:
                    save_path = session.save_path
                    if not save_path:
                        _, _, _, _, save_path = await get_chunk_file_path_name(
                            session.file_name, session.upload_id
                        )
                    await file_storage.clean_chunks(session.upload_id, save_path)
                except Exception as e:
                    logger.error(
                        f"清理分片文件失败 upload_id={session.upload_id}: {e}"
                    )

                try:
                    await UploadChunk.filter(upload_id=session.upload_id).delete()
                    logger.info(f"已清理过期上传会话 upload_id={session.upload_id}")
                except Exception as e:
                    logger.error(
                        f"删除分片记录失败 upload_id={session.upload_id}: {e}"
                    )

        except Exception as e:
            logger.error(f"清理未完成上传任务异常: {e}")
        finally:
            await asyncio.sleep(3600)


# ============ 收件箱事件定义 ============

class CollectionEvents:
    """收件箱事件处理器"""
    _handlers = {}

    @classmethod
    def on(cls, event_name: str, handler):
        """注册事件处理器"""
        if event_name not in cls._handlers:
            cls._handlers[event_name] = []
        cls._handlers[event_name].append(handler)

    @classmethod
    async def emit(cls, event_name: str, data: dict):
        """触发事件"""
        handlers = cls._handlers.get(event_name, [])
        for handler in handlers:
            try:
                await handler(data)
            except Exception as e:
                logger.error(f"事件处理器错误 {event_name}: {e}")

        # 默认日志
        logger.info(f"[Event] {event_name}: {data}")


# 注册默认事件处理器
async def _log_collection_created(data: dict):
    logger.info(f"收件箱已创建: collection_code={data.get('collection_code')}, delivery_code={data.get('delivery_code')}")


async def _log_file_delivered(data: dict):
    logger.info(f"文件已投递: file_id={data.get('file_id')}, collection_code={data.get('collection_code')}")


async def _log_delivery_failed(data: dict):
    logger.warning(f"文件投递失败: file_id={data.get('file_id')}, error={data.get('error')}")


CollectionEvents.on("collection.created", _log_collection_created)
CollectionEvents.on("file.delivered", _log_file_delivered)
CollectionEvents.on("file.delivery_failed", _log_delivery_failed)


async def clean_expired_collections():
    """定期清理过期收件箱：
    - 超时 uploading 状态的投递记录标记为失败
    - 已过期（整箱过期）且超过"过期保存时间"宽限的收件箱，删除整箱及其全部文件
    """
    while True:
        try:
            await refresh_settings()
            now = await get_now()

            # 清理超时的 uploading 状态投递记录
            expire_time = now - datetime.timedelta(hours=1)
            stale_files = await CollectionFile.filter(
                status="uploading", created_at__lt=expire_time
            ).all()
            for sf in stale_files:
                sf.status = "failed"
                await sf.save()
                logger.info(f"投递记录标记为失败: id={sf.id}")

            # 整箱过期 + 宽限到期 → 删除收件箱（含全部文件与记录）
            retention = await _retention_delta()
            if retention is not None:
                boxes = await CollectionBox.all().exclude(expire_style="forever")
                for box in boxes:
                    if not await box.is_expired():
                        continue
                    expired_point = await _expired_point(box)
                    if expired_point is None or expired_point + retention > now:
                        continue
                    await _delete_collection_with_files(box)
                    logger.info(f"已清理过期收件箱: code={box.collection_code}")

        except Exception as e:
            logger.error(f"清理过期收件箱异常: {e}")
        finally:
            await asyncio.sleep(600)


async def _delete_collection_with_files(box) -> None:
    """删除收件箱及其全部文件（文件实体 + 关联记录）"""
    from core.storage import FileStorageInterface, storages
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    collection_files = await CollectionFile.filter(collection=box).all()
    for cf in collection_files:
        if cf.file_code_id:
            file_code = await FileCodes.filter(id=cf.file_code_id).first()
            if file_code:
                try:
                    # 同上：共享文件需先确认无其它记录引用
                    if not await is_file_referenced_elsewhere(
                        file_code.file_path, file_code.uuid_file_name, file_code.id
                    ):
                        await file_storage.delete_file(file_code)
                    else:
                        logger.info(f"文件仍被其它记录引用，仅删除记录: code={file_code.code}")
                except Exception as e:
                    logger.error(f"删除收件箱文件失败 code={file_code.code}: {e}")
                try:
                    await file_code.delete()
                except Exception as e:
                    logger.error(f"删除收件箱文件记录失败 code={file_code.code}: {e}")
    await box.delete()


async def clean_expired_rooms():
    """定期清理过期实时房间（统一直连快传房间）
    count 模式：剩余次数扣尽即视为过期（由 is_expired 统一判断）；
    过期后再保留"过期保存时间"宽限，到期删除。"""
    while True:
        try:
            await refresh_settings()
            now = await get_now()
            retention = await _retention_delta()

            # 逐房间判断过期（forever 永不过期，count 按剩余次数，其余按时间）
            rooms = await Room.all().exclude(expire_style="forever")
            for room in rooms:
                if not await room.is_expired():
                    continue
                if retention is not None:
                    expired_point = await _expired_point(room)
                    if expired_point is None or expired_point + retention > now:
                        continue
                await room.delete()
                # 房间删除/过期：同步清理其保存的消息与缓存文件元信息
                try:
                    await RoomMessage.filter(room_code=room.room_code).delete()
                except Exception:
                    pass
                try:
                    # 先删数据库元信息，再删落盘文件（顺序不能反，否则先删记录会留下无人回收的孤儿文件）
                    await RoomCachedFile.filter(room_code=room.room_code).delete()
                    delete_room_dir(room.room_code)
                except Exception:
                    pass
                logger.info(f"已清理过期房间: code={room.room_code}")

        except Exception as e:
            logger.error(f"清理过期房间异常: {e}")
        finally:
            await asyncio.sleep(600)


async def clean_expired_room_cache_files():
    """定期清理到期的房间服务器缓存文件（房间仍存活，仅该文件 expires_at 到期）。

    只处理显式设置了 expires_at 的记录；expires_at 为 NULL 表示"跟随房间生命周期"，
    由 clean_expired_rooms 在房间回收时统一清理，此处不重复扫描。
    """
    while True:
        try:
            now = await get_now()
            # NULL 不参与比较（SQLite 下 NULL < now 为假），天然只命中设置了到期时间的记录
            expired = await RoomCachedFile.filter(expires_at__lt=now).all()
            for entry in expired:
                if entry.storage_key:
                    await delete_room_file(entry.storage_key)
                # 同步清空历史里同名文件消息的落盘标记：否则历史仍显示"可下载"，
                # 实际文件已随本次清理删除，点击只会 404
                await RoomMessage.filter(
                    room_code=entry.room_code, msg_type="file", content=entry.transfer_id
                ).update(storage_key="")
                await entry.delete()
            if expired:
                logger.info(f"已清理到期房间缓存文件: {len(expired)} 个")
            # 兜底回收上传中断残留的临时文件（崩溃时"写完临时文件但还没改名"的那批）。
            # 同步文件 I/O 会阻塞事件循环，放到线程里执行。
            orphan = await asyncio.to_thread(sweep_incoming)
            if orphan:
                logger.info(f"已回收房间缓存临时文件: {orphan} 个")
        except Exception as e:
            logger.error(f"清理房间缓存文件异常: {e}")
        finally:
            await asyncio.sleep(300)


async def sweep_direct_connections():
    """定期心跳探测直连房间连接，剔除断开但未清理的连接（防止在线人数虚高）"""
    while True:
        try:
            await ws_manager.sweep_rooms()
        except Exception as e:
            logger.error(f"清扫直连连接异常: {e}")
        finally:
            await asyncio.sleep(20)
