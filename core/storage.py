"""可插拔存储层：本地 / S3 / OneDrive / OpenDAL / WebDAV 的统一门面。

## 问题

自建文件分享最常见的运维诉求是"我不想把文件放在本机磁盘上"。但这几种后端的
接口差异极大：本地是文件路径、S3 是 bucket + key + 预签名、OneDrive 是 Graph
SDK（同步调用）、WebDAV 是 HTTP 动词（PUT / MKCOL / PROPFIND）。把这些差异散进
路由层的结果是——每支持一个新后端要改十几个上传/下载入口，任何一处漏改都表现为
"某条路径下文件莫名消失"，且很难定位。

## 方案

一个接口 `FileStorageInterface` + `storages` 注册表。业务层只持有
`storages[settings.file_storage]()`，不感知后端类型。设计约束：

- **路径是后端语义的、相对 root 的字符串**：本地是相对 data_root 的相对路径，
  S3 / WebDAV 是对象 key。调用方不要拼绝对路径，否则换了后端全部失效。
- **分片上传三件套**（save_chunk / merge_chunks / clean_chunks）必须成套实现：
  它们之间有"先写临时或 .part 文件、再原子改名"的隐含契约，缺一个就会出现
  "上传成功但文件没有合并出来"。
- **每个后端自己决定怎么流式化**：能流式就流式（本地下载直接交 FileResponse、
  S3/WebDAV 合并走生成器）；凡是写成"先落地临时文件、再整体读出"的
  （OpenDAL / WebDAV / OneDrive 合并）都是因为该后端没有可用的流式接口，
  用临时文件换取"不全量驻留内存"。
- **`get_file_response` 优先给直链、不能直链才代理**：能直链的给预签名 URL，
  不能的由本服务代理流式转发，并尽力补 `Content-Length`——缺了它，浏览器下载
  看不到进度条，CDN 也无法做范围缓存。

## 刻意不做

- **不把它做成严格的抽象基类**（没有 ABC / abstractmethod）。方法抛
  NotImplementedError 表示"该后端不支持这个能力"，调用方应当自行降级到别的路径
  （例如 `generate_presigned_upload_url` 默认返回 None，调用方据此退化为代理上传）。
  改用 ABC 会把"部分实现"变成不可启动的结构性错误，代价大于收益。
- **不在这一层做删除保护**。`delete_file` 不判断"是否还有别的记录引用这份物理
  文件"：秒传会让多条 FileCodes 共享同一份文件，而这份判断依赖 models，
  放在这里会形成循环依赖。引用检查统一放在 `core/file_dedup.py`，由清理任务
  显式调用（见 `core/tasks.py` 对应注释）。
- **不管磁盘配额**：那是 `core/quota.py` 的职责，这里只管"怎么存"。
- **不缓存 storage 实例**：每次调用现场构造。构造成本很低（本地/S3 只是读几个
  配置项），而长期持有会让"用户改了存储配置却不重启"变成半生效的诡异状态。
"""
import hashlib
import os
import tempfile
import zipfile
from core.logger import logger
import shutil
from typing import Optional, AsyncGenerator
from urllib.parse import quote, unquote

import aiofiles
import aiohttp
import asyncio
from pathlib import Path
import datetime
import re
import aioboto3
from botocore.config import Config
from fastapi import HTTPException, Response, UploadFile
from core.response import APIResponse
from core.settings import data_root, settings
from apps.base.models import FileCodes, UploadChunk
from core.utils import get_file_url, sanitize_filename
from fastapi.responses import FileResponse, StreamingResponse


class FileStorageInterface:
    """存储后端契约。

    不是抽象基类：这里的方法可以只实现一部分。抛 NotImplementedError 表示该后端
    不支持这个能力，调用方负责降级（详见模块头的"刻意不做"）。

    所有 `save_path` 都是**相对各自 root 的路径字符串**：
    本地相对 `data_root`，对象存储即对象 key。传绝对路径在多数后端上会静默变成
    奇怪的顶层目录，务必不要那么写。
    """

    async def save_file(self, file: UploadFile, save_path: str):
        """
        保存文件
        """
        raise NotImplementedError


    async def delete_file(self, file_code: FileCodes):
        """
        删除文件

        无副作用保证：文件不存在时不抛异常（后端各不相同，统一按幂等删除实现），
        重复删除是安全的。**不检查是否还有别的 FileCodes 引用同一份物理文件**，
        秒传场景请调用方先用 core.file_dedup 判断。
        """
        raise NotImplementedError

    async def get_file_url(self, file_code: FileCodes):
        """
        获取文件分享的url

        约定：返回**可直接被使用的绝对 URL**。若后端给不出公网直链，
        则退回 core.utils.get_file_url(code) 拿服务端中转地址
        （带签名、有时效，见 core/utils.py 模块头）。
        """
        raise NotImplementedError

    async def get_file_response(self, file_code: FileCodes):
        """
        获取文件响应（FastAPI Response）

        约定：文件不存在时返回 APIResponse(code=404, detail="文件已过期删除")
        **而不是抛异常** —— 调用方是 HTTP 路由，过期/删除是常态业务分支。
        """
        raise NotImplementedError

    async def save_chunk(self, upload_id: str, chunk_index: int, chunk_data: bytes, chunk_hash: str, save_path: str):
        """保存单个分片。

        chunk_hash 是上传方声明的 SHA-256；本方法**不负责校验**（IO 只管落地），
        真正的校验统一在 merge_chunks 做——那时才能拿到数据库里记录的权威哈希。
        """
        raise NotImplementedError

    async def merge_chunks(self, upload_id: str, chunk_info: UploadChunk, save_path: str) -> tuple[str, str]:
        """合并分片并返回 (落盘路径, 全文件 SHA-256)。

        返回值里的哈希是本 merge 过程中**服务端算出**的权威值，用于秒传去重；
        任何分片缺失或哈希不符时抛 ValueError（调用方据此返回 400）。
        """
        raise NotImplementedError

    async def generate_presigned_upload_url(self, save_path: str, expires_in: int = 900) -> Optional[str]:
        """
        生成预签名上传 URL（可选能力）：不支持直传的后端返回 None，**由调用方
        决定是否退化为"经本服务代理上传"**。刻意不抛 NotImplementedError：直传
        是优化路径，不是契约的一部分。
        :param save_path: 文件保存路径
        :param expires_in: URL过期时间（秒），默认15分钟
        :return: 预签名URL，如果不支持直传则返回None
        """
        return None  # 默认不支持直传，使用代理模式

    async def file_exists(self, save_path: str) -> bool:
        """
        检查文件是否存在
        :param save_path: 文件路径
        :return: 文件是否存在
        """
        raise NotImplementedError

    async def clean_chunks(self, upload_id: str, save_path: str):
        """
        清理临时分片文件（best-effort）

        中途失败只记日志不抛：它的调用方是后台清理任务，
        一次清理失败不该让整轮清理中止（下次轮询会再试）。
        :param upload_id: 上传会话ID
        :param save_path: 文件保存路径
        """
        raise NotImplementedError

    async def get_file_stream(self, save_path: str) -> AsyncGenerator[bytes, None]:
        """按块产出文件内容（供 ZIP 打包等"必须顺序读"的场景）。

        文件不存在时抛 FileNotFoundError（与其它方法"返回 404 壳对象"不同，
        这里没有 HTTP 语义）。块的边界不保证与写入时的分片一致。
        :param save_path: 文件存储路径
        :return: 异步字节流生成器
        """
        raise NotImplementedError

    async def delete_file_by_path(self, storage_path: str, uuid_file_name: str):
        """
        按物理路径删除（不经 FileCodes），用于没有对应记录的文件（如清理孤儿分片）。
        同样幂等：不存在不报错。
        :param storage_path: 存储目录路径
        :param uuid_file_name: 文件名
        """
        raise NotImplementedError

    async def get_file_size(self, save_path: str) -> Optional[int]:
        """
        获取文件大小（字节）；拿不到返回 None。

        刻意不抛异常也不返回 0：0 会被误当成"空文件"，而 None 明确表示"未知"，
        调用方的配额/进度计算才有机会走另一条分支。
        :param save_path: 文件路径
        :return: 文件大小（字节），不支持则返回None
        """
        return None


