from tortoise import connections
from core.logger import logger


async def migrate():
    """迁移 021：存量文本（迁移 020 落盘的 share/text/*.txt）读回 remark 字段。
    文本分享统一模型：文本内容存 FileCodes.remark（备注字段），无独立文件路径。
    迁移后存量文本记录 remark=原内容、file_path=NULL，与新建文本记录一致。
    """
    conn = connections.get("default")
    try:
        rows = await conn.execute_query_dict(
            "SELECT id, file_path, uuid_file_name, remark FROM filecodes WHERE file_path LIKE 'share/text/%'"
        )
        if not rows:
            logger.info("Migration 021: 无待迁移的存量文本")
            return
        from core.settings import settings, data_root
        storage_path = (settings.storage_path or "").strip("/")
        base = data_root if not storage_path else data_root / storage_path
        text_dir = base / "share" / "text"
        migrated = 0
        for row in rows:
            if row.get("remark"):
                continue  # 已有备注则跳过
            fname = row.get("uuid_file_name") or f"{row['id']}.txt"
            fpath = text_dir / fname
            try:
                content = fpath.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning(f"Migration 021: 读取文本 {row['id']} 失败: {e}")
                continue
            await conn.execute_query(
                "UPDATE filecodes SET remark = ?, file_path = NULL, uuid_file_name = NULL WHERE id = ?",
                [content, row["id"]],
            )
            migrated += 1
        logger.info(f"Migration 021: 已将 {migrated} 条存量文本写入 remark")
    except Exception as e:
        logger.error(f"Migration 021 failed: {e}")
        raise
