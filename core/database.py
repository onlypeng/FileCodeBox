import asyncio
import glob
import importlib
import os
from contextlib import asynccontextmanager
from typing import IO

from tortoise import Tortoise

from core.logger import logger
from core.settings import data_root


_DB_FILE = os.path.join(data_root, "filecodebox.db")
_STARTUP_LOCK_FILE = os.path.join(data_root, "filecodebox.startup.lock")


def get_db_config() -> dict:
    """按后台配置的 dbType 生成连接配置。

    - sqlite（默认）：本地文件，单写者，仅支持单进程部署
    - postgres：tortoise.backends.asyncpg
    - mysql：tortoise.backends.mysql（需 asyncmy / aiomysql）

    多进程能力由 core.capabilities 按类型判定，不在连接层硬编码业务限制。
    """
    from core.settings import settings

    db_type = str(getattr(settings, "dbType", "sqlite") or "sqlite").strip().lower()

    if db_type == "postgres":
        credentials = {
            "host": getattr(settings, "dbHost", "127.0.0.1") or "127.0.0.1",
            "port": int(getattr(settings, "dbPort", 5432) or 5432),
            "user": getattr(settings, "dbUser", "") or "",
            "password": getattr(settings, "dbPassword", "") or "",
            "database": getattr(settings, "dbName", "filecodebox") or "filecodebox",
            "schema": getattr(settings, "dbSchema", "public") or "public",
        }
        engine = "tortoise.backends.asyncpg"
    elif db_type == "mysql":
        credentials = {
            "host": getattr(settings, "dbHost", "127.0.0.1") or "127.0.0.1",
            "port": int(getattr(settings, "dbPort", 3306) or 3306),
            "user": getattr(settings, "dbUser", "") or "",
            "password": getattr(settings, "dbPassword", "") or "",
            "database": getattr(settings, "dbName", "filecodebox") or "filecodebox",
        }
        engine = "tortoise.backends.mysql"
    else:
        credentials = {
            "file_path": _DB_FILE,
            "journal_mode": "WAL",
            "busy_timeout": 10000,
            "foreign_keys": "ON",
        }
        engine = "tortoise.backends.sqlite"

    return {
        "connections": {"default": {"engine": engine, "credentials": credentials}},
        "apps": {
            "models": {
                "models": ["apps.base.models"],
                "default_connection": "default",
            }
        },
        "use_tz": False,
        "timezone": "Asia/Shanghai",
    }


def _lock_file(file_obj: IO[str]) -> None:
    if os.name == "nt":
        import msvcrt

        # Windows 需要锁定至少 1 字节
        if os.fstat(file_obj.fileno()).st_size == 0:
            file_obj.write("0")
            file_obj.flush()
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)


def _unlock_file(file_obj: IO[str]) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def db_startup_lock():
    os.makedirs(data_root, exist_ok=True)
    lock_file = open(_STARTUP_LOCK_FILE, "a+", encoding="utf-8")
    try:
        await asyncio.to_thread(_lock_file, lock_file)
        yield
    finally:
        await asyncio.to_thread(_unlock_file, lock_file)
        lock_file.close()


async def init_db():
    try:
        db_config = get_db_config()

        if not Tortoise._inited:
            await Tortoise.init(config=db_config)

        async with db_startup_lock():
            # 创建migrations表
            await Tortoise.get_connection("default").execute_script("""
                CREATE TABLE IF NOT EXISTS migrates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    migration_file VARCHAR(255) NOT NULL UNIQUE,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 执行迁移
            await execute_migrations()

    except Exception as e:
        logger.error(f"数据库初始化失败: {str(e)}")
        raise


async def execute_migrations():
    """执行数据库迁移"""
    try:
        # 收集迁移文件
        migration_files = []
        for root, dirs, files in os.walk("apps"):
            if "migrations" in dirs:
                migration_path = os.path.join(root, "migrations")
                migration_files.extend(glob.glob(os.path.join(migration_path, "migrations_*.py")))

        # 按文件名排序
        migration_files.sort()

        for migration_file in migration_files:
            file_name = os.path.basename(migration_file)

            # 检查是否已执行
            executed = await Tortoise.get_connection("default").execute_query(
                "SELECT id FROM migrates WHERE migration_file = ?", [file_name]
            )

            if not executed[1]:
                logger.info(f"执行迁移: {file_name}")
                # 导入并执行migration
                module_path = migration_file.replace("/", ".").replace("\\", ".").replace(".py", "")
                try:
                    migration_module = importlib.import_module(module_path)
                    if hasattr(migration_module, "migrate"):
                        await migration_module.migrate()
                        # 记录执行
                        await Tortoise.get_connection("default").execute_query(
                            "INSERT INTO migrates (migration_file) VALUES (?)",
                            [file_name]
                        )
                        logger.info(f"迁移完成: {file_name}")
                except Exception as e:
                    logger.error(f"迁移 {file_name} 执行失败: {str(e)}")
                    raise

    except Exception as e:
        logger.error(f"迁移过程发生错误: {str(e)}")
        raise
