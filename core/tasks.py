"""后台周期性任务：过期清理、完成不了的上传回收、缓存/审计回收、WAL checkpoint。

## 问题

本项目的资源几乎都不是"用到立刻释放"的：

- 文件/收件箱/房间过期后还要留一段"过期保存时间"（宽限），到期才能真删；
  否则用户隔天回来发现东西没了，会认为数据丢失
- 分片上传中途放弃 → 分片文件与 UploadChunk 记录永远留着
- SQLite 的 `-wal` 文件在长读事务存在时不回收，"备份 data 目录"拿不到完整数据
- 房间 WS 连接断开但没清理时，在线人数虚高，房间看起来"一直有人"

这些都需要周期性的兜底，而不能指望请求链路自己收拾干净。

## 方案

统一形态：`while True: try ... except(只记录不抛出) finally: await asyncio.sleep(N)`。

- **异常必须吞掉并记录**：后台任务挂掉不会被任何人发现（没有请求会报错），
  一个未捕获异常让清盘任务静默死掉，几天后磁盘被写满才暴露。
- **每轮开头 `refresh_settings()`**：管理员在后台改宽限期/保留期，下一次轮询
  才生效；不做这一步就会出现"改了配置但永远不生效"的伪故障。
- 周期长短按"清理迟到多久可接受"而不是 CPU 成本：文件 10 分钟、房间缓存
  5 分钟、连接清扫 20 秒、审计/checkpoint 1 小时。

## 刻意不做

- **不用 APScheduler / Celery beat**：只有一个进程需要处理的是"定期做一件小事"，
  引入调度框架要额外管理持久化 job 与进程模型；并且 `finally: sleep` 的写法让
  "任务之间没有重叠"天然成立——调度器的错过/并发语义反而是多余的心智负担。
- **不做任务级分布式协调**：多实例部署时每个实例都会跑这些任务。清理类操作是
  幂等的（删除不存在的记录不报错、删除已删文件同样不报错），重复执行的代价
  远小于引入选主机制的复杂度。唯一例外是SQLite 单写者部署本身就不允许多实例，
  见 core/capabilities。
- **不在删除前做二次确认/回收站**：过期 vs 未过期的判定已在 SQL 层完成，
  再留一层"逻辑删除"只会让磁盘回收的时间点变得不可预测。
"""
import asyncio
import datetime
from core.logger import logger
import os
from typing import Optional

from tortoise.expressions import Q

from apps.base.models import FileCodes, UploadChunk, CollectionBox, CollectionFile, Room, RoomMessage, RoomCachedFile, AuditLog
from apps.base.utils import ip_limit, get_chunk_file_path_name
from core.config import refresh_settings
from core.settings import settings, data_root
from core.room_cache import delete_room_dir, delete_room_file, sweep_incoming, delete_room_file_if_unreferenced
from core.db_compat import db_type
from core.file_dedup import delete_physical_files
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


def _prune_empty_dirs(root: str) -> int:
    """删除目录下的空文件夹（含递归后变空的父目录）。

    同步实现，由调用方用 asyncio.to_thread 包起来，避免阻塞事件循环。
    """
    removed = 0
    if not os.path.isdir(root):
        return removed
    base = os.path.abspath(root)
    # topdown=False：先处理子目录，这样子目录被删后变空的父目录也能一并清掉
    for dirpath, dirs, files in os.walk(root, topdown=False):
        # 根目录本身不删：它是存储根，删掉后并发写入会出问题
        if os.path.abspath(dirpath) == base:
            continue
        # 用实际内容判断：walk 给的 dirs/files 是遍历时的快照，
        # 子目录可能已在本次循环中被删，照快照判断会漏掉"刚变空的父目录"
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed += 1
        except OSError:
            # 刚被占用/并发删除：忽略，下次清理再处理
            pass
    return removed


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
            # os.walk 是同步磁盘 I/O，文件多时会阻塞事件循环（期间所有请求都排队），
            # 因此放进线程执行
            if settings.file_storage == "local":
                await asyncio.to_thread(_prune_empty_dirs, f"{data_root}/share/data")
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
                # retention=None 表示「不自动清理」（forever / 无效配置），整轮跳过。
                # 不能写成 `retention is not None and ...`：那样 None 会落到删除分支，
                # 与「永久保留」语义完全相反，造成不可逆数据丢失。
                if retention is None:
                    break
                expired_point = await _expired_point(exp)
                if expired_point is not None and expired_point + retention > now:
                    continue
                await delete_physical_files(file_storage, exp, logger)
                try:
                    await exp.delete()
                except Exception as e:
                    logger.error(f"删除记录失败 code={exp.code}: {e}")
        except Exception as e:
            logger.error(e)
        finally:
            await asyncio.sleep(600)


