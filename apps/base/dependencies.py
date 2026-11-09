"""匿名侧请求依赖：IP 识别与 IP 限流。

## 问题
本站默认开放匿名上传与取件，"一个 IP 无限刷"是最现实的滥用方式：既会打爆磁盘，
也能用来暴力枚举取件码。而限流如果放在进程内存里，多 worker 部署时
每个进程各算各的，**实际放行量是配置值的 N 倍**，限流形同虚设。

## 方案
计数落库（apps/base/models.py:IPLimit），多 worker 共享同一份计数；
递增用 SQL 层的 `F("count") + 1` 而不是"读-改-写"，避免并发请求互相覆盖。

## 刻意不做
**不默认信任 `X-Forwarded-For`**。该头可被客户端任意伪造，
伪造后限流会被完全绕过。只有在 `trustedProxies` 里显式列出的上游地址
发来的转发头才会被采信，并且取"从右往左第一个不在可信列表里的 IP"——
否则攻击者只要在链尾附加一个可信值就能继续伪造。
"""
import datetime
from fastapi import HTTPException, Request
from tortoise.expressions import F

from apps.base.models import IPLimit
from core.settings import settings
from core.utils import get_now


def resolve_client_ip(request: Request) -> str:
    """解析可信来源 IP：仅在 request.client 命中 trustedProxies 时采信转发头。

    抽成模块级函数，让限流依赖与审计中间件共用同一口径——两处各自实现时，
    反代场景下审计记网关地址、限流记真实 IP，排查时会对不上。
    """
    client_ip = request.client.host if request.client else "unknown"
    trusted_proxies = getattr(settings, "trustedProxies", "")
    if trusted_proxies:
        trusted_list = [ip.strip() for ip in trusted_proxies.split(",") if ip.strip()]
        if client_ip in trusted_list:
            # 仅信任来自可信代理的转发头
            forwarded_for = request.headers.get("X-Forwarded-For")
            if forwarded_for:
                # 取从右往左第一个非可信代理的 IP
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
        """是否允许该 IP 继续请求（True = 放行）。

        只查询不写入，因此**不会**刷新窗口——窗口只在 add_ip 时推进。
        这个不对称是刻意的：失败请求不应延长封禁时间。
        """
        if not self.key:
            return True
        record = await IPLimit.filter(bucket=self.key, ip=ip).first()
        if record and record.count >= self.count:
            # get_now 与 Tortoise 的 timezone 配置（Asia/Shanghai）一致，可直接比较
            if record.last_time + datetime.timedelta(minutes=self.minutes) > await get_now():
                return False
        return True

    async def add_ip(self, ip: str) -> int:
        """计数 +1，返回递增后的值。副作用：会写库（首次访问会插入记录）。

        调用约定：**只在判定为"失败/违规"的分支里调用**（登录失败、取件码错误等）。
        把它用在成功路径上会让正常用户被自己刷出去。
        窗口过期时重置为 1 而非累加，否则一次古老的失败会永远压着计数。
        """
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
        """FastAPI 依赖入口：解析客户端 IP 并做限流校验。

        返回解析出的 IP 字符串，供调用方写审计日志——**不要**再用
        `request.client.host` 重新取，二者在反代场景下并不一致。
        超限抛 423（Locked）而非 429：前端按 423 展示"请求次数过多，请稍后再试"，
        改状态码会破坏既有前端分支。
        """
        # 获取客户端真实IP，防止伪造（口径与审计中间件共用 resolve_client_ip）
        client_ip = resolve_client_ip(request)
        if not await self.check_ip(client_ip):
            raise HTTPException(status_code=423, detail="请求次数过多，请稍后再试")
        return client_ip
