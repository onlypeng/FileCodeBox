"""可插拔病毒扫描钩子。

本项目的定位是自建轻量分享，不内置扫描引擎，但"有人上传了恶意文件"是真实
风险。这里留出钩子：上传落盘后回调外部扫描服务（本地命令或 HTTP 接口），
命中则拒绝并告警。

设计原则：
- **未配置就不启用**，零开销零副作用（默认关闭）
- 扫描失败不拖垮上传：扫描服务不可用时按 virusScanFailOpen 决定放行还是拒绝，
  默认放行并告警——扫描是辅助能力，不该因为扫描器挂了让整个上传功能不可用
- 命令钩子用参数列表执行，**不经 shell**，避免文件名被拼进命令行造成注入
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
from typing import Optional, Tuple

from core.logger import logger
from core.settings import settings


def enabled() -> bool:
    """是否启用扫描（未配置钩子时视为未启用）。"""
    return bool(getattr(settings, "virusScanEnabled", False)) and bool(
        (getattr(settings, "virusScanHook", "") or "").strip()
    )


def _timeout_seconds() -> int:
    try:
        return max(1, min(300, int(getattr(settings, "virusScanTimeoutSec", 30) or 30)))
    except Exception:
        return 30


def _run_command_sync(cmd_template: str, file_path: str, timeout: int) -> Tuple[bool, str]:
    """执行本地扫描命令。返回 (是否干净, 说明)。

    约定沿用 clamscan：返回码 0 = 干净，1 = 发现病毒，其它 = 扫描失败。
    命令用 {file} 占位符引用待扫描文件路径。
    """
    import subprocess

    # 不经 shell：参数按列表传递，文件名不会被当作命令解析。
    # posix=False 在 Windows 上必需——posix 模式会把路径里的反斜杠当转义字符吃掉。
    argv = [
        arg.replace("{file}", file_path)
        for arg in shlex.split(cmd_template, posix=(os.name != "nt"))
    ]
    if not argv:
        return True, ""
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError:
        return True, f"扫描命令不存在: {argv[0]}"
    except subprocess.TimeoutExpired:
        return True, f"扫描超时（{timeout}s）"

    out = (proc.stdout or b"").decode("utf-8", "ignore").strip()
    if proc.returncode == 0:
        return True, out[:200]
    if proc.returncode == 1:
        return False, out[:200] or "扫描器报告文件含恶意内容"
    return True, f"扫描异常退出码 {proc.returncode}: {out[:200]}"


async def scan_file(file_path: str) -> Tuple[bool, str, bool]:
    """扫描落盘文件。

    返回 (是否干净, 说明, 是否真的执行了扫描)。
    "未执行"包括：功能未启用、外部存储拿不到本地文件——这些都不应阻断上传。
    """
    if not file_path:
        return True, "", False
    if not enabled():
        return True, "", False

    hook = (getattr(settings, "virusScanHook", "") or "").strip()
    timeout = _timeout_seconds()

    if hook.startswith("http://") or hook.startswith("https://"):
        return await _scan_via_http(hook, file_path, timeout)

    # 命令模式在线程里跑，避免阻塞事件循环
    clean, detail = await asyncio.to_thread(_run_command_sync, hook, file_path, timeout)
    return clean, detail, True


async def _scan_via_http(url: str, file_path: str, timeout: int) -> Tuple[bool, str, bool]:
    """POST 文件到扫描服务，约定返回 JSON {"infected": bool, "reason": str}。"""
    try:
        import aiohttp
    except Exception:
        return True, "缺少 aiohttp，无法调用 HTTP 扫描钩子", True

    try:
        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            with open(file_path, "rb") as fp:
                data = aiohttp.FormData()
                data.add_field("file", fp.read(), filename="upload.bin")
                async with session.post(url, data=data) as resp:
                    if resp.status != 200:
                        return True, f"扫描服务返回 {resp.status}", True
                    try:
                        payload = await resp.json(content_type=None)
                    except Exception:
                        text = (await resp.text())[:200]
                        return True, f"扫描服务返回非 JSON: {text}", True
    except asyncio.TimeoutError:
        return True, f"扫描超时（{timeout}s）", True
    except Exception as e:
        return True, f"调用扫描服务失败: {e}", True

    infected = bool(payload.get("infected"))
    reason = str(payload.get("reason") or "")[:200]
    return (not infected), (reason or ("扫描器报告文件含恶意内容" if infected else "")), True


def should_block_when_scan_failed(detail: str) -> bool:
    """扫描"没得出结论"时是否阻断上传。

    fail-open（默认）：放行并告警，扫描器挂掉不该让上传功能整体不可用。
    fail-closed：拒绝，适合对安全要求高于可用性的部署。
    """
    if getattr(settings, "virusScanFailOpen", True):
        logger.warning(f"病毒扫描未得出结论，按配置放行: {detail}")
        return False
    logger.error(f"病毒扫描未得出结论，按配置拒绝上传: {detail}")
    return True
