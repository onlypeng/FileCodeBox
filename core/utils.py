# @Time    : 2023/8/13 19:54
# @Author  : Lan
# @File    : utils.py
# @Software: PyCharm
import datetime
import hashlib
import hmac as hmac_module
import logging
import os
import re
import secrets
import string
import time

from core.settings import settings

logger = logging.getLogger(__name__)


async def get_random_num():
    """
    获取6位随机数
    :return:
    """
    return secrets.randbelow(900000) + 100000


r_s = string.ascii_uppercase + string.digits


async def get_random_string():
    """
    获取随机字符串（口令/码位数由后台配置 codeLength 决定，默认 6 位）
    :return:
    """
    length = int(getattr(settings, "codeLength", 6) or 6)
    length = max(4, min(length, 12))
    return "".join(secrets.choice(r_s) for _ in range(length))


async def get_now():
    """
    获取当前时间
    :return:
    """
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


# 下载 token 有效期（秒）：取件后分享链接在窗口内可下载；
# count（按次）模式靠 select 扣次数约束，超窗后需重新取件（重新扣次数）
SELECT_TOKEN_TTL = 24 * 3600


async def get_select_token(code: str):
    """获取下载token，使用随机盐增加不可预测性，并编码签发时间限制有效期"""
    token = settings.admin_token
    salt = secrets.token_hex(8)
    issued_at = int(time.time())
    hash_value = hashlib.sha256(
        f"{code}{salt}{token}{issued_at}".encode()
    ).hexdigest()
    return f"{hash_value}.{salt}.{issued_at}"


async def verify_select_token(key: str, code: str) -> bool:
    """验证下载token（签名 + 有效期；兼容无时间戳的旧格式）"""
    token = settings.admin_token
    if "." not in key:
        return False
    parts = key.split(".")
    if len(parts) == 3:
        hash_value, salt, issued_at = parts
        try:
            issued = int(issued_at)
        except ValueError:
            return False
        if time.time() - issued > SELECT_TOKEN_TTL:
            return False
        expected = hashlib.sha256(
            f"{code}{salt}{token}{issued_at}".encode()
        ).hexdigest()
        return hmac_module.compare_digest(hash_value, expected)
    if len(parts) == 2:
        # 旧格式（无时间戳）：仅校验签名（存量链接）
        hash_value, salt = parts
        expected = hashlib.sha256(f"{code}{salt}{token}".encode()).hexdigest()
        return hmac_module.compare_digest(hash_value, expected)
    return False


async def get_file_url(code: str):
    """
    对于需要通过服务器中转下载的服务，获取文件下载地址
    :param code:
    :return:
    """
    return f"/share/download?key={await get_select_token(code)}&code={code}"


def hash_password(password: str) -> str:
    """
    使用 PBKDF2 + SHA256 + salt 哈希密码
    返回格式: pbkdf2$<iterations>$<salt>$<hash>
    """
    salt = os.urandom(16).hex()
    iterations = 100000
    password_hash = hashlib.pbkdf2_hmac(
        'sha256', password.encode(), salt.encode(), iterations
    ).hex()
    return f"pbkdf2${iterations}${salt}${password_hash}"


def verify_password(password: str, hashed: str) -> bool:
    """
    验证密码是否匹配
    支持新格式 (pbkdf2$iterations$salt$hash) 和旧格式 (sha256$salt$hash) 和明文
    """
    # 非字符串/空值一律视为不匹配（防止 startswith 等操作对畸形存储值抛异常导致登录 500）
    if not isinstance(hashed, str) or not hashed:
        return False

    # 新格式: pbkdf2$iterations$salt$hash
    if hashed.startswith("pbkdf2$"):
        parts = hashed.split("$")
        if len(parts) != 4:
            return False
        _, iterations_str, salt, stored_hash = parts
        try:
            iterations = int(iterations_str)
        except ValueError:
            # 畸形哈希（如迭代次数非数字）：视为不匹配，避免登录 500
            return False
        password_hash = hashlib.pbkdf2_hmac(
            'sha256', password.encode(), salt.encode(), iterations
        ).hex()
        return hmac_module.compare_digest(password_hash, stored_hash)

    # 旧格式: sha256$salt$hash (兼容迁移)
    if hashed.startswith("sha256$"):
        parts = hashed.split("$")
        if len(parts) != 3:
            return False
        _, salt, stored_hash = parts
        password_hash = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
        return hmac_module.compare_digest(password_hash, stored_hash)

    # 明文兼容（仅用于迁移，验证后自动升级）
    return password == hashed


def is_password_hashed(password: str) -> bool:
    """
    检查密码是否已经是哈希格式
    """
    return (password.startswith("pbkdf2$") and len(password.split("$")) == 4) or \
           (password.startswith("sha256$") and len(password.split("$")) == 3)


