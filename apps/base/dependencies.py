import datetime
from fastapi import HTTPException, Request
from tortoise.expressions import F

from apps.base.models import IPLimit
from core.settings import settings
from core.utils import get_now


class IPRateLimit:
    """IP 限流：计数持久化到数据库（多 worker 共享，进程内不再各自计数）

    key 为限流桶名（error / upload），同一 IP 在时间窗口内的计数跨 worker 一致。
    时间使用 naive 本地时间（与原实现一致，避免 Tortoise 时区往返差异）。
    """

    def __init__(self, count: int, minutes: int, key: str = ""):
        self.count = count
        self.minutes = minutes
        self.key = key

    async def check_ip(self, ip: str) -> bool:
        if not self.key:
            return True
        record = await IPLimit.filter(bucket=self.key, ip=ip).first()
        if record and record.count >= self.count:
            # get_now 与 Tortoise 的 timezone 配置（Asia/Shanghai）一致，可直接比较
            if record.last_time + datetime.timedelta(minutes=self.minutes) > await get_now():
                return False
        return True

    async def add_ip(self, ip: str) -> int:
        if not self.key:
            return 0
        now = await get_now()
        record = await IPLimit.filter(bucket=self.key, ip=ip).first()
        if record is None:
            await IPLimit.create(bucket=self.key, ip=ip, count=1, last_time=now)
            return 1
        # 窗口已过期：重置计数并重新计时
        if record.last_time + datetime.timedelta(minutes=self.minutes) <= now:
            await IPLimit.filter(id=record.id).update(count=1, last_time=now)
            return 1
        # 原子递增，防止并发请求互相覆盖
        await IPLimit.filter(id=record.id).update(count=F("count") + 1, last_time=now)
        updated = await IPLimit.filter(id=record.id).first()
        return updated.count if updated else 1

    async def remove_expired_ip(self) -> None:
        """清理时间窗口外已过期的记录（由后台任务周期调用）"""
        if not self.key:
            return
        expire_before = await get_now() - datetime.timedelta(minutes=self.minutes)
        await IPLimit.filter(bucket=self.key, last_time__lt=expire_before).delete()

    async def __call__(self, request: Request) -> str:
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
        if not await self.check_ip(client_ip):
            raise HTTPException(status_code=423, detail="请求次数过多，请稍后再试")
        return client_ip
