# @Time    : 2023/8/13 19:54
# @Author  : Lan
# @File    : utils.py
# @Software: PyCharm
import datetime
import hashlib
import hmac as hmac_module
import os
import random
import re
import secrets
import string
import time

from core.settings import settings


async def get_random_num():
    """
    获取6位随机数
    :return:
    """
    return secrets.randbelow(900000) + 100000


r_s = string.ascii_uppercase + string.digits


async def get_random_string():
    """
    获取随机字符串
    :return:
    """
    return "".join(secrets.choice(r_s) for _ in range(6))


async def get_now():
    """
    获取当前时间
    :return:
    """
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


async def get_select_token(code: str):
    """获取下载token，使用随机盐增加不可预测性"""
    token = settings.admin_token
    salt = secrets.token_hex(8)
    hash_value = hashlib.sha256(f"{code}{salt}{token}".encode()).hexdigest()
    return f"{hash_value}.{salt}"


async def verify_select_token(key: str, code: str) -> bool:
    """验证下载token"""
    token = settings.admin_token
    if "." in key:
        hash_value, salt = key.rsplit(".", 1)
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


async def max_save_times_desc(max_save_seconds: int):
    """
    获取最大保存时间的描述
    :param max_save_seconds:
    :return:
    """

    def gen_desc_zh(value: int, desc: str):
        if value > 0:
            return f"{value}{desc}"
        else:
            return ""

    def gen_desc_en(value: int, desc: str):
        if value > 0:
            ret = f"{value} {desc}"
            if value > 1:
                ret += "s"
            ret += " "
            return ret
        else:
            return ""

    max_timedelta = datetime.timedelta(seconds=max_save_seconds)
    desc_zh, desc_en = "最长保存时间：", "Max save time: "
    desc_zh += gen_desc_zh(max_timedelta.days, "天")
    desc_en += gen_desc_en(max_timedelta.days, "day")
    desc_zh += gen_desc_zh(max_timedelta.seconds // 3600, "小时")
    desc_en += gen_desc_en(max_timedelta.seconds // 3600, "hour")
    desc_zh += gen_desc_zh(max_timedelta.seconds % 3600 // 60, "分钟")
    desc_en += gen_desc_en(max_timedelta.seconds % 3600 // 60, "minute")
    desc_zh += gen_desc_zh(max_timedelta.seconds % 60, "秒")
    desc_en += gen_desc_en(max_timedelta.seconds % 60, "second")
    return desc_zh, desc_en


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
    if not hashed:
        return False

    # 新格式: pbkdf2$iterations$salt$hash
    if hashed.startswith("pbkdf2$"):
        parts = hashed.split("$")
        if len(parts) != 4:
            return False
        _, iterations_str, salt, stored_hash = parts
        iterations = int(iterations_str)
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


def _get_encryption_key() -> bytes:
    """从 admin_token 派生加密密钥"""
    # 使用固定盐值从 admin_token 派生密钥
    key_material = hashlib.sha256(f"FileCodeBox_Encryption_{settings.admin_token}".encode()).digest()
    return key_material


def encrypt_value(value: str) -> str:
    """加密敏感值，返回 base64 编码的加密数据（含随机盐）"""
    if not value:
        return value
    import base64 as b64
    key = _get_encryption_key()
    # 使用随机盐避免相同明文产生相同密文
    salt = secrets.token_bytes(16)
    value_bytes = value.encode('utf-8')
    # 将 salt 和 key 混合作为实际加密密钥
    actual_key = hashlib.pbkdf2_hmac('sha256', key, salt, 1000, dklen=len(value_bytes))
    encrypted = bytes(a ^ b for a, b in zip(value_bytes, actual_key))
    return f"enc:{b64.b64encode(salt + encrypted).decode()}"


def decrypt_value(value: str) -> str:
    """解密敏感值"""
    if not value or not value.startswith("enc:"):
        return value
    import base64 as b64
    key = _get_encryption_key()
    raw = b64.b64decode(value[4:])
    if len(raw) < 17:  # 至少包含 16 字节 salt + 1 字节数据
        return value
    salt = raw[:16]
    encrypted = raw[16:]
    actual_key = hashlib.pbkdf2_hmac('sha256', key, salt, 1000, dklen=len(encrypted))
    decrypted = bytes(a ^ b for a, b in zip(encrypted, actual_key))
    return decrypted.decode('utf-8')


def is_encrypted(value: str) -> bool:
    """检查值是否已加密"""
    return isinstance(value, str) and value.startswith("enc:")
