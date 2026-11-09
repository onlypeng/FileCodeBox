"""审计日志写入。

设计要点：审计是旁路能力，**绝不能因为审计失败影响主流程**（上传成功却因为审计
写不进去而返回失败是不可接受的）。因此 record() 内部吞掉所有异常并记日志。
"""
from __future__ import annotations

import contextvars

from core.logger import logger

# 当前请求的可信来源 IP，由 main.py 的 audit_ip_middleware 在请求入口写入。
# 用 ContextVar 而不是给每条 record() 调用补 ip= 参数：后台有十几处写操作，
# 逐个补既易漏，也让"审计 IP"变成可选而非默认行为。
_request_ip: contextvars.ContextVar[str] = contextvars.ContextVar(
    "audit_request_ip", default=""
)


def set_request_ip(ip: str) -> None:
    """写入当前请求的可信来源 IP（见 apps/base/dependencies.resolve_client_ip）。"""
    _request_ip.set(ip or "")


def get_request_ip() -> str:
    """取当前请求 IP；非 HTTP 上下文（定时任务、启动流程）返回空串。"""
    return _request_ip.get()


async def record(
    action: str,
    target_type: str = "",
    target_id: str = "",
    result: str = "success",
    detail: str = "",
    ip: str = "",
    actor: str = "",
) -> None:
    """写入一条审计记录；失败只记日志，不向上抛。

    显式传 ip 优先；未传时回落到请求上下文（后台写操作不必再逐个注入依赖）。
    """
    ip = ip or get_request_ip()
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
