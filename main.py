# @Time    : 2023/8/9 23:23
# @Author  : Lan
# @File    : main.py
# @Software: PyCharm
import asyncio
import html
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.responses import Response
from tortoise import Tortoise
from tortoise.contrib.fastapi import register_tortoise

from apps.admin.views import admin_api
from apps.base.models import KeyValue
from apps.base.utils import ip_limit
from apps.base.views import share_api, chunk_api, presign_api, collection_api, delivery_api, ws_api, room_api, compat_room_api
from core.config import ensure_settings_row, refresh_settings
from core.database import db_startup_lock, get_db_config, init_db
from core.logger import logger
from core.response import APIResponse
from core.settings import settings, BASE_DIR, DEFAULT_CONFIG
from core.tasks import delete_expire_files, clean_incomplete_uploads, clean_expired_collections, clean_expired_rooms, sweep_direct_connections
from core.utils import hash_password, is_password_hashed, verify_password


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
    room_cleanup_task = asyncio.create_task(clean_expired_rooms())
    sweep_task = asyncio.create_task(sweep_direct_connections())
    logger.info("应用初始化完成")

    try:
        yield
    finally:
        # 清理操作
        logger.info("正在关闭应用...")
        task.cancel()
        chunk_cleanup_task.cancel()
        collection_cleanup_task.cancel()
        room_cleanup_task.cancel()
        sweep_task.cancel()
        await asyncio.gather(task, chunk_cleanup_task, collection_cleanup_task, room_cleanup_task, sweep_task, return_exceptions=True)
        await Tortoise.close_connections()
        logger.info("应用已关闭")


async def load_config():
    await ensure_settings_row()
    await KeyValue.update_or_create(
        key="sys_start", defaults={"value": int(time.time() * 1000)}
    )
    await refresh_settings()

    await migrate_password_to_hash()

    # 密钥配置检查：未设置环境变量时打日志警告（不阻断启动，保持向后兼容）
    if not os.getenv("JWT_SECRET"):
        logger.warning("未设置环境变量 JWT_SECRET：JWT 签名密钥回退到管理员口令派生，强烈建议配置（openssl rand -hex 32）")
    if not os.getenv("ENCRYPTION_KEY"):
        logger.warning("未设置环境变量 ENCRYPTION_KEY：敏感配置加密密钥回退到管理员口令派生，强烈建议配置（openssl rand -hex 32）")

    ip_limit["error"].minutes = settings.errorMinute
    ip_limit["error"].count = settings.errorCount
    ip_limit["upload"].minutes = settings.uploadMinute
    ip_limit["upload"].count = settings.uploadRateLimitCount


async def migrate_password_to_hash():
    default_token = DEFAULT_CONFIG.get("admin_token", "")
    # 默认口令风险提示（明文或哈希后仍等于默认值，均视为未修改）
    if default_token and (
        settings.admin_token == default_token
        or (
            is_password_hashed(settings.admin_token)
            and verify_password(default_token, settings.admin_token)
        )
    ):
        logger.warning("检测到管理员口令仍为默认值，强烈建议登录后台修改密码，否则存在被入侵风险")
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


# ============ 动态 CORS 中间件 ============
# Starlette 的 CORSMiddleware 在应用启动时固化白名单，无法响应后台在线修改。
# 本中间件每次请求从运行时 settings.cors_origins 读取（配合 5 秒配置刷新即时生效），
# 在模块导入期挂载即可，不依赖 lifespan 时序。


class DynamicCORSMiddleware:
    def __init__(self, app):
        self.app = app

    def _origins(self) -> list:
        return [
            origin.strip()
            for origin in str(getattr(settings, "cors_origins", "")).split(",")
            if origin.strip()
        ]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        origins = self._origins()
        if not origins:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        method = headers.get("access-control-request-method")
        is_preflight = method is not None

        if not origin:
            await self.app(scope, receive, send)
            return

        allowed = origin in origins
        if is_preflight:
            # 预检：白名单外直接拒绝（浏览器会因缺少 allow-origin 拦截）
            if not allowed:
                await Response(status_code=400)(scope, receive, send)
                return
            response = Response(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin",
                },
            )
            await response(scope, receive, send)
            return

        # 普通请求：仅白名单 Origin 注入 CORS 头（非白名单请求浏览器默认禁止跨域读取）
        async def send_with_cors(message):
            if message["type"] == "http.response.start" and allowed:
                headers_list = list(message.get("headers", []))
                headers_list.append((b"access-control-allow-origin", origin.encode()))
                headers_list.append((b"access-control-expose-headers", b"Content-Disposition"))
                headers_list.append((b"vary", b"Origin"))
                message["headers"] = headers_list
            await send(message)

        await self.app(scope, receive, send_with_cors)


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