def _rmtree_quiet(path) -> str:
    """删除目录树，失败时返回错误信息而非抛出（供 asyncio.to_thread 调用）。"""
    try:
        shutil.rmtree(path)
        return ""
    except Exception as e:
        return str(e)


class SystemFileStorage(FileStorageInterface):
    """本地磁盘后端（默认）。root 是 `data_root`，一切路径相对它解析。

    同步文件 IO 一律用 `asyncio.to_thread` 包起来：本地后端的 IO 全部阻塞事件
    循环，本地磁盘恰恰最容易变成"文件多了整体就卡住"的来源。
    """

    def __init__(self):
        self.chunk_size = 256 * 1024
        self.root_path = data_root

    def _save(self, file, save_path):
        with open(save_path, "wb") as f:
            chunk = file.read(self.chunk_size)
            while chunk:
                f.write(chunk)
                chunk = file.read(self.chunk_size)

    async def save_file(self, file: UploadFile, save_path: str):
        path_obj = Path(save_path)
        directory = str(path_obj.parent)
        # 二次消毒：调用方给的路径只保证"相对 data_root"，文件名仍可能带 "../"
        # 或 Windows 保留字符（不同后端对这个越界的容忍度不同，这里统一收紧）
        filename = await sanitize_filename(path_obj.name)
        # 构建安全的完整保存路径
        safe_save_path = self.root_path / directory / filename
        # 确保目录存在
        if not safe_save_path.parent.exists():
            safe_save_path.parent.mkdir(parents=True)
        await asyncio.to_thread(self._save, file.file, safe_save_path)


    async def delete_file(self, file_code: FileCodes):
        # 幂等：存在就删、不存在直接返回。清理任务会并发跑同一个文件，
        # 抛 FileNotFoundError 会让整轮清理把这个文件的记录永远留着
        save_path = self.root_path / await file_code.get_file_path()
        if save_path.exists():
            save_path.unlink()

    async def get_file_url(self, file_code: FileCodes):
        return await get_file_url(file_code.code)

    async def get_file_response(self, file_code: FileCodes):
        file_path = self.root_path / await file_code.get_file_path()
        if not file_path.exists():
            return APIResponse(code=404, detail="文件已过期删除")
        filename = f"{file_code.prefix}{file_code.suffix}"
        encoded_filename = quote(filename, safe='')
        content_disposition = f"attachment; filename*=UTF-8''{encoded_filename}"
        
        # 尝试获取文件系统大小，如果成功则设置 Content-Length
        headers = {"Content-Disposition": content_disposition}
        try:
            content_length = file_path.stat().st_size
            headers["Content-Length"] = str(content_length)
        except Exception:
            # 如果获取文件大小失败，则不提供 Content-Length
            pass
        
        return FileResponse(
            file_path,
            media_type="application/octet-stream",
            headers=headers,
            filename=filename  # 保留原始文件名以备某些场景使用
        )

    async def save_chunk(self, upload_id: str, chunk_index: int, chunk_data: bytes, chunk_hash: str, save_path: str):
        """
        保存分片文件到本地文件系统
        :param upload_id: 上传会话ID
        :param chunk_index: 分片索引
        :param chunk_data: 分片数据
        :param chunk_hash: 分片哈希值
        :param save_path: 文件保存路径
        """
        chunk_dir = self.root_path / save_path
        chunk_path = chunk_dir.parent / 'chunks' / upload_id / f"{chunk_index}.part"
        if not chunk_path.parent.exists():
            chunk_path.parent.mkdir(parents=True, exist_ok=True)
        # 使用临时文件写入，确保原子性
        temp_path = chunk_path.with_suffix('.tmp')
        try:
            async with aiofiles.open(temp_path, "wb") as f:
                await f.write(chunk_data)
            # 原子重命名
            temp_path.rename(chunk_path)
        except Exception as e:
            if temp_path.exists():
                temp_path.unlink()
            raise e

    async def merge_chunks(self, upload_id: str, chunk_info: UploadChunk, save_path: str) -> tuple[str, str]:
        """
        合并本地文件系统的分片文件并返回文件路径和完整哈希值
        :param upload_id: 上传会话ID
        :param chunk_info: 分片信息
        :param save_path: 文件保存路径
        :return: (文件路径, 文件哈希值)
        """
        output_path = self.root_path / save_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_base_dir = output_path.parent / 'chunks' / upload_id
        file_sha256 = hashlib.sha256()
        
        # 使用临时文件写入，确保原子性
        temp_output = output_path.with_suffix('.merging')
        try:
            async with aiofiles.open(temp_output, "wb") as out_file:
                for i in range(chunk_info.total_chunks):
                    # 获取分片记录
                    chunk_record = await UploadChunk.filter(upload_id=upload_id, chunk_index=i).first()
                    if not chunk_record:
                        raise ValueError(f"分片{i}记录不存在")
                    chunk_path = chunk_base_dir / f"{i}.part"
                    if not chunk_path.exists():
                        raise ValueError(f"分片{i}文件不存在")
                    async with aiofiles.open(chunk_path, "rb") as in_file:
                        chunk_data = await in_file.read()
                        current_hash = hashlib.sha256(chunk_data).hexdigest()
                        if current_hash != chunk_record.chunk_hash:
                            raise ValueError(f"分片{i}哈希不匹配: 期望 {chunk_record.chunk_hash}, 实际 {current_hash}")
                        file_sha256.update(chunk_data)
                        await out_file.write(chunk_data)
            # 原子重命名
            temp_output.rename(output_path)
        except Exception as e:
            if temp_output.exists():
                temp_output.unlink()
            raise e
        return str(output_path), file_sha256.hexdigest()

    async def clean_chunks(self, upload_id: str, save_path: str):
        """
        清理本地文件系统的临时分片文件
        :param upload_id: 上传会话ID
        :param save_path: 文件保存路径
        """
        chunk_dir = (self.root_path / save_path).parent / 'chunks' / upload_id
        if chunk_dir.exists():
            # rmtree 是同步磁盘 I/O：分片多时（大文件切成上百片）会阻塞事件循环，
            # 期间所有请求都在排队，因此放进线程执行
            err = await asyncio.to_thread(_rmtree_quiet, chunk_dir)
            if err:
                logger.info(f"清理本地分片目录失败: {err}")
        # 清理父级 chunks 目录（如果为空）
        chunks_parent = chunk_dir.parent
        if chunks_parent.exists() and not any(chunks_parent.iterdir()):
            try:
                chunks_parent.rmdir()
            except Exception as e:
                logger.info(f"清理 chunks 父目录失败: {e}")

    async def file_exists(self, save_path: str) -> bool:
        """
        检查文件是否存在于本地文件系统
        :param save_path: 文件路径
        :return: 文件是否存在
        """
        file_path = self.root_path / save_path
        return file_path.exists()

    async def get_file_stream(self, save_path: str) -> AsyncGenerator[bytes, None]:
        """获取本地文件流"""
        file_path = self.root_path / save_path
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {save_path}")
        async with aiofiles.open(file_path, "rb") as f:
            while True:
                chunk = await f.read(256 * 1024)
                if not chunk:
                    break
                yield chunk

    async def delete_file_by_path(self, storage_path: str, uuid_file_name: str):
        """根据路径删除本地文件"""
        file_path = self.root_path / storage_path / uuid_file_name
        if file_path.exists():
            file_path.unlink()

    async def get_file_size(self, save_path: str) -> Optional[int]:
        """获取本地文件大小"""
        file_path = self.root_path / save_path
        try:
            return file_path.stat().st_size
        except Exception:
            return None


