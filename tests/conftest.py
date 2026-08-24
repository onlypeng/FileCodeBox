# 测试共享夹具：初始化独立临时 SQLite 数据库，并挂载分享路由
import sys
from pathlib import Path

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tortoise import Tortoise

# 确保项目根目录可被导入（apps / core 包）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.admin.views import admin_api  # noqa: E402
from apps.base.views import (  # noqa: E402
    share_api,
    chunk_api,
    presign_api,
    collection_api,
    delivery_api,
    room_api,
)


def _build_test_db_config(db_path: str) -> dict:
    """构建使用独立临时 SQLite 的 Tortoise 配置（不跑迁移、不依赖真实数据）。"""
    return {
        "connections": {
            "default": {
                "engine": "tortoise.backends.sqlite",
                "credentials": {"file_path": db_path},
            }
        },
        "apps": {
            "models": {
                "models": ["apps.base.models"],
                "default_connection": "default",
            }
        },
        "use_tz": False,
        "timezone": "Asia/Shanghai",
    }


@pytest_asyncio.fixture
async def tortoise_db(tmp_path):
    """初始化独立临时 SQLite（供纯模型/服务层测试复用），每个测试使用独立临时数据库。"""
    db_file = tmp_path / "test_filecodebox.db"
    # Tortoise.init 会先关闭旧连接再初始化，可安全复用
    await Tortoise.init(config=_build_test_db_config(str(db_file)))
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


@pytest_asyncio.fixture
async def client(tmp_path, tortoise_db):
    """每个测试使用独立临时数据库，提供异步 HTTP 客户端（挂载全部业务路由）。"""
    app = FastAPI()
    app.include_router(share_api)
    app.include_router(chunk_api)
    app.include_router(presign_api)
    app.include_router(collection_api)
    app.include_router(delivery_api)
    app.include_router(room_api)
    app.include_router(admin_api)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def admin_token(client):
    """使用默认管理员口令登录，返回管理 Token。"""
    resp = await client.post(
        "/admin/login", json={"password": "FileCodeBox2023"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["detail"]["token"]
