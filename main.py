# @Time    : 2023/8/9 23:23
# @Author  : Lan
# @File    : main.py
# @Software: PyCharm
import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from tortoise import Tortoise
from tortoise.contrib.fastapi import register_tortoise

from apps.admin.views import admin_api
from apps.base.models import KeyValue
from apps.base.utils import ip_limit
from apps.base.views import share_api, chunk_api, presign_api, collection_api, delivery_api, ws_api
from core.config import ensure_settings_row, refresh_settings
from core.database import db_startup_lock, get_db_config, init_db
from core.logger import logger
from core.response import APIResponse
from core.settings import settings, BASE_DIR, DEFAULT_CONFIG
from core.tasks import delete_expire_files, clean_incomplete_uploads, clean_expired_collections
from core.utils import hash_password, is_password_hashed


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("正在初始化应用...")
    # 初始化数据库
    await init_db()

    # 加载配置（多进程下串行化启动写操作）
    async with db_startup_lock():
        await load_config()
    app.mount(
        "/assets",
        StaticFiles(directory=f"./{settings.themesSelect}/assets"),
        name="assets",
    )

    # 启动后台任务
    task = asyncio.create_task(delete_expire_files())
    chunk_cleanup_task = asyncio.create_task(clean_incomplete_uploads())
    collection_cleanup_task = asyncio.create_task(clean_expired_collections())
    logger.info("应用初始化完成")

    try:
        yield
    finally:
        # 清理操作
        logger.info("正在关闭应用...")
        task.cancel()
        chunk_cleanup_task.cancel()
        collection_cleanup_task.cancel()
        await asyncio.gather(task, chunk_cleanup_task, collection_cleanup_task, return_exceptions=True)
        await Tortoise.close_connections()
        logger.info("应用已关闭")


async def load_config():
    await ensure_settings_row()
    await KeyValue.update_or_create(
        key="sys_start", defaults={"value": int(time.time() * 1000)}
    )
    await refresh_settings()

    await migrate_password_to_hash()

    ip_limit["error"].minutes = settings.errorMinute
    ip_limit["error"].count = settings.errorCount
    ip_limit["upload"].minutes = settings.uploadMinute
    ip_limit["upload"].count = settings.uploadCount


async def migrate_password_to_hash():
    # 如果是明文密码，迁移为 pbkdf2 哈希
    if not is_password_hashed(settings.admin_token):
        hashed = hash_password(settings.admin_token)
        settings.admin_token = hashed
        config_record = await KeyValue.filter(key="settings").first()
        if config_record and config_record.value:
            config_record.value["admin_token"] = hashed
            await config_record.save()
            logger.info("已将管理员密码迁移为哈希存储")
    # 如果是旧的 sha256 哈希，升级为 pbkdf2（需要用户下次登录时触发，此处仅标记）
    elif settings.admin_token.startswith("sha256$"):
        logger.info("检测到旧版SHA256哈希密码，建议修改密码以升级安全性")


app = FastAPI(lifespan=lifespan)

# 配置缓存
_config_cache = {"last_refresh": 0}
_CONFIG_CACHE_TTL = 5  # 5秒缓存

@app.middleware("http")
async def refresh_settings_middleware(request, call_next):
    # 每5秒刷新一次配置，而非每次请求都刷新
    now = time.time()
    if now - _config_cache["last_refresh"] >= _CONFIG_CACHE_TTL:
        await refresh_settings()
        _config_cache["last_refresh"] = now
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# 使用 register_tortoise 来添加异常处理器
register_tortoise(
    app,
    config=get_db_config(),
    generate_schemas=False,
    add_exception_handlers=True,
)

app.include_router(share_api)
app.include_router(chunk_api)
app.include_router(presign_api)
app.include_router(admin_api)
app.include_router(collection_api)
app.include_router(delivery_api)
app.include_router(ws_api)


@app.exception_handler(404)
@app.get("/")
async def index(request=None, exc=None):
    return HTMLResponse(
        content=open(
            BASE_DIR / f"{settings.themesSelect}/index.html", "r", encoding="utf-8"
        )
        .read()
        .replace("{{title}}", str(settings.name))
        .replace("{{description}}", str(settings.description))
        .replace("{{keywords}}", str(settings.keywords))
        .replace("{{opacity}}", str(settings.opacity))
        .replace('"/assets/', '"assets/')
        .replace("{{background}}", str(settings.background)),
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/robots.txt")
async def robots():
    return HTMLResponse(content=settings.robotsText, media_type="text/plain")


@app.post("/")
async def get_config():
    return APIResponse(
        detail={
            "name": settings.name,
            "description": settings.description,
            "explain": settings.page_explain,
            "uploadSize": settings.uploadSize,
            "expireStyle": settings.expireStyle,
            "enableChunk": settings.enableChunk,
            "openUpload": settings.openUpload,
            "notify_title": settings.notify_title,
            "notify_content": settings.notify_content,
            "show_admin_address": settings.showAdminAddr,
            "max_save_seconds": settings.max_save_seconds,
            "maxMultiFileCount": getattr(settings, "maxMultiFileCount", 20),
            "maxCollectionFiles": getattr(settings, "maxCollectionFiles", 100),
            "maxSendFiles": getattr(settings, "maxSendFiles", 20),
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app="main:app",
        host=settings.serverHost,
        port=settings.serverPort,
        reload=False,
        workers=settings.serverWorkers,
    )
