"""通用工具：随机码、下载签名 token、口令哈希、文件/名称清洗、敏感值加解密。

这里既有"到处都要用到的小函数"，也有两处**容易被轻视的安全相关实现**，说明如下。

## 下载 token（`get_select_token` / `verify_select_token`）

取件成功后前端拿到一个直达下载链接。早期版本只用 admin_token 做 HMAC，
结果链接永久有效且可被无限制转发。现在 token 里带上签发时间戳并限 24 小时，
同时保留对旧两段的格式的兼容——网上存量分享链接必须还能打开。

## 敏感配置加解密（`encrypt_value` / `decrypt_value`）

对象存储密钥、网盘密码入库时不存明文。这里有两代实现并存：
- 新格式 `fe:`：Fernet（AES-128-CBC + HMAC），有认证，抗篡改
- 旧格式 `enc:`：自研 XOR 流加密，**没有完整性校验**，仅保留解密路径
  （删除会让老实例的凭据变成不可解，无法自愈）

密钥派生两条路径：`ENCRYPTION_KEY` 环境变量优先，缺失时退回 admin_token 派生。
后者是个妥协——它让"改管理密码"与"改加密密钥"耦合在一起，因此解密失败时只告警、
返回原值，不阻断启动。

## 刻意不做

不引入 passlib / bcrypt：`hash_password` 用 pbkdf2_hmac(sha256, 10万次) 足够，
且无需额外依赖与原生库编译，跨平台（尤其 Windows）安装体验更重要。
如果要提高强度，应当把迭代次数做成可配置项，而不是换算法（换算法同样要保留
旧格式的验证分支，见 `verify_password` 对三种格式的支持）。
"""
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
    """纯数字随机串，恒 6 位（100000~999999）。

    与 `get_random_string` 不同：**不受 codeLength 影响**。
    注意：当前仓库内暂无调用点，属保留工具函数。
    """
    return secrets.randbelow(900000) + 100000


r_s = string.ascii_uppercase + string.digits


async def get_random_string():
    """随机分享/投递码（大写字母 + 数字）。

    长度由后台 `codeLength` 决定并**钳制到 4~12**：口令太短易被暴力枚举、
    太长则用户输错率高，这个区间是可用性经验值。
    刻意排除易混字符（0/O、1/I 之类的冲突靠只用大写 + 数字已缓解，不再引入
    更多字符集：取件码需要在手机上转录）。
    """
    length = int(getattr(settings, "codeLength", 6) or 6)
    length = max(4, min(length, 12))
    return "".join(secrets.choice(r_s) for _ in range(length))


async def get_now():
    """当前时间（UTC+8，带时区）。

    全项目时间基准都走这里，不用 `datetime.now()`：
    - Tortoise 连接配置 `use_tz=False`，存进去的是本地时间，比较时必须同基准
    - 固定 Asia/Shanghai 而不取系统时区：容器默认 UTC，跟随主机会让过期判定
      在部署到不同时区的机器时漂移 8 小时（表现为"文件提前/迟迟不过期"）
    """
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


# 下载 token 有效期（秒）：取件后分享链接在窗口内可下载；
# count（按次）模式靠 select 扣次数约束，超窗后需重新取件（重新扣次数）
SELECT_TOKEN_TTL = 24 * 3600


async def get_select_token(code: str):
    """签发下载 token：`hash.salt.issued_at`。

    用随机盐而非纯服务端密钥参与哈希 —— 同一取件码每次签发结果不同，
    无法通过观察两个链接推断出它们是同一个码，也避免预计算彩虹表。
    admin_token 参与签名意味着**改管理密码会让所有已下发的链接立即失效**，
    这是刻意的：重置口令本就意味着旧凭据全部作废。
    """
    token = settings.admin_token
    salt = secrets.token_hex(8)
    issued_at = int(time.time())
    hash_value = hashlib.sha256(
        f"{code}{salt}{token}{issued_at}".encode()
    ).hexdigest()
    return f"{hash_value}.{salt}.{issued_at}"


async def verify_select_token(key: str, code: str) -> bool:
    """验证下载 token（签名 + 有效期；兼容无时间戳的旧格式）。

    返回 False 而不是抛异常：调用方（下载路由）统一按"链接无效"处理，
    不需要区分是过期还是被篡改 —— 区分了只会给攻击者更多提示。
    旧格式（两段）**永久有效**，这是为了存量分享链接的兼容，属已知取舍。
    """
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
    """取件后的下载直链（走服务器中转）。

    只有"存储后端给不出可直接访问的公网 URL"时才用这个：本地磁盘、WebDAV、
    OpenDAL、OneDrive 代理模式，以及 S3 开了代理（`s3_proxy=1`）。
    每次调用签一个新 token（见 `SELECT_TOKEN_TTL`），同一个取件码多次结果不同。
    """
    return f"/share/download?key={await get_select_token(code)}&code={code}"


