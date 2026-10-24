"""TURN 连通性自检。

对称 NAT / 严格防火墙下 P2P 直连会失败，此时依赖 TURN 中继。但 TURN 配置错误
（域名解析失败、端口未放行、协议不匹配）在服务端没有任何征兆——只有用户报告
"传文件一直卡住"才会被发现。

本模块提供后台可调用的探测：对配置了 transport=tcp 的 TURN 做 TCP 连通性测试，
UDP 的无法用 TCP 探测，单独标注说明。

注意：这只验证"端口可达"，不验证凭据是否正确——凭据问题需要在真实协商中暴露。
"""
from __future__ import annotations

import asyncio
import socket
from urllib.parse import urlparse

from core.logger import logger
from core.settings import settings


def _parse_turn_url(url: str) -> tuple[str, int, str]:
    """解析 TURN 地址，返回 (host, port, transport)。

    标准写法是 `turn:host:port?transport=udp`——**没有 `//`**，所以 urlparse 会把
    `host:port` 放到 path 而不是 netloc，直接用 parsed.hostname 会拿到空串。
    这里兼容三种写法：turn:host:port、turn://host:port、turns:host:port。
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    default_port = 5349 if scheme == "turns" else 3478

    host = parsed.hostname or ""
    port = parsed.port or default_port
    if not host:
        # 无 // 的写法：host:port 落在 path 里
        remainder = parsed.path or ""
        # 去掉可能残留的 netloc 形式（"//host:port"）
        remainder = remainder.lstrip("/")
        if ":" in remainder:
            host, _, port_s = remainder.rpartition(":")
            if port_s.isdigit():
                port = int(port_s)
        else:
            host = remainder
        if not port:
            port = default_port

    transport = "udp"
    query = (parsed.query or "").lower()
    if "transport=tcp" in query:
        transport = "tcp"
    elif "transport=udp" in query:
        transport = "udp"
    return host, port, transport


async def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    if not host:
        return False, "缺少主机名"
    try:
        # 解析放线程里，避免 DNS 阻塞事件循环
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, proto=socket.IPPROTO_TCP)
    except Exception as e:
        return False, f"解析失败: {type(e).__name__}"
    if not infos:
        return False, "解析无结果"

    last_err = ""
    for info in infos[:3]:
        family, socktype, proto, _canon, addr = info
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(timeout)
            try:
                await asyncio.to_thread(sock.connect, addr)
                return True, "可达"
            finally:
                sock.close()
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return False, last_err or "连接失败"


async def check_turn_servers() -> dict:
    """逐个探测后台配置的 TURN 服务器，返回可达性结果。"""
    try:
        servers = getattr(settings, "directTurnServers", None) or []
    except Exception as e:
        logger.warning(f"读取 TURN 配置失败: {e}")
        servers = []

    results = []
    for item in servers:
        if not isinstance(item, dict):
            continue
        urls = item.get("urls")
        url_list = urls if isinstance(urls, list) else ([urls] if isinstance(urls, str) else [])
        for url in url_list:
            if not isinstance(url, str) or not url.strip():
                continue
            host, port, transport = _parse_turn_url(url.strip())
            if transport == "tcp":
                ok, msg = await _tcp_reachable(host, port)
                status = "ok" if ok else "unreachable"
            else:
                ok, msg = True, "UDP 无法用 TCP 探测，需实际协商验证"
                status = "unknown"
            results.append({
                "url": url.strip(),
                "host": host,
                "port": port,
                "transport": transport,
                "status": status,
                "message": msg,
                "has_credential": bool(item.get("username") and item.get("credential")),
            })

    configured = len(results)
    unreachable = sum(1 for r in results if r["status"] == "unreachable")
    return {
        "configured": configured,
        "unreachable": unreachable,
        "relay_enabled": int(getattr(settings, "directRelayEnabled", 1) or 0) != 0,
        "results": results,
    }
