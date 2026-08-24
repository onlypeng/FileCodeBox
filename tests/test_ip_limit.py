# 验证 IP 限流持久化到数据库：计数递增、窗口过期重置、过期记录清理、多实例共享
import datetime

import pytest
from fastapi import HTTPException

from apps.base.dependencies import IPRateLimit
from apps.base.models import IPLimit
from core.utils import get_now


async def test_upload_bucket_counts_and_blocks(tortoise_db):
    """上传桶计数递增，达到上限后同一 IP 被拦截。"""
    limit = IPRateLimit(count=3, minutes=10, key="upload")
    assert await limit.add_ip("1.2.3.4") == 1
    assert await limit.add_ip("1.2.3.4") == 2
    assert await limit.add_ip("1.2.3.4") == 3
    assert await limit.check_ip("1.2.3.4") is False  # 达到上限
    # 未达上限的其他 IP 不受影响
    assert await limit.check_ip("5.6.7.8") is True


async def test_error_bucket_separate_from_upload(tortoise_db):
    """error 桶与 upload 桶相互独立计数。"""
    upload_limit = IPRateLimit(count=2, minutes=10, key="upload")
    error_limit = IPRateLimit(count=2, minutes=10, key="error")
    await upload_limit.add_ip("1.2.3.4")
    await upload_limit.add_ip("1.2.3.4")
    # 上传桶已满，但错误桶仍是 0，放行
    assert await error_limit.check_ip("1.2.3.4") is True
    await error_limit.add_ip("1.2.3.4")
    await error_limit.add_ip("1.2.3.4")
    assert await error_limit.check_ip("1.2.3.4") is False


async def test_window_expiry_resets_count(tortoise_db):
    """时间窗口过期后计数重置，重新放行。"""
    limit = IPRateLimit(count=2, minutes=1, key="upload")
    await limit.add_ip("9.9.9.9")
    await limit.add_ip("9.9.9.9")
    assert await limit.check_ip("9.9.9.9") is False
    # 回拨 last_time 到窗口外（模拟时间流逝）
    record = await IPLimit.filter(bucket="upload", ip="9.9.9.9").first()
    record.last_time = await get_now() - datetime.timedelta(minutes=5)
    await record.save()
    assert await limit.check_ip("9.9.9.9") is True  # 窗口过期，放行
    assert await limit.add_ip("9.9.9.9") == 1  # 重置为 1
    record = await IPLimit.filter(bucket="upload", ip="9.9.9.9").first()
    assert record.count == 1


async def test_remove_expired_ip_cleans_stale_records(tortoise_db):
    """过期记录被后台任务清理，未过期记录保留。"""
    limit = IPRateLimit(count=5, minutes=1, key="upload")
    await limit.add_ip("1.1.1.1")
    stale = await IPLimit.create(
        bucket="upload", ip="2.2.2.2", count=3,
        last_time=await get_now() - datetime.timedelta(minutes=30),
    )
    await limit.remove_expired_ip()
    assert await IPLimit.filter(bucket="upload", ip="2.2.2.2").exists() is False
    assert await IPLimit.filter(bucket="upload", ip="1.1.1.1").exists() is True


async def test_no_key_limits_are_noop(tortoise_db):
    """未配置 key 的限流器为 noop（防止误配导致全部拦截）。"""
    limit = IPRateLimit(count=2, minutes=1, key="")
    assert await limit.check_ip("x") is True
    assert await limit.add_ip("x") == 0