class S3FileStorage(FileStorageInterface):
    def __init__(self):
        self.access_key_id = settings.s3_access_key_id
        self.secret_access_key = settings.s3_secret_access_key
        self.bucket_name = settings.s3_bucket_name
        self.s3_hostname = settings.s3_hostname
        self.region_name = settings.s3_region_name
        self.signature_version = settings.s3_signature_version
        self.endpoint_url = settings.s3_endpoint_url or f"https://{self.s3_hostname}"
        self.aws_session_token = settings.aws_session_token
        self.proxy = settings.s3_proxy
        self.session = aioboto3.Session(
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
        )
        if not settings.s3_endpoint_url:
            self.endpoint_url = f"https://{self.s3_hostname}"
        else:
            # 如果提供了 s3_endpoint_url，则优先使用它
            self.endpoint_url = settings.s3_endpoint_url

    async def save_file(self, file: UploadFile, save_path: str):
        async with self.session.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_session_token=self.aws_session_token,
                region_name=self.region_name,
                config=Config(signature_version=self.signature_version),
        ) as s3:
            # 使用 upload_fileobj 流式上传，避免将整个文件加载到内存
            await s3.upload_fileobj(
                file.file,
                self.bucket_name,
                save_path,
                ExtraArgs={"ContentType": file.content_type or "application/octet-stream"},
            )


    async def delete_file(self, file_code: FileCodes):
        async with self.session.client(
                "s3",
                endpoint_url=self.endpoint_url,
                region_name=self.region_name,
                config=Config(signature_version=self.signature_version),
        ) as s3:
            await s3.delete_object(
                Bucket=self.bucket_name, Key=await file_code.get_file_path()
            )

    async def get_file_response(self, file_code: FileCodes):
        try:
            filename = file_code.prefix + file_code.suffix
            content_length = None  # 初始化为 None，表示未知大小
            
            async with self.session.client(
                    "s3",
                    endpoint_url=self.endpoint_url,
                    region_name=self.region_name,
                    config=Config(signature_version=self.signature_version),
            ) as s3:
                # 尝试获取文件大小（HEAD请求）
                try:
                    head_response = await s3.head_object(
                        Bucket=self.bucket_name,
                        Key=await file_code.get_file_path()
                    )
                    # 从HEAD响应中获取Content-Length
                    if 'ContentLength' in head_response:
                        content_length = head_response['ContentLength']
                    elif 'Content-Length' in head_response['ResponseMetadata']['HTTPHeaders']:
                        content_length = int(head_response['ResponseMetadata']['HTTPHeaders']['Content-Length'])
                except Exception:
                    # 如果HEAD请求失败，则不提供 Content-Length
                    pass
                
                link = await s3.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": self.bucket_name,
                        "Key": await file_code.get_file_path(),
                    },
                    ExpiresIn=3600,
                )
            
            # 创建流式生成器，确保session总是被关闭
            async def stream_generator():
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.get(link) as resp:
                            if resp.status != 200:
                                raise HTTPException(
                                    status_code=resp.status,
                                    detail="从S3获取文件失败"
                                )
                            chunk_size = 65536
                            while True:
                                chunk = await resp.content.read(chunk_size)
                                if not chunk:
                                    break
                                yield chunk
                    except HTTPException:
                        raise
                    except Exception:
                        raise HTTPException(status_code=503, detail="服务代理下载异常")
            
            from fastapi.responses import StreamingResponse
            encoded_filename = quote(filename, safe='')
            headers = {
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
            }
            if content_length is not None:
                headers["Content-Length"] = str(content_length)
            return StreamingResponse(
                stream_generator(),
                media_type="application/octet-stream",
                headers=headers
            )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=503, detail="服务代理下载异常，请稍后再试")

    async def get_file_url(self, file_code: FileCodes):
        # 文本分享已合并为文件分享（.txt 落盘），统一按文件生成 URL
        if self.proxy:
            return await get_file_url(file_code.code)
        else:
            async with self.session.client(
                    "s3",
                    endpoint_url=self.endpoint_url,
                    region_name=self.region_name,
                    config=Config(signature_version=self.signature_version),
            ) as s3:
                result = await s3.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": self.bucket_name,
                        "Key": await file_code.get_file_path(),
                    },
                    ExpiresIn=3600,
                )
                return result

    async def save_chunk(self, upload_id: str, chunk_index: int, chunk_data: bytes, chunk_hash: str, save_path: str):
        """
        保存分片到 S3（使用独立对象存储每个分片）
        注意：这里不使用 S3 原生的 multipart upload，而是将每个分片作为独立对象存储
        """
        chunk_key = str(Path(save_path).parent / "chunks" / upload_id / f"{chunk_index}.part")
        async with self.session.client(
            's3',
            endpoint_url=self.endpoint_url,
            aws_session_token=self.aws_session_token,
            region_name=self.region_name,
            config=Config(signature_version=self.signature_version),
        ) as s3:
            # 将分片作为独立对象上传
            await s3.put_object(
                Bucket=self.bucket_name,
                Key=chunk_key,
                Body=chunk_data,
                Metadata={
                    'chunk-hash': chunk_hash,
                    'chunk-index': str(chunk_index)
                }
            )

    async def merge_chunks(self, upload_id: str, chunk_info: UploadChunk, save_path: str) -> tuple[str, str]:
        """
        合并 S3 上的分片文件
        使用 S3 的 multipart upload API 实现流式合并，避免内存问题
        """
        file_sha256 = hashlib.sha256()
        chunk_dir = str(Path(save_path).parent / "chunks" / upload_id)

        async with self.session.client(
            's3',
            endpoint_url=self.endpoint_url,
            aws_session_token=self.aws_session_token,
            region_name=self.region_name,
            config=Config(signature_version=self.signature_version),
        ) as s3:
            # 创建 multipart upload
            mpu = await s3.create_multipart_upload(
                Bucket=self.bucket_name,
                Key=save_path,
                ContentType='application/octet-stream'
            )
            mpu_id = mpu['UploadId']
            parts = []

            try:
                # 按顺序读取、验证并上传每个分片
                for i in range(chunk_info.total_chunks):
                    chunk_key = f"{chunk_dir}/{i}.part"
                    chunk_record = await UploadChunk.filter(upload_id=upload_id, chunk_index=i).first()
                    if not chunk_record:
                        raise ValueError(f"分片{i}记录不存在")

                    try:
                        response = await s3.get_object(
                            Bucket=self.bucket_name,
                            Key=chunk_key
                        )
                        chunk_data = await response['Body'].read()
                    except Exception as e:
                        raise ValueError(f"分片{i}文件不存在: {e}")

                    current_hash = hashlib.sha256(chunk_data).hexdigest()
                    if current_hash != chunk_record.chunk_hash:
                        raise ValueError(f"分片{i}哈希不匹配: 期望 {chunk_record.chunk_hash}, 实际 {current_hash}")

                    file_sha256.update(chunk_data)

                    # 上传分片到 multipart upload
                    part_response = await s3.upload_part(
                        Bucket=self.bucket_name,
                        Key=save_path,
                        UploadId=mpu_id,
                        PartNumber=i + 1,  # S3 part numbers start at 1
                        Body=chunk_data
                    )
                    parts.append({
                        'PartNumber': i + 1,
                        'ETag': part_response['ETag']
                    })

                    # 释放内存
                    del chunk_data

                # 完成 multipart upload
                await s3.complete_multipart_upload(
                    Bucket=self.bucket_name,
                    Key=save_path,
                    UploadId=mpu_id,
                    MultipartUpload={'Parts': parts}
                )
            except Exception as e:
                # 出错时取消 multipart upload
                await s3.abort_multipart_upload(
                    Bucket=self.bucket_name,
                    Key=save_path,
                    UploadId=mpu_id
                )
                raise e

        return save_path, file_sha256.hexdigest()

    async def clean_chunks(self, upload_id: str, save_path: str):
        """
        清理 S3 上的临时分片文件
        :param upload_id: 上传会话ID
        :param save_path: 文件保存路径
        """
        chunk_dir = str(Path(save_path).parent / "chunks" / upload_id)
        async with self.session.client(
            's3',
            endpoint_url=self.endpoint_url,
            aws_session_token=self.aws_session_token,
            region_name=self.region_name,
            config=Config(signature_version=self.signature_version),
        ) as s3:
            try:
                # 列出并删除所有分片对象
                paginator = s3.get_paginator('list_objects_v2')
                async for page in paginator.paginate(Bucket=self.bucket_name, Prefix=chunk_dir):
                    objects = page.get('Contents', [])
                    if objects:
                        delete_objects = [{'Key': obj['Key']} for obj in objects]
                        await s3.delete_objects(
                            Bucket=self.bucket_name,
                            Delete={'Objects': delete_objects}
                        )
            except Exception as e:
                logger.info(f"清理 S3 分片数据时出错: {e}")

    async def generate_presigned_upload_url(self, save_path: str, expires_in: int = 900) -> Optional[str]:
        """
        生成S3预签名上传URL
        :param save_path: 文件保存路径
        :param expires_in: URL过期时间（秒），默认15分钟
        :return: 预签名PUT URL
        """
        async with self.session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_session_token=self.aws_session_token,
            region_name=self.region_name,
            config=Config(signature_version=self.signature_version),
        ) as s3:
            return await s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": save_path,
                },
                ExpiresIn=expires_in,
            )

    async def file_exists(self, save_path: str) -> bool:
        """
        检查文件是否存在于S3
        :param save_path: 文件路径
        :return: 文件是否存在
        """
        async with self.session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_session_token=self.aws_session_token,
            region_name=self.region_name,
            config=Config(signature_version=self.signature_version),
        ) as s3:
            try:
                await s3.head_object(Bucket=self.bucket_name, Key=save_path)
                return True
            except Exception:
                return False

    async def get_file_stream(self, save_path: str) -> AsyncGenerator[bytes, None]:
        """获取S3文件流"""
        async with self.session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_session_token=self.aws_session_token,
            region_name=self.region_name,
            config=Config(signature_version=self.signature_version),
        ) as s3:
            response = await s3.get_object(Bucket=self.bucket_name, Key=save_path)
            async with response["Body"] as stream:
                while True:
                    chunk = await stream.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk

    async def delete_file_by_path(self, storage_path: str, uuid_file_name: str):
        """根据路径删除S3文件"""
        key = f"{storage_path}/{uuid_file_name}"
        async with self.session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region_name,
            config=Config(signature_version=self.signature_version),
        ) as s3:
            await s3.delete_object(Bucket=self.bucket_name, Key=key)

    async def get_file_size(self, save_path: str) -> Optional[int]:
        """获取S3文件大小"""
        try:
            async with self.session.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_session_token=self.aws_session_token,
                region_name=self.region_name,
                config=Config(signature_version=self.signature_version),
            ) as s3:
                head = await s3.head_object(Bucket=self.bucket_name, Key=save_path)
                return head.get('ContentLength')
        except Exception:
            return None