def pagination(page: int, size: int, max_size: int = 100) -> tuple[int, int]:
    """
    计算分页偏移（多接口共用，避免各处重复 (page-1)*size）。
    钳制非法输入：page/size 最小为 1，size 不超过 max_size。
    """
    page = max(1, int(page))
    size = min(max(1, int(size)), max_size)
    return page, (page - 1) * size


async def sanitize_filename(filename: str) -> str:
    """
    安全处理文件名：
    1. 剥离路径只保留文件名
    2. 替换非法字符
    3. 处理空文件名情况
    """
    filename = os.path.basename(filename)
    illegal_chars = r'[\\/*?:"<>|\x00-\x1F]'  # 包含控制字符
    # 替换非法字符为下划线
    cleaned = re.sub(illegal_chars, "_", filename)
    # 处理空格（可选替换为_）
    cleaned = cleaned.replace(" ", "_")
    # 处理连续下划线
    cleaned = re.sub(r"_+", "_", cleaned)
    # 处理首尾特殊字符
    cleaned = cleaned.strip("._")
    # 处理空文件名情况
    if not cleaned:
        cleaned = "unnamed_file"
    # 长度限制（按需调整）
    return cleaned[:255]


# 敏感字段列表
SENSITIVE_KEYS = {
    "s3_secret_access_key", "onedrive_password", "webdav_password",
    "aws_session_token", "s3_access_key_id",
}

# 新加密格式前缀（Fernet/AES-GCM）
_ENCRYPT_PREFIX = "fe:"
# 旧加密格式前缀（自研 XOR 流加密，仅用于兼容解密旧数据）
_LEGACY_PREFIX = "enc:"


def _get_encryption_key() -> bytes:
    """
    获取加密密钥（32 字节 urlsafe base64，可直接用于 Fernet）
    优先使用环境变量 ENCRYPTION_KEY，未配置时回退到 admin_token 派生（保持向后兼容）
    """
    import base64 as b64

    env_key = os.getenv("ENCRYPTION_KEY")
    if env_key:
        key_material = env_key.encode("utf-8")
    else:
        key_material = f"FileCodeBox_Encryption_{settings.admin_token}".encode()
    return b64.urlsafe_b64encode(hashlib.sha256(key_material).digest())


def _get_legacy_encryption_key() -> bytes:
    """旧版 XOR 加密的密钥派生（固定从 admin_token 派生，用于解密存量数据）"""
    key_material = hashlib.sha256(f"FileCodeBox_Encryption_{settings.admin_token}".encode()).digest()
    return key_material


def encrypt_value(value: str) -> str:
    """使用 Fernet（AES-128-CBC + HMAC）加密敏感值"""
    if not value:
        return value
    from cryptography.fernet import Fernet

    f = Fernet(_get_encryption_key())
    token = f.encrypt(value.encode("utf-8"))
    return f"{_ENCRYPT_PREFIX}{token.decode()}"


def decrypt_value(value: str) -> str:
    """解密敏感值（兼容新 Fernet 格式与旧 XOR 格式）"""
    if not value or not (value.startswith(_ENCRYPT_PREFIX) or value.startswith(_LEGACY_PREFIX)):
        return value

    if value.startswith(_ENCRYPT_PREFIX):
        try:
            from cryptography.fernet import Fernet

            f = Fernet(_get_encryption_key())
            return f.decrypt(value[len(_ENCRYPT_PREFIX):].encode()).decode("utf-8")
        except Exception as e:
            # 密钥变更或数据损坏时返回原文避免崩溃，但必须告警以便排查
            logger.warning("敏感配置解密失败（可能修改了管理员密码而未配置 ENCRYPTION_KEY）: %s", e)
            return value

    # 旧格式兼容解密（XOR 流加密）
    import base64 as b64

    key = _get_legacy_encryption_key()
    try:
        raw = b64.b64decode(value[len(_LEGACY_PREFIX):])
        if len(raw) < 17:  # 至少包含 16 字节 salt + 1 字节数据
            return value
        salt = raw[:16]
        encrypted = raw[16:]
        actual_key = hashlib.pbkdf2_hmac('sha256', key, salt, 1000, dklen=len(encrypted))
        decrypted = bytes(a ^ b for a, b in zip(encrypted, actual_key))
        return decrypted.decode('utf-8')
    except Exception:
        return value


def is_encrypted(value: str) -> bool:
    """检查值是否已加密"""
    return isinstance(value, str) and (
        value.startswith(_ENCRYPT_PREFIX) or value.startswith(_LEGACY_PREFIX)
    )
