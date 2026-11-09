"""Migration 020：把历史上直接存在数据库里的文本内容落盘为文件，统一到文件存储层。

迁移由 core/database.py 顺序执行，**函数名必须是 migrate()**；
写成 upgrade() 会被静默跳过（runner 只认 migrate 属性），直到运行时报 no such column 才暴露。
迁移必须幂等：已存在列/表时安全跳过，因为每次启动都会重新扫描。
"""
from tortoise import connections
from core.logger import logger


async def migrate():
    """迁移 020：文本分享合并到文件分享。
    1) 将存量 text 字段内容落盘为文件（share/text/{date}/{code}.txt，便于统一按文件取件/下载）
    2) 删除 FileCodes.text 列
    说明：新代码 share_text 已改为直接落盘文件，text 列不再写入。
    """
    conn = connections.get("default")
    try:
        # 1) 落盘存量文本
        rows = await conn.execute_query_dict(
            "SELECT id, code, text FROM filecodes WHERE text IS NOT NULL AND text != ''"
        )
        failed: list[str] = []
        if rows:
            # 用 settings.storage_path 拼接（与后端一致）；取默认 data 目录
            from core.settings import settings, data_root
            storage_path = (settings.storage_path or "").strip("/")
            base = data_root if not storage_path else data_root / storage_path
            text_dir = base / "share" / "text"
            text_dir.mkdir(parents=True, exist_ok=True)
            for row in rows:
                fname = f"{row['code']}.txt"
                fpath = text_dir / fname
                try:
                    fpath.write_text(row["text"] or "", encoding="utf-8")
                except Exception as e:
                    logger.warning(f"Migration 020: 落盘文本 {row['code']} 失败: {e}")
                    failed.append(str(row["code"]))
                    continue
                rel = f"share/text/{fname}"
                await conn.execute_query(
                    "UPDATE filecodes SET file_path = ?, uuid_file_name = ?, prefix = ?, suffix = ?, size = ? WHERE id = ?",
                    [rel, fname, "Text", ".txt", len(row["text"] or ""), row["id"]],
                )
            logger.info(f"Migration 020: 已落盘 {len(rows) - len(failed)} 条存量文本")
        if failed:
            # 关键：单行落盘失败时**不能** DROP COLUMN text。
            # 旧写法只 continue 就无条件删列，那几行正文会随列一起消失且不可恢复。
            # 这里抛异常让迁移不记账（execute_migrations 失败即不 INSERT migrates），
            # 运维修复磁盘/权限后重启会整体重跑；已成功的行重复落盘是幂等的。
            logger.error(
                f"Migration 020: {len(failed)} 条文本落盘失败，保留 text 列待重跑: {failed[:10]}"
            )
            raise RuntimeError(
                f"Migration 020: 文本落盘失败 {len(failed)} 条，已保留 text 列"
            )
        # 2) 删除 text 列
        cols = await conn.execute_query_dict("PRAGMA table_info(filecodes)")
        if any(c["name"] == "text" for c in cols):
            await conn.execute_script("ALTER TABLE filecodes DROP COLUMN text")
            logger.info("Migration 020: 已删除 filecodes.text 列")
        logger.info("Migration 020 completed successfully")
    except Exception as e:
        logger.error(f"Migration 020 failed: {e}")
        raise