class OneDriveFileStorage(FileStorageInterface):
    def __init__(self):
        try:
            import msal
            from office365.graph_client import GraphClient
            from office365.runtime.client_request_exception import (
                ClientRequestException,
            )
        except ImportError:
            raise ImportError("请先安装`msal`和`Office365-REST-Python-Client`")
        self.msal = msal
        self.domain = settings.onedrive_domain
        self.client_id = settings.onedrive_client_id
        self.username = settings.onedrive_username
        self.password = settings.onedrive_password
        self.proxy = settings.onedrive_proxy
        self._ClientRequestException = ClientRequestException

        try:
            client = GraphClient(self.acquire_token_pwd)
            self.root_path = (
                client.me.drive.root.get_by_path(settings.onedrive_root_path)
                .get()
                .execute_query()
            )
        except ClientRequestException as e:
            if e.code == "itemNotFound":
                client.me.drive.root.create_folder(settings.onedrive_root_path)
                self.root_path = (
                    client.me.drive.root.get_by_path(
                        settings.onedrive_root_path)
                    .get()
                    .execute_query()
                )
            else:
                raise e
        except Exception as e:
            raise Exception("OneDrive验证失败，请检查配置是否正确\n" + str(e))

    def acquire_token_pwd(self):
        authority_url = f"https://login.microsoftonline.com/{self.domain}"
        app = self.msal.PublicClientApplication(
            authority=authority_url, client_id=self.client_id
        )
        result = app.acquire_token_by_username_password(
            username=self.username,
            password=self.password,
            scopes=["https://graph.microsoft.com/.default"],
        )
        return result

    def _get_path_str(self, path):
        if isinstance(path, str):
            path = path.replace("\\", "/").replace("//", "/").split("/")
        elif isinstance(path, Path):
            path = str(path).replace("\\", "/").replace("//", "/").split("/")
        else:
            raise TypeError("path must be str or Path")
        path[-1] = path[-1].split(".")[0]
        return "/".join(path)

    def _save(self, file, save_path):
        content = file.file.read()
        # save_path 是字符串路径，提取文件名
        name = os.path.basename(save_path)
        path = self._get_path_str(save_path)
        self.root_path.get_by_path(path).upload(name, content).execute_query()

    async def save_file(self, file: UploadFile, save_path: str):
        await asyncio.to_thread(self._save, file, save_path)

    def _delete(self, save_path):
        path = self._get_path_str(save_path)
        try:
            self.root_path.get_by_path(path).delete_object().execute_query()
        except self._ClientRequestException as e:
            if e.code == "itemNotFound":
                pass
            else:
                raise e


    async def delete_file(self, file_code: FileCodes):
        await asyncio.to_thread(self._delete, await file_code.get_file_path())

    def _convert_link_to_download_link(self, link):
        m1 = re.search(r"https://(.+)\.sharepoint\.com", link)
        m2 = re.search(r"personal/(.+)/", link)
        if not m1 or not m2:
            raise ValueError(f"无法解析 OneDrive 分享链接: {link[:100]}")
        p1 = m1.group(1)
        p2 = m2.group(1)
        m3 = re.search(rf"{p2}/(.+)", link)
        if not m3:
            raise ValueError(f"无法解析 OneDrive 分享链接: {link[:100]}")
        p3 = m3.group(1)
        return f"https://{p1}.sharepoint.com/personal/{p2}/_layouts/52/download.aspx?share={p3}"

    def _get_file_url(self, save_path, name):
        path = self._get_path_str(save_path)
        remote_file = self.root_path.get_by_path(path + "/" + name)
        expiration_datetime = datetime.datetime.now(
            tz=datetime.timezone.utc
        ) + datetime.timedelta(hours=1)
        expiration_datetime = expiration_datetime.strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        permission = remote_file.create_link(
            "view", "anonymous", expiration_datetime=expiration_datetime
        ).execute_query()
        return self._convert_link_to_download_link(permission.link.webUrl)

    async def get_file_response(self, file_code: FileCodes):
        try:
            filename = file_code.prefix + file_code.suffix
            link = await asyncio.to_thread(
                self._get_file_url, await file_code.get_file_path(), filename
            )
            
            content_length = None  # 初始化为 None，表示未知大小
            
            async def stream_generator():
                async with aiohttp.ClientSession() as session:
                    try:
                        # 尝试发送HEAD请求获取Content-Length
                        try:
                            async with session.head(link) as resp:
                                if resp.status == 200 and 'Content-Length' in resp.headers:
                                    nonlocal content_length
                                    content_length = int(resp.headers['Content-Length'])
                        except Exception:
                            pass
                        
                        async with session.get(link) as resp:
                            if resp.status != 200:
                                raise HTTPException(
                                    status_code=resp.status,
                                    detail="从OneDrive获取文件失败"
                                )
                            chunk_size = 65536
                            while True:
                                chunk = await resp.content.read(chunk_size)
                                if not chunk:
                                    break
                                yield chunk
                    except HTTPException:
                        raise
                    except Exception:
                        raise HTTPException(status_code=503, detail="服务代理下载异常")
            
            encoded_filename = quote(filename, safe='')
            headers = {
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
            }
            if content_length is not None:
                headers["Content-Length"] = str(content_length)
            return StreamingResponse(
                stream_generator(),
                media_type="application/octet-stream",
                headers=headers
            )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=503, detail="服务代理下载异常，请稍后再试")

    async def get_file_url(self, file_code: FileCodes):
        if self.proxy:
            return await get_file_url(file_code.code)
        else:
            return await asyncio.to_thread(
                self._get_file_url,
                await file_code.get_file_path(),
                f"{file_code.prefix}{file_code.suffix}",
            )

    def _save_chunk(self, chunk_path: str, chunk_data: bytes):
        """同步保存分片到 OneDrive"""
        path_parts = chunk_path.replace("\\", "/").split("/")
        filename = path_parts[-1]
        dir_path = "/".join(path_parts[:-1])
        
        # 确保目录存在
        current_folder = self.root_path
        for part in dir_path.split("/"):
            if part:
                try:
                    current_folder = current_folder.get_by_path(part).get().execute_query()
                except self._ClientRequestException as e:
                    if e.code == "itemNotFound":
                        current_folder = current_folder.create_folder(part).execute_query()
                    else:
                        raise e
        
        # 上传分片
        current_folder.upload(filename, chunk_data).execute_query()

    async def save_chunk(self, upload_id: str, chunk_index: int, chunk_data: bytes, chunk_hash: str, save_path: str):
        """保存分片到 OneDrive"""
        chunk_path = str(Path(save_path).parent / "chunks" / upload_id / f"{chunk_index}.part")
        await asyncio.to_thread(self._save_chunk, chunk_path, chunk_data)

    def _read_chunk(self, chunk_path: str) -> bytes:
        """同步读取分片"""
        path = self._get_path_str(chunk_path)
        file_obj = self.root_path.get_by_path(path).get().execute_query()
        return file_obj.get_content().execute_query().value

    def _upload_merged(self, save_path: str, data: bytes):
        """同步上传合并后的文件"""
        path_parts = save_path.replace("\\", "/").split("/")
        filename = path_parts[-1]
        dir_path = "/".join(path_parts[:-1])
        
        # 确保目录存在
        current_folder = self.root_path
        for part in dir_path.split("/"):
            if part:
                try:
                    current_folder = current_folder.get_by_path(part).get().execute_query()
                except self._ClientRequestException as e:
                    if e.code == "itemNotFound":
                        current_folder = current_folder.create_folder(part).execute_query()
                    else:
                        raise e
        
        current_folder.upload(filename, data).execute_query()

    async def merge_chunks(self, upload_id: str, chunk_info: UploadChunk, save_path: str) -> tuple[str, str]:
        """合并 OneDrive 上的分片文件，使用临时文件避免内存问题"""
        file_sha256 = hashlib.sha256()
        chunk_dir = str(Path(save_path).parent / "chunks" / upload_id)

        # 使用临时文件存储合并数据
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name

        try:
            async with aiofiles.open(temp_path, 'wb') as out_file:
                for i in range(chunk_info.total_chunks):
                    chunk_path = f"{chunk_dir}/{i}.part"
                    chunk_record = await UploadChunk.filter(upload_id=upload_id, chunk_index=i).first()
                    if not chunk_record:
                        raise ValueError(f"分片{i}记录不存在")

                    try:
                        chunk_data = await asyncio.to_thread(self._read_chunk, chunk_path)
                    except Exception as e:
                        raise ValueError(f"分片{i}文件不存在: {e}")

                    current_hash = hashlib.sha256(chunk_data).hexdigest()
                    if current_hash != chunk_record.chunk_hash:
                        raise ValueError(f"分片{i}哈希不匹配: 期望 {chunk_record.chunk_hash}, 实际 {current_hash}")

                    file_sha256.update(chunk_data)
                    await out_file.write(chunk_data)
                    del chunk_data  # 释放内存

            # 读取临时文件并上传
            async with aiofiles.open(temp_path, 'rb') as f:
                merged_content = await f.read()
            await asyncio.to_thread(self._upload_merged, save_path, merged_content)
        finally:
            # 清理临时文件
            if os.path.exists(temp_path):
                os.unlink(temp_path)

        return save_path, file_sha256.hexdigest()

    def _delete_chunk_dir(self, chunk_dir: str):
        """同步删除分片目录"""
        try:
            path = self._get_path_str(chunk_dir)
            self.root_path.get_by_path(path).delete_object().execute_query()
        except self._ClientRequestException as e:
            if e.code != "itemNotFound":
                raise e

    async def clean_chunks(self, upload_id: str, save_path: str):
        """清理 OneDrive 上的临时分片文件"""
        chunk_dir = str(Path(save_path).parent / "chunks" / upload_id)
        try:
            await asyncio.to_thread(self._delete_chunk_dir, chunk_dir)
        except Exception as e:
            logger.info(f"清理 OneDrive 分片时出错: {e}")

    def _file_exists(self, save_path: str) -> bool:
        """同步检查文件是否存在"""
        try:
            path = self._get_path_str(save_path)
            self.root_path.get_by_path(path).get().execute_query()
            return True
        except self._ClientRequestException as e:
            if e.code == "itemNotFound":
                return False
            raise e

    async def file_exists(self, save_path: str) -> bool:
        """
        检查文件是否存在于OneDrive
        :param save_path: 文件路径
        :return: 文件是否存在
        """
        return await asyncio.to_thread(self._file_exists, save_path)