async def clean_incomplete_uploads():
    """清理"上传没走完"的分片会话。

    判定依据：`UploadChunk` 里 chunk_index=-1 的那条是会话哨兵记录，
    它的 created_at 超过 `chunkExpireHours`（默认 24 小时，后台可配）即认为用户放弃了。
    该键已登记在 core/settings.py 的 DEFAULT_CONFIG，经 UpdateConfigRequest 白名单
    落库，后台「系统设置 → 上传」可调整；读取点再钳一次下界，防止非法值把哨兵
    当成刚创建而永远不回收。

    顺序是先删存储实体、再删库记录：反过来的话一旦删库后清理存储失败，
    分片文件就成了没人回收的孤儿。
    """
    while True:
        try:
            await refresh_settings()
            file_storage: FileStorageInterface = storages[settings.file_storage]()
            expire_hours = max(1, int(getattr(settings, "chunkExpireHours", 24) or 24))
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
    """收件箱事件总线（进程内、内存态）。

    handler 表是类属性：注册即全局生效，且**不做去重**——同一 handler 重复注册
    会执行多次（默认 handler 在模块导入时注册，模块被重复导入时会叠加）。
    刻意不引入第三方事件库：这里只有三个"写日志"的默认消费者，
    引入框架带来的注册/生命周期管理成本大于收益。
    """
    _handlers = {}

    @classmethod
    def on(cls, event_name: str, handler):
        """注册事件处理器"""
        if event_name not in cls._handlers:
            cls._handlers[event_name] = []
        cls._handlers[event_name].append(handler)

    @classmethod
    async def emit(cls, event_name: str, data: dict):
        """按注册顺序**串行**触发 handler。

        单个 handler 抛异常只被记录、不影响后续 handler 执行：事件是旁路通知，
        一个消费者写日志失败不该让"文件已投递"这件事对其它消费者消失。
        返回值始终为 None —— 事件没有"被接住才有意义"的语义。
        """
        handlers = cls._handlers.get(event_name, [])
        for handler in handlers:
            try:
                await handler(data)
            except Exception as e:
                logger.error(f"事件处理器错误 {event_name}: {e}")

        # 默认日志
        logger.info(f"[Event] {event_name}: {data}")


# 注册默认事件处理器（模块导入时执行，故 import 本模块即有副作用）
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
    """连带删除某个收件箱的文件实体、关联文件记录与收件箱本身。

    不做整体事务：-file 的 storage 操作（S3/OneDrive/WebDAV 的网络调用）无法和
    ORM 处在同一事务里。因此顺序是"先删文件实体 → 再删 FileCodes → 最后删箱子"，
    中途失败会留下孤儿记录但不会留下"看得见却下载不了"的幽灵文件。
    """
    from core.storage import FileStorageInterface, storages
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    collection_files = await CollectionFile.filter(collection=box).all()
    for cf in collection_files:
        if cf.file_code_id:
            file_code = await FileCodes.filter(id=cf.file_code_id).first()
            if file_code:
                await delete_physical_files(file_storage, file_code, logger)
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
                # 与收件箱口径一致：None = 不自动清理，整轮跳过
                if retention is None:
                    break
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
                    # 去重共享：删之前先确认没有其它条目/消息仍引用这份落盘文件，
                    # 否则那些条目的下载会集体 404（见 core/room_cache 收口函数）。
                    await delete_room_file_if_unreferenced(
                        entry.room_code, entry.storage_key,
                        exclude_cached_id=entry.id, exclude_transfer_id=entry.transfer_id,
                    )
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


async def clean_expired_audit_logs():
    """定期清理超过保留期的审计日志。

    下载类操作会持续写审计，长期运行若不清理 audit_log 表会无限增长。
    auditLogRetentionDays = 0 时不清理（由运维自行归档）。
    """
    while True:
        try:
            days = int(getattr(settings, "auditLogRetentionDays", 90) or 0)
            if days > 0:
                cutoff = await get_now() - datetime.timedelta(days=days)
                deleted = await AuditLog.filter(created_at__lt=cutoff).delete()
                if deleted:
                    logger.info(f"已清理过期审计日志: {deleted} 条（保留 {days} 天）")
        except Exception as e:
            logger.error(f"清理审计日志异常: {e}")
        finally:
            # 每天检查一次即可
            await asyncio.sleep(3600)


async def sqlite_wal_checkpoint():
    """定期执行 SQLite WAL checkpoint（仅 SQLite 生效）。

    SQLite 在 WAL 模式下会把写入先放进 `-wal` 文件，默认每 1000 页自动
    checkpoint 一次；但写入繁忙或存在长读事务时 checkpoint 会被推迟，
    `-wal` 会持续增长且不会自动回收。TRUNCATE 模式把内容写回主库并截断
    `-wal`，让"备份 data 目录"真的拿到完整数据（否则只拷 .db 会丢近期写入）。
    """
    while True:
        try:
            if db_type() == "sqlite":
                from tortoise import connections

                conn = connections.get("default")
                await conn.execute_query("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            logger.error(f"SQLite WAL checkpoint 失败: {e}")
        finally:
            # 每小时一次即可：checkpoint 本身有 I/O 成本
            await asyncio.sleep(3600)


async def sweep_direct_connections():
    """定期心跳探测直连房间连接，剔除断开但未清理的连接（防止在线人数虚高）"""
    while True:
        try:
            await ws_manager.sweep_rooms()
        except Exception as e:
            logger.error(f"清扫直连连接异常: {e}")
        finally:
            await asyncio.sleep(20)
