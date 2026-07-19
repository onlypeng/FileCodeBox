# @Time    : 2023/8/15 22:00
# @Author  : Lan
# @File    : tasks.py
# @Software: PyCharm
import asyncio
import datetime
import logging
import os

from tortoise.expressions import Q

from apps.base.models import FileCodes, UploadChunk, CollectionBox, CollectionFile, PresignUploadSession
from apps.base.utils import ip_limit, get_chunk_file_path_name
from core.config import refresh_settings, should_refresh_config
from core.settings import settings, data_root
from core.storage import FileStorageInterface, storages
from core.utils import get_now


async def delete_expire_files():
    while True:
        try:
            if should_refresh_config():
                await refresh_settings()
            file_storage: FileStorageInterface = storages[settings.file_storage]()
            # 遍历 share目录下的所有文件夹，删除空的文件夹，并判断父目录是否为空，如果为空也删除
            if settings.file_storage == "local":
                for root, dirs, files in os.walk(f"{data_root}/share/data"):
                    if not dirs and not files:
                        os.rmdir(root)
            await ip_limit["error"].remove_expired_ip()
            await ip_limit["upload"].remove_expired_ip()
            now = await get_now()
            # 过期条件：时间过期 或 count 用尽
            expire_data = await FileCodes.filter(
                Q(expired_at__isnull=False, expired_at__lt=now) | Q(expired_count=0)
            ).exclude(
                # 排除 forever 模式（expired_count=-1 且无过期时间）
                Q(expired_count=-1, expired_at__isnull=True) |
                # 排除 forever 模式下 count 为 0 的情况（forever 不因 count 用尽而删除）
                Q(expired_count=0, expire_style="forever")
            ).all()
            for exp in expire_data:
                try:
                    await file_storage.delete_file(exp)
                except Exception as e:
                    logging.error(f"删除过期文件失败 code={exp.code}: {e}")
                try:
                    await exp.delete()
                except Exception as e:
                    logging.error(f"删除记录失败 code={exp.code}: {e}")
        except Exception as e:
            logging.error(e)
        finally:
            await asyncio.sleep(600)


async def clean_incomplete_uploads():
    while True:
        try:
            if should_refresh_config():
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
                    logging.error(
                        f"清理分片文件失败 upload_id={session.upload_id}: {e}"
                    )

                try:
                    await UploadChunk.filter(upload_id=session.upload_id).delete()
                    logging.info(f"已清理过期上传会话 upload_id={session.upload_id}")
                except Exception as e:
                    logging.error(
                        f"删除分片记录失败 upload_id={session.upload_id}: {e}"
                    )

        except Exception as e:
            logging.error(f"清理未完成上传任务异常: {e}")
        finally:
            await asyncio.sleep(3600)


async def clean_expired_presign_sessions():
    """定期清理过期预签名上传会话"""
    while True:
        try:
            if should_refresh_config():
                await refresh_settings()
            now = await get_now()
            expired_sessions = await PresignUploadSession.filter(expires_at__lt=now).all()
            for session in expired_sessions:
                try:
                    if session.mode == "direct":
                        file_storage: FileStorageInterface = storages[settings.file_storage]()
                        if await file_storage.file_exists(session.save_path):
                            temp_file_code = FileCodes(
                                file_path=os.path.dirname(session.save_path),
                                uuid_file_name=os.path.basename(session.save_path),
                            )
                            await file_storage.delete_file(temp_file_code)
                except Exception as e:
                    logging.error(
                        f"清理预签名上传文件失败 upload_id={session.upload_id}: {e}"
                    )

                try:
                    await session.delete()
                    logging.info(f"已清理过期预签名上传会话 upload_id={session.upload_id}")
                except Exception as e:
                    logging.error(
                        f"删除预签名上传记录失败 upload_id={session.upload_id}: {e}"
                    )

        except Exception as e:
            logging.error(f"清理过期预签名上传会话异常: {e}")
        finally:
            await asyncio.sleep(600)


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
                logging.error(f"事件处理器错误 {event_name}: {e}")

        # 默认日志
        logging.info(f"[Event] {event_name}: {data}")


# 注册默认事件处理器
async def _log_collection_created(data: dict):
    logging.info(f"收件箱已创建: collection_code={data.get('collection_code')}, delivery_code={data.get('delivery_code')}")


async def _log_file_delivered(data: dict):
    logging.info(f"文件已投递: file_id={data.get('file_id')}, collection_code={data.get('collection_code')}")


async def _log_delivery_failed(data: dict):
    logging.warning(f"文件投递失败: file_id={data.get('file_id')}, error={data.get('error')}")


CollectionEvents.on("collection.created", _log_collection_created)
CollectionEvents.on("file.delivered", _log_file_delivered)
CollectionEvents.on("file.delivery_failed", _log_delivery_failed)


async def clean_expired_collections():
    """定期清理过期收件箱（仅清理超时 uploading 状态的投递记录）"""
    while True:
        try:
            if should_refresh_config():
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
                logging.info(f"投递记录标记为失败: id={sf.id}")

        except Exception as e:
            logging.error(f"清理过期收件箱异常: {e}")
        finally:
            await asyncio.sleep(600)


async def clean_admin_rate_limit():
    """定期清理管理后台速率限制缓存"""
    from apps.admin.views import admin_rate_limit
    while True:
        try:
            await admin_rate_limit.cleanup()
        except Exception as e:
            logging.error(f"清理管理后台速率限制异常: {e}")
        finally:
            await asyncio.sleep(300)