class OpenDALFileStorage(FileStorageInterface):
    def __init__(self):
        try:
            import opendal
        except ImportError:
            raise ImportError('请先安装 `opendal`, 例如: "pip install opendal"')
        self.service = settings.opendal_scheme
        service_settings = {}
        for key, value in settings.items():
            if key.startswith("opendal_" + self.service):
                setting_name = key.split("_", 2)[2]
                service_settings[setting_name] = value
        self.operator = opendal.AsyncOperator(
            settings.opendal_scheme, **service_settings
        )

    async def save_file(self, file: UploadFile, save_path: str):
        # 使用流式写入，避免将整个文件读入内存
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
        try:
            async with aiofiles.open(tmp_path, "wb") as f:
                while True:
                    chunk = await asyncio.to_thread(file.file.read, 256 * 1024)
                    if not chunk:
                        break
                    await f.write(chunk)
            # 读取临时文件并写入存储
            async with aiofiles.open(tmp_path, "rb") as f:
                content = await f.read()
            await self.operator.write(save_path, content)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


    async def delete_file(self, file_code: FileCodes):
        await self.operator.delete(await file_code.get_file_path())

    async def get_file_url(self, file_code: FileCodes):
        return await get_file_url(file_code.code)

    async def get_file_response(self, file_code: FileCodes):
        try:
            filename = file_code.prefix + file_code.suffix
            content_length = None  # 初始化为 None，表示未知大小
            
            # 尝试获取文件大小
            try:
                stat_result = await self.operator.stat(await file_code.get_file_path())
                if hasattr(stat_result, 'content_length') and stat_result.content_length:
                    content_length = stat_result.content_length
                elif hasattr(stat_result, 'size') and stat_result.size:
                    content_length = stat_result.size
            except Exception:
                # 如果获取大小失败，则不提供 Content-Length
                pass
            
            # 尝试使用流式读取器
            try:
                # OpenDAL 可能提供 reader 方法返回一个异步读取器
                reader = await self.operator.reader(await file_code.get_file_path())
            except AttributeError:
                # 如果 reader 方法不存在，回退到全量读取（兼容旧版本）
                content = await self.operator.read(await file_code.get_file_path())
                encoded_filename = quote(filename, safe='')
                headers = {
                    "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
                }
                if content_length is not None:
                    headers["Content-Length"] = str(content_length)
                return Response(
                    content, headers=headers, media_type="application/octet-stream"
                )
            
            async def stream_generator():
                chunk_size = 65536
                while True:
                    chunk = await reader.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            
            encoded_filename = quote(filename, safe='')
            headers = {
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
            }
            if content_length is not None:
                headers["Content-Length"] = str(content_length)
            return StreamingResponse(
                stream_generator(),
                media_type="application/octet-stream",
                headers=headers
            )
        except Exception as e:
            logger.info(e)
            raise HTTPException(status_code=404, detail="文件已过期删除")

    async def save_chunk(self, upload_id: str, chunk_index: int, chunk_data: bytes, chunk_hash: str, save_path: str):
        """保存分片到 OpenDAL 存储"""
        chunk_path = str(Path(save_path).parent / "chunks" / upload_id / f"{chunk_index}.part")
        await self.operator.write(chunk_path, chunk_data)

    async def merge_chunks(self, upload_id: str, chunk_info: UploadChunk, save_path: str) -> tuple[str, str]:
        """合并 OpenDAL 存储上的分片文件，使用临时文件避免内存问题"""
        file_sha256 = hashlib.sha256()
        chunk_dir = str(Path(save_path).parent / "chunks" / upload_id)

        # 使用临时文件存储合并数据
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name

        try:
            async with aiofiles.open(temp_path, 'wb') as out_file:
                for i in range(chunk_info.total_chunks):
                    chunk_path = f"{chunk_dir}/{i}.part"
                    chunk_record = await UploadChunk.filter(upload_id=upload_id, chunk_index=i).first()
                    if not chunk_record:
                        raise ValueError(f"分片{i}记录不存在")

                    try:
                        chunk_data = await self.operator.read(chunk_path)
                    except Exception as e:
                        raise ValueError(f"分片{i}文件不存在: {e}")

                    current_hash = hashlib.sha256(chunk_data).hexdigest()
                    if current_hash != chunk_record.chunk_hash:
                        raise ValueError(f"分片{i}哈希不匹配: 期望 {chunk_record.chunk_hash}, 实际 {current_hash}")

                    file_sha256.update(chunk_data)
                    await out_file.write(chunk_data)
                    del chunk_data  # 释放内存

            # 读取临时文件并写入存储
            async with aiofiles.open(temp_path, 'rb') as f:
                merged_content = await f.read()
            await self.operator.write(save_path, merged_content)
        finally:
            # 清理临时文件
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        
        return save_path, file_sha256.hexdigest()

    async def clean_chunks(self, upload_id: str, save_path: str):
        """清理 OpenDAL 存储上的临时分片文件"""
        chunk_dir = str(Path(save_path).parent / "chunks" / upload_id)
        try:
            # OpenDAL 支持递归删除
            await self.operator.remove_all(chunk_dir)
        except Exception as e:
            logger.info(f"清理 OpenDAL 分片时出错: {e}")

    async def file_exists(self, save_path: str) -> bool:
        """
        检查文件是否存在于OpenDAL存储
        :param save_path: 文件路径
        :return: 文件是否存在
        """
        try:
            await self.operator.stat(save_path)
            return True
        except Exception:
            return False


