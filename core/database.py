"""数据库初始化：连接配置生成 + 自建迁移执行。

## 问题

项目用 Tortoise ORM，但它的 `generate_schemas` 只会按 model 建表，**不会加列**。
而本项目要给已上线的数据加字段（例如给 room 表加容量开关），升级后旧实例启动时
表结构仍是旧的，查询直接报错。同时多进程部署下多个 worker 会同时跑到初始化，
建表/加列会互相踩。

此外 SQLite 是多写者不安全的单文件库，多个 worker 同时执行"建表 + 加列"会互相
踩踏，因此需要一个**跨进程**的文件锁把这段结构变更串行化。

## 方案

- 连接配置集中在 `get_db_config()`：一行 `dbType` 决定 sqlite / postgres / mysql，
  业务代码不许再写方言假设（详见 core/db_compat.py）。
- 自建迁移：约定 `apps/**/migrations/migrations_*.py` 提供 `async def migrate()`，
  按**文件名字典序**执行，已执行的文件名记进 `migrates` 表做幂等，
  因此每个迁移文件必须自己写"列不存在才加"（用 `core.db_compat.column_exists`）。
- 启动锁 `db_startup_lock()`：把"建 migrates 表 + 跑迁移"这段跨进程串行化，
  Windows 用 msvcrt、POSIX 用 flock 各写一套，`asyncio.to_thread` 包起来避免
  阻塞事件循环。锁是**咨询锁**：靠所有实例都遵守这个约定生效。

## 刻意不做

不引入 Aerich（Tortoise 官方迁移工具）。它需要在仓库里维护迁移快照并与 Tortoise
版本绑定，而这个项目的表结构变更频率低、且全部是"幂等加列/建索引"这类安全变更；
引入迁移工具带来的版本号耦合与额外命令，代价大于收益。

同样，不把这个锁做成跨主机分布式锁：它只解决"单机多 worker 共用同一份 data 目录"
场景；跨主机应当用 postgres / mysql 并把结构变更放到发布流程里，而不是让每个
实例各自跑迁移互相竞争。
"""
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
    """申请排他文件锁（同步，调用方在线程里跑）。

    两套实现并存是必需的：Windows 没有 fcntl，而 msvcrt.LK_LOCK 是阻塞式的，
    进程崩退时由 OS 自动释放（这正是我们要的 —— 死锁不该让下次启动永远起不来）。
    """
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
    """串行化"启动期结构变更"的咨询锁。

    用 "a+" 打开：只追加不清空，避免两个实例同时启动时互相把对方的锁文件截断。
    进程退出或崩溃时 OS 自动释放，不需要清理逻辑。
    注意它保护的是**本机**上的多 worker（同一份 data 目录），不是跨主机。
    """
    os.makedirs(data_root, exist_ok=True)
    lock_file = open(_STARTUP_LOCK_FILE, "a+", encoding="utf-8")
    try:
        await asyncio.to_thread(_lock_file, lock_file)
        yield
    finally:
        await asyncio.to_thread(_unlock_file, lock_file)
        lock_file.close()


async def init_db():
    """初始化连接与表结构，允许多次调用（靠 `Tortoise._inited` 短路）。

    副作用：建 `migrates` 表并执行迁移。失败原样向上抛 —— 结构不对继续跑只会
    在查询时才报更费解的错，宁可启动失败。
    """
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
    """按文件名字典序执行 `apps/**/migrations/migrations_*.py`。

    约定（调用方与迁移作者都须知悉）：
    - 已执行过的文件名记录在 `migrates` 表，**再次启动不会再跑**；
      迁移文件上线后被修改，对旧实例无效，必须新建一个顺序更大的文件。
    - 迁移实现必须是幂等的（先判 `column_exists` / `table_exists`），
      因为"记录文件"与"执行完成"之间有窗口：执行成功但记账失败时会重试。
    - 任一迁移失败即中止并向上抛，后续迁移不会执行 —— 迁移之间有依赖时
      顺序必须是显式的（靠文件名的字典序前缀，如 migrations_001_...）。
    """
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
