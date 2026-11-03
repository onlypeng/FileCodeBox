"""管理后台的鉴权依赖与 JWT 实现。

## 问题
后台接口全部挂在同一个 `APIRouter` 上（见 apps/admin/views.py），
如果鉴权只靠"路由注册时记得加 Depends"，漏一个就是一个未授权访问漏洞，
而且这种漏洞在功能测试里不会暴露（正常请求全都带着 Token）。

## 方案
鉴权**提升为 router 级依赖**（`dependencies=[Depends(admin_required)]`），
新增路由自动继承；登录接口是唯一例外，用 `ADMIN_PUBLIC_ENDPOINTS` 显式放行
（白名单而非黑名单：新增接口默认受保护）。

## 校验顺序与失败语义
`_require_admin_payload` 严格按三步走，任何一步失败都抛 **401** 且
**不带具体原因**（统一文案"未授权或授权校验失败"）：
1. Authorization 头必须是 `Bearer <token>`（缺失/格式错 → 401）
2. 签名与有效期校验（`verify_token` 抛出 ValueError → 401）
3. payload 必须带 `is_admin=True`（防止拿着别的用途签发的 Token 混进来）

刻意不区分 401/403、也不区分"过期"与"签名错"：精确的失败原因
等于给攻击者一个可用的探测信号（能分辨出"Token 格式对但签名错"）。

## 刻意不做
**不实现 Token 撤销/会话表**。Token 是无状态 JWT，改密码或退出后旧 Token 在
过期前仍然有效；接受这个代价是因为后台是单管理员场景，
引入会话表带来的是持久化与多实例的复杂度。真要立即失效，
只能轮换 `JWT_SECRET`（环境变量）——这也正是推荐生产环境配置它的原因：
否则密钥回退到 `admin_token`，改密码会连带把所有人踢下线。
"""
# @Time    : 2023/8/15 17:43
# @Author  : Lan
# @File    : depends.py
# @Software: PyCharm
from fastapi import Header, HTTPException
from fastapi.requests import Request
import base64
import hmac
import json
import os
import secrets
import time
from apps.admin.services import FileService, ConfigService
from core.settings import settings


def _get_jwt_secret() -> str:
    """
    获取 JWT 签名密钥。
    优先使用环境变量 JWT_SECRET；未配置时回退到 admin_token（向后兼容，但生产环境必须配置）。
    注意：配置 JWT_SECRET 后，修改管理员密码不再导致已签发 Token 失效。
    """
    env_secret = os.getenv("JWT_SECRET")
    if env_secret:
        return env_secret
    return settings.admin_token


def create_token(data: dict, expires_in: int = 3600 * 24 * 30) -> str:
    """签发 JWT（HS256，标准 base64url）。

    默认有效期 30 天：后台是低频操作，短有效期只会逼管理员反复登录。
    payload 里的 `jti` 仅为唯一性而生成，**服务端不记录**，因此它目前不参与
    任何撤销判断——不要误以为能用它做黑名单。

    :param data: 数据负载（后台签发时固定带 `is_admin=True`）
    :param expires_in: 过期时间(秒)
    """
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode()
    ).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({
            **data,
            "iat": int(time.time()),
            "jti": secrets.token_hex(8),
            "exp": int(time.time()) + expires_in,
        }).encode()
    ).decode().rstrip("=")

    signature = hmac.new(
        _get_jwt_secret().encode(), f"{header}.{payload}".encode(), "sha256"
    ).digest()
    signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")

    return f"{header}.{payload}.{signature}"