class WebDAVFileStorage(FileStorageInterface):
    _instance: Optional["WebDAVFileStorage"] = None

    def __init__(self):
        if not hasattr(self, "_initialized"):
            self.base_url = settings.webdav_url.rstrip("/") + "/"
            self.auth = aiohttp.BasicAuth(
                login=settings.webdav_username, password=settings.webdav_password
            )
            self._initialized = True

    def _build_url(self, path: str) -> str:
        encoded_path = quote(str(path.replace("\\", "/").lstrip("/")).lstrip("/"))
        return f"{self.base_url}{encoded_path}"

    async def _mkdir_p(self, directory_path: str):
        """递归创建目录（类似mkdir -p）"""
        path_obj = Path(unquote(directory_path))
        current_path = ""

        async with aiohttp.ClientSession(auth=self.auth) as session:
            # 逐级检查目录是否存在
            for part in path_obj.parts:
                current_path = str(Path(current_path) / part)
                url = self._build_url(current_path)

                # 检查目录是否存在
                async with session.head(url) as resp:
                    if resp.status == 404:
                        # 创建目录
                        async with session.request("MKCOL", url) as mkcol_resp:
                            if mkcol_resp.status not in (200, 201, 409):
                                raise HTTPException(
                                    status_code=503,
                                    detail="WebDAV目录创建失败，请检查存储配置",
                                )

    async def _is_dir_empty(self, dir_path: str) -> bool:
        """检查目录是否为空"""
        url = self._build_url(dir_path)

        async with aiohttp.ClientSession(auth=self.auth) as session:
            async with session.request("PROPFIND", url, headers={"Depth": "1"}) as resp:
                if resp.status != 207:  # 207 是 Multi-Status 响应
                    return False
                content = await resp.text()
                # 如果只有一个 response（当前目录），说明目录为空
                return content.count("<D:response>") <= 1

    async def _delete_empty_dirs(self, file_path: str, session: aiohttp.ClientSession):
        """递归删除空目录"""
        path_obj = Path(file_path)
        current_path = path_obj.parent

        while str(current_path) != ".":
            if not await self._is_dir_empty(str(current_path)):
                break

            url = self._build_url(str(current_path))
            async with session.delete(url) as resp:
                if resp.status not in (200, 204, 404):
                    break

            current_path = current_path.parent

    async def save_file(self, file: UploadFile, save_path: str):
        """保存文件（自动创建目录，流式上传）"""
        path_obj = Path(save_path)
        directory_path = str(path_obj.parent)
        # 提取原始文件名并进行清理
        filename = await sanitize_filename(path_obj.name)
        # 构建安全的保存路径
        safe_save_path = str(Path(directory_path) / filename)

        try:
            # 先创建目录结构
            await self._mkdir_p(directory_path)
            # 上传文件（流式）
            url = self._build_url(safe_save_path)

            async def file_sender():
                """流式读取文件内容"""
                chunk_size = 256 * 1024  # 256KB chunks
                while True:
                    chunk = await asyncio.to_thread(file.file.read, chunk_size)
                    if not chunk:
                        break
                    yield chunk

            async with aiohttp.ClientSession(auth=self.auth) as session:
                async with session.put(
                        url,
                        data=file_sender(),
                        headers={"Content-Type": file.content_type or "application/octet-stream"}
                ) as resp:
                    if resp.status not in (200, 201, 204):
                        raise HTTPException(
                            status_code=503,
                            detail="WebDAV文件上传失败，请检查存储配置",
                        )
        except aiohttp.ClientError:
            # 连接类错误不向客户端回显远程细节（避免泄露存储地址/凭据信息）
            raise HTTPException(status_code=503, detail="WebDAV连接异常，请稍后再试")


    async def delete_file(self, file_code: FileCodes):
        """删除WebDAV文件及空目录"""
        file_path = await file_code.get_file_path()
        url = self._build_url(file_path)
        try:
            async with aiohttp.ClientSession(auth=self.auth) as session:
                # 删除文件
                async with session.delete(url) as resp:
                    if resp.status not in (200, 204, 404):
                        raise HTTPException(
                            status_code=503,
                            detail="WebDAV删除失败，请检查存储配置",
                        )

                # 使用同一个 session 删除空目录
                await self._delete_empty_dirs(file_path, session)

        except aiohttp.ClientError:
            raise HTTPException(status_code=503, detail="WebDAV连接异常，请稍后再试")

    async def get_file_url(self, file_code: FileCodes):
        return await get_file_url(file_code.code)

    async def get_file_response(self, file_code: FileCodes):
        """获取文件响应（代理模式）"""
        try:
            filename = file_code.prefix + file_code.suffix
            url = self._build_url(await file_code.get_file_path())
            content_length = None  # 初始化为 None，表示未知大小
            
            async def stream_generator():
                nonlocal content_length
                async with aiohttp.ClientSession(auth=self.auth) as session:
                    try:
                        # 尝试发送HEAD请求获取Content-Length
                        try:
                            async with session.head(url) as resp:
                                if resp.status == 200 and 'Content-Length' in resp.headers:
                                    content_length = int(resp.headers['Content-Length'])
                        except Exception:
                            pass
                        
                        async with session.get(url) as resp:
                            if resp.status != 200:
                                raise HTTPException(
                                    status_code=resp.status,
                                    detail="文件获取失败"
                                )
                            chunk_size = 65536
                            while True:
                                chunk = await resp.content.read(chunk_size)
                                if not chunk:
                                    break
                                yield chunk
                    except HTTPException:
                        raise
                    except Exception:
                        raise HTTPException(status_code=503, detail="服务代理下载异常")
            
            encoded_filename = quote(filename, safe='')
            headers = {
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
            }
            if content_length is not None:
                headers["Content-Length"] = str(content_length)
            return StreamingResponse(
                stream_generator(),
                media_type="application/octet-stream",
                headers=headers
            )
        except aiohttp.ClientError:
            raise HTTPException(
                status_code=503, detail="WebDAV连接异常")
        except HTTPException:
            raise

    async def save_chunk(self, upload_id: str, chunk_index: int, chunk_data: bytes, chunk_hash: str, save_path: str):
        """保存分片到 WebDAV"""
        chunk_dir = str(Path(save_path).parent / "chunks" / upload_id)
        chunk_path = f"{chunk_dir}/{chunk_index}.part"
        
        # 先创建目录结构
        await self._mkdir_p(chunk_dir)
        
        chunk_url = self._build_url(chunk_path)
        async with aiohttp.ClientSession(auth=self.auth) as session:
            async with session.put(chunk_url, data=chunk_data) as resp:
                if resp.status not in (200, 201, 204):
                    raise HTTPException(
                        status_code=503,
                        detail="WebDAV分片上传失败，请检查存储配置"
                    )

    async def merge_chunks(self, upload_id: str, chunk_info: UploadChunk, save_path: str) -> tuple[str, str]:
        """
        合并 WebDAV 上的分片文件
        使用临时文件避免内存问题
        """
        file_sha256 = hashlib.sha256()
        chunk_dir = str(Path(save_path).parent / "chunks" / upload_id)

        # 使用临时文件存储合并数据，避免内存问题
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name

        try:
            async with aiohttp.ClientSession(auth=self.auth) as session:
                # 按顺序读取并验证每个分片，写入临时文件
                async with aiofiles.open(temp_path, 'wb') as out_file:
                    for i in range(chunk_info.total_chunks):
                        chunk_path = f"{chunk_dir}/{i}.part"
                        chunk_url = self._build_url(chunk_path)

                        # 获取分片记录
                        chunk_record = await UploadChunk.filter(upload_id=upload_id, chunk_index=i).first()
                        if not chunk_record:
                            raise ValueError(f"分片{i}记录不存在")

                        # 下载分片数据
                        async with session.get(chunk_url) as resp:
                            if resp.status != 200:
                                raise ValueError(f"分片{i}文件不存在或无法访问")
                            chunk_data = await resp.read()

                        # 验证哈希
                        current_hash = hashlib.sha256(chunk_data).hexdigest()
                        if current_hash != chunk_record.chunk_hash:
                            raise ValueError(f"分片{i}哈希不匹配: 期望 {chunk_record.chunk_hash}, 实际 {current_hash}")

                        file_sha256.update(chunk_data)
                        await out_file.write(chunk_data)
                        del chunk_data  # 释放内存

                # 确保目标目录存在
                output_dir = str(Path(save_path).parent)
                await self._mkdir_p(output_dir)

                # 流式上传合并后的文件
                output_url = self._build_url(save_path)

                async def file_sender():
                    async with aiofiles.open(temp_path, 'rb') as f:
                        while True:
                            chunk = await f.read(256 * 1024)
                            if not chunk:
                                break
                            yield chunk

                async with session.put(output_url, data=file_sender()) as resp:
                    if resp.status not in (200, 201, 204):
                        raise HTTPException(
                            status_code=503,
                            detail="WebDAV合并文件上传失败，请检查存储配置"
                        )
        finally:
            # 清理临时文件
            if os.path.exists(temp_path):
                os.unlink(temp_path)

        return save_path, file_sha256.hexdigest()

    async def clean_chunks(self, upload_id: str, save_path: str):
        """
        清理 WebDAV 上的临时分片文件
        :param upload_id: 上传会话ID
        :param save_path: 文件保存路径
        """
        chunk_dir = str(Path(save_path).parent / "chunks" / upload_id)
        chunk_dir_url = self._build_url(chunk_dir)
        async with aiohttp.ClientSession(auth=self.auth) as session:
            try:
                # 检查分片目录是否存在
                async with session.request("PROPFIND", chunk_dir_url, headers={"Depth": "1"}) as resp:
                    if resp.status == 207:  # 207 表示 Multi-Status
                        # 获取目录下的所有分片文件
                        xml_data = await resp.text()
                        file_paths = re.findall(
                            r'<D:href>(.*?)</D:href>', xml_data)
                        for file_path in file_paths:
                            if file_path.endswith(".part"):
                                # 删除分片文件
                                file_url = self._build_url(file_path)
                                async with session.delete(file_url) as delete_resp:
                                    if delete_resp.status not in (200, 204, 404):
                                        logger.info(f"删除分片文件失败: {file_path}")

                        # 删除分片目录
                        async with session.delete(chunk_dir_url) as delete_resp:
                            if delete_resp.status not in (200, 204, 404):
                                logger.info(f"删除分片目录失败: {chunk_dir_url}")
                    else:
                        logger.info(f"分片目录不存在: {chunk_dir_url}")
            except Exception as e:
                logger.info(f"清理 WebDAV 分片时出错: {e}")

    async def file_exists(self, save_path: str) -> bool:
        """
        检查文件是否存在于WebDAV
        :param save_path: 文件路径
        :return: 文件是否存在
        """
        url = self._build_url(save_path)
        async with aiohttp.ClientSession(auth=self.auth) as session:
            async with session.head(url) as resp:
                return resp.status == 200

    async def get_file_stream(self, save_path: str) -> AsyncGenerator[bytes, None]:
        """获取WebDAV文件流"""
        url = self._build_url(save_path)
        async with aiohttp.ClientSession(auth=self.auth) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise FileNotFoundError(f"文件获取失败: {save_path}")
                while True:
                    chunk = await resp.content.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk

    async def delete_file_by_path(self, storage_path: str, uuid_file_name: str):
        """根据路径删除WebDAV文件"""
        key = f"{storage_path}/{uuid_file_name}"
        url = self._build_url(key)
        async with aiohttp.ClientSession(auth=self.auth) as session:
            async with session.delete(url) as resp:
                if resp.status not in (200, 204, 404):
                    logger.info(f"WebDAV文件删除失败: {key}")


