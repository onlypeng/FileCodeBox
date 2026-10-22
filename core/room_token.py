"""房间成员令牌：让"我是本房间成员"这件事在断线后依然可证明。

背景：房间缓存的上传/删除接口要求调用方是本房间成员。最早的实现只认
"client_id 当前保持 WebSocket 连接"，于是 WS 一断（切后台、弱网重连）
用户就无法管理自己刚上传的文件。

令牌在 WS 握手（welcome）时签发，短期有效；HTTP 接口改为
"令牌有效 或 当前在线" 二选一即可放行：
- 仍具备原有的安全性（令牌无法伪造，未加入房间者拿不到）
- 解除了"必须在线"的体验约束

注意：令牌不是长期身份。过期后需要重新加入房间获取，避免泄露后被无限期使用。
"""
import hmac
import time

from core.room_cache import app_secret

# 成员令牌默认有效期（小时）。房间本身是临时的，无需长期令牌。
MEMBER_TOKEN_TTL = 2 * 3600


def issue_member_token(room_code: str, client_id: str, ttl: int = MEMBER_TOKEN_TTL) -> str:
    """签发成员令牌，格式 `<exp>:<sig>`。"""
    exp = int(time.time()) + max(60, int(ttl))
    return f"{exp}:{_sign(room_code, client_id, exp)}"


def _sign(room_code: str, client_id: str, exp: int) -> str:
    raw = f"member:{room_code}:{client_id}:{exp}".encode()
    return hmac.new(app_secret().encode(), raw, "sha256").hexdigest()[:32]


def verify_member_token(room_code: str, client_id: str, token: str) -> bool:
    """校验成员令牌：房间码与 client_id 均需匹配，且未过期。"""
    if not (room_code and client_id and token):
        return False
    parts = (token or "").split(":")
    if len(parts) != 2:
        return False
    exp_s, sig = parts
    try:
        exp = int(exp_s)
    except (TypeError, ValueError):
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_sign(room_code, client_id, exp), sig)
