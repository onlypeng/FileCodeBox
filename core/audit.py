"""审计日志写入。

设计要点：审计是旁路能力，**绝不能因为审计失败影响主流程**（上传成功却因为审计
写不进去而返回失败是不可接受的）。因此 record() 内部吞掉所有异常并记日志。
"""
from __future__ import annotations

from core.logger import logger


async def record(
    action: str,
    target_type: str = "",
    target_id: str = "",
    result: str = "success",
    detail: str = "",
    ip: str = "",
    actor: str = "",
) -> None:
    """写入一条审计记录；失败只记日志，不向上抛。"""
    try:
        from apps.base.models import AuditLog

        await AuditLog.create(
            action=(action or "")[:32],
            target_type=(target_type or "")[:32],
            target_id=(target_id or "")[:64],
            result=(result or "success")[:16],
            detail=(detail or "")[:255],
            ip=(ip or "")[:64],
            actor=(actor or "")[:64],
        )
    except Exception as e:
        # 审计失败不影响业务：只留日志，避免主流程被拖垮
        logger.warning(f"写入审计日志失败 action={action}: {e}")