# 挂载动态 CORS 中间件（白名单运行时读取 settings.cors_origins，后台修改即时生效）
app.add_middleware(DynamicCORSMiddleware)

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
app.include_router(room_api)
app.include_router(compat_room_api)


# API 前缀：这些路径的 404 应返回 JSON（而非回退到 SPA 首页 HTML），便于前端统一处理
_API_PREFIXES = ("/admin", "/share", "/chunk", "/presign", "/collection", "/delivery", "/room", "/ws")


@app.exception_handler(404)
async def not_found_handler(request, exc=None):
    path = getattr(getattr(request, "url", None), "path", "") or ""
    if path.startswith(_API_PREFIXES):
        return JSONResponse(status_code=404, content={"code": 404, "message": "接口不存在", "detail": None})
    # 非 API 路径：返回 SPA 首页（hash 路由由前端接管）
    return await index(request, exc)


@app.get("/")
async def index(request=None, exc=None):
    # 模板注入前转义，防止后台配置被篡改后注入任意 HTML/脚本（存储型 XSS 面）
    return HTMLResponse(
        content=open(
            BASE_DIR / f"{settings.themesSelect}/index.html", "r", encoding="utf-8"
        )
        .read()
        .replace("{{title}}", html.escape(str(settings.name)))
        .replace("{{description}}", html.escape(str(settings.description)))
        .replace("{{keywords}}", html.escape(str(settings.keywords)))
        .replace("{{opacity}}", html.escape(str(settings.opacity)))
        .replace('"/assets/', '"assets/')
        .replace("{{background}}", html.escape(str(settings.background))),
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
            # 与前端字段名保持一致，避免首页页面说明缺失
            "page_explain": settings.page_explain,
            "uploadSize": settings.uploadSize,
            "expireStyle": settings.expireStyle,
            "enableChunk": settings.enableChunk,
            "openUpload": settings.openUpload,
            "notify_title": settings.notify_title,
            "notify_content": settings.notify_content,
            "show_admin_address": settings.showAdminAddr,
            "expiredRetentionStyle": getattr(settings, "expiredRetentionStyle", "day"),
            "expiredRetentionValue": getattr(settings, "expiredRetentionValue", 1),
            "maxMultiFileCount": getattr(settings, "maxMultiFileCount", 20),
            "maxCollectionFiles": getattr(settings, "maxCollectionFiles", 100),
            "maxSendFiles": getattr(settings, "maxSendFiles", 20),
            "maxTextLength": getattr(settings, "maxTextLength", 200000),
            "directRelayEnabled": getattr(settings, "directRelayEnabled", 1),
            "directRelaySpeedLimit": getattr(settings, "directRelaySpeedLimit", 0),
            "mediaDefaultQuality": getattr(settings, "mediaDefaultQuality", "auto"),
            "defaultMaxMembers": getattr(settings, "defaultMaxMembers", 10),
            # 公共接口不暴露 TURN 凭据（username/credential），仅公开 urls；
            # 完整凭据仅在加入实时房间后由 WebSocket 信令（turn_servers）下发
            "directTurnServers": [
                {"urls": s.get("urls")}
                for s in getattr(settings, "directTurnServers", [])
                if isinstance(s, dict) and s.get("urls")
            ],
            # 口令/码位数（4~12），供前端取件/投递输入与自动提交使用
            "codeLength": getattr(settings, "codeLength", 6),
            # 前端 SPA 站点默认主题（light / dark / enterprise），供前端实时同步
            "spaTheme": getattr(settings, "spaTheme", "light"),
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