def verify_token(token: str) -> dict:
    """校验并解码 JWT，返回 payload。

    失败语义：**统一抛 ValueError**，不区分签名错/过期/格式错，
    由上层转成同一个 401 文案（避免给攻击者提供探测信号）。

    是否管理员**不由本函数判断**：它只保证 Token 是真的、没过期，
    `is_admin` 的断言留给 `_require_admin_payload`，让"验签"与"授权"分离。

    :param token: JWT token
    :return: 解码后的数据
    """
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")

        # 兼容老版 base64 和标准 base64url
        def _b64decode_urlsafe(s: str) -> bytes:
            # 先补齐 padding
            pad = 4 - len(s) % 4
            if pad != 4:
                s += "=" * pad
            try:
                return base64.urlsafe_b64decode(s)
            except Exception:
                return base64.b64decode(s)

        # 验证签名
        expected_signature = hmac.new(
            _get_jwt_secret().encode(),
            f"{header_b64}.{payload_b64}".encode(),
            "sha256",
        ).digest()
        expected_sig_str = base64.urlsafe_b64encode(expected_signature).decode().rstrip("=")

        if not hmac.compare_digest(signature_b64, expected_sig_str):
            raise ValueError("无效的签名")

        # 解码payload
        payload = json.loads(_b64decode_urlsafe(payload_b64))

        # 检查是否过期
        if payload.get("exp", 0) < time.time():
            raise ValueError("token已过期")

        return payload
    except Exception as e:
        raise ValueError(f"token验证失败: {str(e)}")


def _extract_bearer_token(authorization: str) -> str:
    """从 Authorization 头取出 Token（缺失或不是 Bearer 时抛 401）。

    用 `split(" ", 1)` 而不是 `[1]`：Token 本身不会含空格，但多余空格会导致
    静默取到空串，这里显式判空更稳妥。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未授权或授权校验失败")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="未授权或授权校验失败")
    return token


def _require_admin_payload(authorization: str) -> dict:
    """校验管理员身份并返回 JWT payload；任一环节失败抛 401（详情见模块头）。

    前提：仅用于后台接口。分享侧的匿名接口走 `share_required_login`，
    那里在关闭游客上传时**返回 403 而非 401**，语义是"需要先登录"而不是"凭证无效"。
    """
    token = _extract_bearer_token(authorization)
    try:
        payload = verify_token(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="未授权或授权校验失败")
    if not payload.get("is_admin", False):
        raise HTTPException(status_code=401, detail="未授权或授权校验失败")
    return payload


# 白名单：只有登录接口可匿名访问。写死 (方法, 路径) 精确匹配而非前缀匹配，
# 避免将来新增 /admin/login/xxx 之类的路径被顺带放行。
# 注意这里比对的是 request.url.path，若部署在子路径下需带上前缀。
ADMIN_PUBLIC_ENDPOINTS = {("POST", "/admin/login")}


async def admin_required(
    authorization: str = Header(default=None), request: Request = None
):
    """router 级管理员鉴权依赖：命中白名单返回 None，否则校验 Token。

    返回值本身无意义（FastAPI 忽略），关键是**失败即抛 401 中断请求**。
    `request` 为 None 时（直接函数调用等非 HTTP 场景）不做白名单豁免，
    走完整校验——宁可拦错，不可放过。
    """
    if request and (request.method, request.url.path) in ADMIN_PUBLIC_ENDPOINTS:
        return None
    return _require_admin_payload(authorization)


async def share_required_login(authorization: str = Header(default=None)):
    """分享上传的准入依赖：关闭游客上传时，只有管理员能传。

    - `openUpload=True`：直接放行（返回值 True 恒真，调用方无需判断）
    - `openUpload=False`：缺 Token 抛 **403**（"请先登录后台"，是可恢复的操作指引），
      有 Token 但非管理员抛 **401**（凭证无效）

    刻意用 403 而不是 401 表达"本站未开启游客上传"：401 会诱导客户端弹出
    登录框并重试，而这里重试也没用——是站点策略不允许，不是凭证缺失。

    :param authorization: 认证头信息
    :return: 恒为 True；失败以抛 HTTPException 的方式中断
    """
    if not settings.openUpload:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=403, detail="本站未开启游客上传，如需上传请先登录后台"
            )
        _require_admin_payload(authorization)

    return True


async def get_file_service():
    """注入 FileService。

    每次请求新建实例而不是复用单例：实例持有的是**当前**存储后端
    （`storages[settings.file_storage]`），管理员在后台切换存储后，
    下一个请求必须拿到新的后端，复用单例会继续往旧后端写。
    """
    return FileService()


async def get_config_service():
    """注入 ConfigService（无状态，实例化的唯一目的是让视图不直接依赖类）。"""
    return ConfigService()