storages = {
    "local": SystemFileStorage,
    "s3": S3FileStorage,
    "onedrive": OneDriveFileStorage,
    "opendal": OpenDALFileStorage,
    "webdav": WebDAVFileStorage,
}


async def create_zip_stream(file_items: list) -> AsyncGenerator[bytes, None]:
    """
    流式ZIP打包生成器，使用临时文件避免内存问题
    :param file_items: FileItem 或 FileCodes 对象列表
    :return: 异步字节流生成器
    """
    file_storage: FileStorageInterface = storages[settings.file_storage]()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp_path = tmp.name

    try:
        # 使用临时目录存放文件，然后流式写入ZIP
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            used_names: set[str] = set()
            for item in file_items:
                if hasattr(item, 'file_name') and hasattr(item, 'storage_path'):
                    base_name = item.file_name
                    full_path = f"{item.storage_path}/{item.uuid_file_name}"
                elif hasattr(item, 'prefix') and hasattr(item, 'file_path'):
                    base_name = f"{item.prefix}{item.suffix}"
                    full_path = f"{item.file_path}/{item.uuid_file_name}"
                else:
                    continue

                name = base_name
                # 二次消毒，防止 ZIP 条目包含路径穿越（Zip Slip）
                name = await sanitize_filename(name)
                counter = 1
                while name in used_names:
                    stem, ext = os.path.splitext(base_name)
                    name = f"{await sanitize_filename(stem)}_{counter}{ext}"
                    counter += 1
                used_names.add(name)

                # 使用临时文件避免将整个文件读入内存
                with tempfile.NamedTemporaryFile(delete=False) as item_tmp:
                    item_tmp_path = item_tmp.name
                try:
                    async with aiofiles.open(item_tmp_path, "wb") as f:
                        async for chunk in file_storage.get_file_stream(full_path):
                            await f.write(chunk)
                    zf.write(item_tmp_path, name)
                finally:
                    if os.path.exists(item_tmp_path):
                        os.unlink(item_tmp_path)

        async with aiofiles.open(tmp_path, "rb") as f:
            while True:
                chunk = await f.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
