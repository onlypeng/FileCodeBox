"""Migration 004：给 uploadchunk 增加 save_path，让分片落盘位置可追溯。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections


async def add_save_path_to_uploadchunk():
    conn = connections.get("default")
    await conn.execute_script(
        """
        ALTER TABLE uploadchunk ADD COLUMN save_path VARCHAR(512) NULL;
        """
    )


async def migrate():
    await add_save_path_to_uploadchunk()
