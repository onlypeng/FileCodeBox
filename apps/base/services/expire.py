"""过期配置校验服务 — 统一处理所有接口中的过期时间校验逻辑"""
import datetime
from datetime import timedelta
from typing import Optional

from fastapi import HTTPException

from core.settings import settings


def expire_to_seconds(expire_style: str, expire_value: int) -> int:
    """将过期时间转换为秒数（count 模式返回 0，不限制时间）"""
    if expire_style == "day":
        return expire_value * 86400
    elif expire_style == "hour":
        return expire_value * 3600
    elif expire_style == "minute":
        return expire_value * 60
    elif expire_style == "count":
        return 0  # count 模式仅限制使用次数，不限制时间
    return 0


def calculate_expired_at(
    expire_style: str, expire_value: int, base_time: datetime.datetime
) -> Optional[datetime.datetime]:
    """统一计算过期时间（count 模式给 365 天，forever 返回 None）"""
    if expire_style == "forever":
        return None
    delta_map = {
        "day": timedelta(days=1),
        "hour": timedelta(hours=1),
        "minute": timedelta(minutes=1),
        "count": timedelta(days=365),  # count 模式给较长过期时间
    }
    delta = delta_map.get(expire_style, timedelta(days=7))
    return base_time + delta * expire_value


def validate_expire_config(
    expire_style: str,
    expire_value: int,
    label: str = "过期时间",
    max_save_seconds: Optional[int] = None,
) -> None:
    """
    统一校验过期配置：
    1. 过期类型是否在允许列表中
    2. 过期时间是否超过系统最大保存时间

    Args:
        expire_style: 过期类型 (day/hour/minute/count/forever)
        expire_value: 过期数值
        label: 用于错误提示的标签
        max_save_seconds: 系统最大保存秒数，默认从 settings 读取
    """
    if expire_style not in settings.expireStyle:
        raise HTTPException(400, f"{label}类型错误")

    if max_save_seconds is None:
        max_save_seconds = getattr(settings, "max_save_seconds", 0)

    if max_save_seconds > 0 and expire_style != "forever":
        expire_seconds = expire_to_seconds(expire_style, expire_value)
        if expire_seconds > max_save_seconds:
            max_days = max_save_seconds // 86400
            raise HTTPException(400, f"{label}超过系统限制，最长{max_days}天")


def validate_expire_not_exceed_collection(
    child_style: str,
    child_value: int,
    collection_style: str,
    collection_value: int,
    child_label: str = "子码",
) -> None:
    """
    校验投件码/取件码过期时间不超过收件箱过期时间
    """
    if collection_style == "forever":
        return  # 收件箱永久，子码无限制
    if child_style == "forever":
        raise HTTPException(400, f"{child_label}不能设为永久，收件箱将在{expire_to_seconds(collection_style, collection_value) // 86400}天后过期")
    child_seconds = expire_to_seconds(child_style, child_value)
    collection_seconds = expire_to_seconds(collection_style, collection_value)
    if child_seconds > collection_seconds:
        raise HTTPException(400, f"{child_label}过期时间不能超过收件箱过期时间")