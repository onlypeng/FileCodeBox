from typing import Dict, Union
from datetime import datetime, timedelta
from fastapi import HTTPException, Request

from core.settings import settings


class IPRateLimit:
    def __init__(self, count: int, minutes: int):
        self.ips: Dict[str, Dict[str, Union[int, datetime]]] = {}
        self.count = count
        self.minutes = minutes

    def check_ip(self, ip: str) -> bool:
        if ip in self.ips:
            ip_info = self.ips[ip]
            if ip_info["count"] >= self.count:
                if ip_info["time"] + timedelta(minutes=self.minutes) > datetime.now():
                    return False
                self.ips.pop(ip)
        return True

    def add_ip(self, ip: str) -> int:
        ip_info = self.ips.get(ip, {"count": 0, "time": datetime.now()})
        ip_info["count"] += 1
        ip_info["time"] = datetime.now()
        self.ips[ip] = ip_info
        return ip_info["count"]

    async def remove_expired_ip(self) -> None:
        now = datetime.now()
        expiration = timedelta(minutes=self.minutes)
        self.ips = {
            ip: info
            for ip, info in self.ips.items()
            if info["time"] + expiration >= now
        }

    def __call__(self, request: Request) -> str:
        # 获取客户端真实IP，防止伪造
        client_ip = request.client.host if request.client else "unknown"
        trusted_proxies = getattr(settings, "trustedProxies", "")
        if trusted_proxies:
            trusted_list = [ip.strip() for ip in trusted_proxies.split(",") if ip.strip()]
            if client_ip in trusted_list:
                # 仅信任来自可信代理的转发头
                forwarded_for = request.headers.get("X-Forwarded-For")
                if forwarded_for:
                    # 取第一个非可信代理的IP
                    ips = [ip.strip() for ip in forwarded_for.split(",")]
                    for ip in reversed(ips):
                        if ip not in trusted_list:
                            client_ip = ip
                            break
                else:
                    real_ip = request.headers.get("X-Real-IP")
                    if real_ip:
                        client_ip = real_ip.strip()
        if not self.check_ip(client_ip):
            raise HTTPException(status_code=423, detail="请求次数过多，请稍后再试")
        return client_ip
