from typing import Dict, Union, Optional
from datetime import datetime, timedelta
from fastapi import HTTPException, Request
import re

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
        client_ip = self.get_client_ip(request)
        if not self.check_ip(client_ip):
            raise HTTPException(status_code=423, detail="请求次数过多，请稍后再试")
        return client_ip

    @staticmethod
    def get_client_ip(request: Request) -> str:
        client_ip = request.client.host if request.client else "unknown"
        trusted_proxies = getattr(settings, "trustedProxies", "")
        if trusted_proxies:
            trusted_list = [ip.strip() for ip in trusted_proxies.split(",") if ip.strip()]
            if client_ip in trusted_list:
                forwarded_for = request.headers.get("X-Forwarded-For")
                if forwarded_for:
                    ips = [ip.strip() for ip in forwarded_for.split(",")]
                    for ip in reversed(ips):
                        if ip not in trusted_list:
                            client_ip = ip
                            break
                else:
                    real_ip = request.headers.get("X-Real-IP")
                    if real_ip:
                        client_ip = real_ip.strip()
        return client_ip


class AdminRateLimit:
    def __init__(self, count: int = 100, minutes: int = 15):
        self.ips: Dict[str, Dict[str, Union[int, datetime]]] = {}
        self.count = count
        self.minutes = minutes
        self.login_attempts: Dict[str, Dict[str, Union[int, datetime]]] = {}
        self.login_max_attempts = 5
        self.login_lockout_minutes = 15

    def check_login(self, ip: str) -> bool:
        if ip in self.login_attempts:
            attempt_info = self.login_attempts[ip]
            if attempt_info["count"] >= self.login_max_attempts:
                if attempt_info["time"] + timedelta(minutes=self.login_lockout_minutes) > datetime.now():
                    return False
                self.login_attempts.pop(ip)
        return True

    def record_login_failure(self, ip: str) -> int:
        ip_info = self.login_attempts.get(ip, {"count": 0, "time": datetime.now()})
        ip_info["count"] += 1
        ip_info["time"] = datetime.now()
        self.login_attempts[ip] = ip_info
        return ip_info["count"]

    def reset_login_attempts(self, ip: str) -> None:
        if ip in self.login_attempts:
            self.login_attempts.pop(ip)

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

    async def cleanup(self) -> None:
        now = datetime.now()
        expiration = timedelta(minutes=self.minutes)
        self.ips = {
            ip: info
            for ip, info in self.ips.items()
            if info["time"] + expiration >= now
        }
        login_expiration = timedelta(minutes=self.login_lockout_minutes)
        self.login_attempts = {
            ip: info
            for ip, info in self.login_attempts.items()
            if info["time"] + login_expiration >= now
        }

    def __call__(self, request: Request) -> str:
        client_ip = IPRateLimit.get_client_ip(request)
        
        if request.url.path.endswith("/login") and request.method == "POST":
            if not self.check_login(client_ip):
                raise HTTPException(status_code=423, detail="登录失败次数过多，请稍后再试")
        
        if not self.check_ip(client_ip):
            raise HTTPException(status_code=423, detail="管理后台请求次数过多，请稍后再试")
        
        self.add_ip(client_ip)
        return client_ip


def validate_admin_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密码长度至少8位")
    if not re.search(r"[a-zA-Z]", password):
        raise HTTPException(status_code=400, detail="密码必须包含字母")
    if not re.search(r"[0-9]", password):
        raise HTTPException(status_code=400, detail="密码必须包含数字")


def validate_file_name(name: str) -> str:
    if not name or len(name.strip()) == 0:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="文件名过长")
    if "\x00" in name:
        raise HTTPException(status_code=400, detail="文件名包含非法字符")
    return name.strip()


def validate_ip_address(ip: str) -> bool:
    ipv4_pattern = r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
    ipv6_pattern = r"^([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}$"
    return bool(re.match(ipv4_pattern, ip)) or bool(re.match(ipv6_pattern, ip))