def hash_password(password: str) -> str:
    """口令哈希，返回 `pbkdf2$<iterations>$<salt>$<hash>`。

    把迭代次数写进结果串：将来调高强度不需要迁移存量数据——验证时按串里记录的
    次数算，新旧哈希可以并存，各自验证通过即可。
    """
    salt = os.urandom(16).hex()
    iterations = 100000
    password_hash = hashlib.pbkdf2_hmac(
        'sha256', password.encode(), salt.encode(), iterations
    ).hex()
    return f"pbkdf2${iterations}${salt}${password_hash}"


def verify_password(password: str, hashed: str) -> bool:
    """校验口令，兼容三种存储形态：pbkdf2$... / sha256$... / 明文。

    明文分支是**迁移期遗留**：老版本直接把明文存进了库，这里验证通过即可
    （调用方会在验证成功后用 hash_password 升级存储）。
    时间可比性靠 hmac.compare_digest，避免用 == 比较哈希泄漏时序信息。
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
    """是否为哈希形态（用于"验证通过后自动把明文升级为哈希"的判断）。

    只看格式前缀与段数，不校验内容；畸形串返回 False，
    于是会被当成明文处理（后者同样验证失败），不会出现误判为已哈希而跳过升级。
    """
    return (password.startswith("pbkdf2$") and len(password.split("$")) == 4) or \
           (password.startswith("sha256$") and len(password.split("$")) == 3)


def pagination(page: int, size: int, max_size: int = 100) -> tuple[int, int, int]:
    """
    计算分页参数（多接口共用，避免各处重复 (page-1)*size）。

    返回 `(page, size, offset)`，**size 是钳制后的值**：调用方必须用返回的 size
    去 `.limit()`，否则钳制只作用于 offset，`size=10000` 仍会一次性拖出上万行，
    且 page/offset 与 limit 口径不一致会造成分页重叠/漏项。
    钳制非法输入：page/size 最小为 1，size 不超过 max_size。
    """
    page = max(1, int(page))
    size = min(max(1, int(size)), max_size)
    return page, size, (page - 1) * size


async def sanitize_filename(filename: str) -> str:
    """把文件名压成一个安全的裸名（不含目录）。

    这里是**最后一道防线**：所有写入磁盘/对象存储的 key 都要过一遍。
    注意它同时做"去目录"（basename）与"去非法字符"，并且最终结果可能是
    `unnamed_file` —— 不要拿返回值去推断原文件名。

    返回空的可能性已排除（退化成 `unnamed_file`），长度上限 255 对齐常见
    文件系统 NAME_MAX。
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
    """新格式密钥（32 字节 urlsafe base64，可直接喂给 Fernet）。

    环境变量优先；缺失时回落到 admin_token 派生（保持向后兼容）。
    后者意味着**改管理密码会让已加密的凭据不可解**，此时 decrypt_value 只告警
    不抛——生产环境应显式配 ENCRYPTION_KEY 并作为密钥轮换的唯一入口。
    """
    import base64 as b64

    env_key = os.getenv("ENCRYPTION_KEY")
    if env_key:
        key_material = env_key.encode("utf-8")
    else:
        key_material = f"FileCodeBox_Encryption_{settings.admin_token}".encode()
    return b64.urlsafe_b64encode(hashlib.sha256(key_material).digest())


def _get_legacy_encryption_key() -> bytes:
    """旧版 XOR 加密的密钥派生（固定从 admin_token 派生，用于解密存量数据）。

    刻意固定用 admin_token、不走环境变量：新密钥体系的引入不能改变老数据的
    解密结果，否则老实例升级后凭据直接丢失。
    """
    key_material = hashlib.sha256(f"FileCodeBox_Encryption_{settings.admin_token}".encode()).digest()
    return key_material


def encrypt_value(value: str) -> str:
    """加密敏感配置值（新格式 `fe:`）。

    空值原样返回（不加前缀）——否则"没配"和"配了空串"在库里会变成两种形态，
    读取侧的判断要跟着分叉。 cryptography 延迟导入：单实例部署不配对象存储时
    不必为此付出 import 成本。
    """
    if not value:
        return value
    from cryptography.fernet import Fernet

    f = Fernet(_get_encryption_key())
    token = f.encrypt(value.encode("utf-8"))
    return f"{_ENCRYPT_PREFIX}{token.decode()}"


def decrypt_value(value: str) -> str:
    """解密敏感值（新 `fe:` 与旧 `enc:` 兼容）；无前缀视为明文原样返回。

    **失败时返回输入原值而不是抛异常**：init/refresh_settings 流程会因为一个
    坏凭据把整个启动带崩，这是不可接受的。代价是调用方拿到"看起来像明文"的
    密钥去连 S3 会失败——已用 warning 日志明确指向"改过管理员密码或未配
    ENCRYPTION_KEY"。
    """
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
    """是否已加密（决定是否跳过二次加密）。

    必须同时认两种前缀：只认新的话，老数据会在每次保存时被嵌套加密。
    """
    return isinstance(value, str) and (
        value.startswith(_ENCRYPT_PREFIX) or value.startswith(_LEGACY_PREFIX)
    )
