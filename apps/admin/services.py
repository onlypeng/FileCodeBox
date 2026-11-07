"""后台业务服务层：文件管理与配置读写。

## 问题
后台视图要同时处理"删记录"和"删文件"，而这两者**不是一回事**：
秒传/去重会让多条 FileCodes 指向同一份物理文件，直接删文件会让别的取件码
变成死链（表现为下载 404，且极难归因）。配置侧同理：管理员在表单里看到的
敏感字段是 `******`，原样保存就会把星号写进数据库、把真凭据冲掉。

## 方案
- 删除前一律先查引用（`core.file_dedup.is_file_referenced_elsewhere`），
  仍被引用的只删记录不删文件。
- 配置更新把"脱敏占位符"识别为一等公民：`******` 或空串表示**不修改**，
  从库里继承原值；真实值才走加密。

## 刻意不做
**不在服务层做权限判断**。调用方（后台路由）已由 router 级 `admin_required`
统一鉴权，服务层重复校验会让"服务被其它入口复用时"产生虚假的安全感——
真正的边界在路由依赖上。

## 前提
`KeyValue(key="settings")` 记录由启动流程 `core.config` 用 `get_or_create` 保证存在，
因此本模块用 `update` 而非 upsert；绕过启动流程直接调用会静默丢配置。
"""
from core.storage import FileStorageInterface, storages
from core.settings import settings
from core.config import _sync_ip_limits
from core.file_dedup import delete_physical_files
from core.audit import record
from core.logger import logger
from apps.base.models import FileCodes, KeyValue
from fastapi import HTTPException
from core.utils import hash_password, is_password_hashed


class FileService:
    """文件管理：后台列表与删除。

    实例在构造时绑定当前存储后端，因此**不做单例复用**——管理员切换存储后
    必须让下一个请求拿到新的后端（见 apps/admin/dependencies.get_file_service）。
    """

    def __init__(self):
        self.file_storage: FileStorageInterface = storages[settings.file_storage]()

    async def delete_file(self, file_id: int):
        """删除一条分享记录；物理文件仅在无其它记录引用时一并删除。

        副作用：写审计日志（`delete`）。日志**先于**记录删除写入，
        因为 `target_id` 取的是记录上的取件码，删完就拿不到了。
        目标不存在时抛 404。
        """
        file_code = await FileCodes.filter(id=file_id).first()
        if not file_code:
            raise HTTPException(status_code=404, detail="文件不存在")
        # 物理文件删除统一收口：单文件按自身路径、多文件按 FileItem 逐项删除，
        # 每个文件删前都查引用（秒传共享）。多文件记录 uuid_file_name 恒为 None，
        # 旧路径判断会让物理文件永远留在磁盘上。
        await delete_physical_files(self.file_storage, file_code, logger)
        await record("delete", target_type="filecode", target_id=str(file_code.code),
                     result="success", detail="后台删除文件", actor="admin")
        await file_code.delete()

    async def list_files(self, page: int, size: int, keyword: str = "", status: str = ""):
        """分页查询分享记录，返回 (记录列表, 总数)。

        边界条件：
        - **排除收件箱投递件**——它们复用 FileCodes 表但不是"文件分享"，
          混进列表会让管理员误删后连带清空别人的收件箱。
        - `status` 允许多选（逗号分隔）。两者都选或不传 = 不过滤；
          只选一个才加过滤条件。非法取值被静默忽略（不影响分页结果）。
        - 多文件分享会预取子项（`file_items`），单文件为 `[]`。
        """
        from core.utils import get_now, pagination
        from tortoise.expressions import Q

        page, offset = pagination(page, size)
        now = await get_now()
        # 排除收件箱文件：用反向关联 JOIN 完成（collection_files 关联存在即排除），
        # 避免每次分页请求全表加载 CollectionFile 的 file_code_id 列表到内存
        query = FileCodes.exclude(collection_files__id__isnull=False).filter(prefix__icontains=keyword)

        # 状态筛选（多选，逗号分隔：active=有效 / expired=已过期；两者都选=全部）
        # 过期条件：时间过期（expired_at < now 且 非次数模式）或 次数用尽（次数模式且剩余 <= 0）
        time_expired = Q(expired_at__lt=now, expired_count__lt=0)
        count_expired = Q(expired_count__lte=0, expired_count__gte=0)
        expired_q = time_expired | count_expired
        statuses = [s for s in status.split(",") if s in ("active", "expired")]
        if statuses and "active" not in statuses:
            query = query.filter(expired_q)          # 仅 expired
        elif statuses and "expired" not in statuses:
            query = query.exclude(expired_q)         # 仅 active
        # active+expired 或空 → 不过滤（全部）

        total = await query.count()
        # prefetch items 消除多文件子项列表的 N+1 查询
        files = await query.limit(size).offset(offset).prefetch_related("items")

        result = []
        for f in files:
            item = {
                "id": f.id,
                "code": f.code,
                "prefix": f.prefix,
                "suffix": f.suffix,
                "size": f.size,
                "remark": f.remark or None,  # 文本分享内容（备注字段）
                "used_count": f.used_count,
                "expired_at": f.expired_at.isoformat() if f.expired_at else None,
                "expired_count": f.expired_count,
                "expire_style": f.expire_style,
                "expire_value": f.expire_value,
                "created_at": f.created_at.isoformat() if f.created_at else None,
                "is_multi_file": f.is_multi_file,
                "file_count": 0,
                "file_items": [],
            }
            # 多文件分享，获取子文件列表（已预取，无需逐条查询）
            if f.is_multi_file:
                items = f.items or []
                item["file_count"] = len(items)
                item["file_items"] = [
                    {
                        "id": i.id,
                        "file_name": i.file_name,
                        "file_size": i.file_size,
                    }
                    for i in items
                ]
            result.append(item)

        return result, total


class ConfigService:
    """系统配置读写：对外脱敏、对内加密、改完热生效。"""

    def get_config(self):
        """导出完整配置，**敏感字段替换为 `******`**。

        脱敏是"读时"而不是"存时"做的：库里存的是密文，只有这里会在出口处掩盖，
        因此新增敏感字段只需加入 `SENSITIVE_KEYS`，无需改存储逻辑。
        """
        from core.utils import SENSITIVE_KEYS
        config = dict(settings.items())
        # 脱敏敏感字段
        for key in SENSITIVE_KEYS:
            if key in config and config[key]:
                config[key] = "******"
        # admin_token 不在 SENSITIVE_KEYS（它是口令哈希、不是可解密凭据），
        # 但哈希同样不应回给管理端配置表单，单独按占位符掩盖。
        # 不能把它塞进 SENSITIVE_KEYS：那会让明文口令在 update_config 里被
        # encrypt_value 当成凭据加密，登录就再也验不过了。
        if config.get("admin_token"):
            config["admin_token"] = "******"
        return config

    async def update_config(self, data: dict):
        """合并写入配置并热生效。

        前提：`data` 已由 `UpdateConfigRequest` 白名单过滤（见 apps/admin/schemas.py），
        **本方法不再做白名单判断**，只做二次校验。直接传入任意 dict 会写入非法键。

        处理顺序是有讲究的，不要调换：
        1. 口令：空/占位符视为"不修改"，明文才哈希（已是哈希值则原样保留，
           避免把哈希当明文再哈希一次导致永远登不进去）
        2. 敏感字段：占位符继承库中旧值 → 明文加密存储
        3. 数值字段：转 int/float，限速与配额类拒绝负数
        4. 落库 + 同步到运行时 `settings` + 刷新 IP 限流实例

        副作用：写库、改进程内 `settings`（**含解密后的明文凭据**）、
        调用 `core.config._sync_ip_limits` 就地更新限流阈值。
        最后一步刻意不重新构造限流实例，避免替换掉被调用方持有的引用。

        安全提示：敏感字段用 Fernet 加密，密钥优先取环境变量 `ENCRYPTION_KEY`；
        未配置时回退到由 `admin_token` 派生——**改管理员口令会让存量密文解不开**，
        故生产环境必须先配 `ENCRYPTION_KEY`。
        """
        from core.utils import hash_password, is_password_hashed, encrypt_value, decrypt_value, is_encrypted, SENSITIVE_KEYS

        # 读取现有配置，用于继承"脱敏占位符"对应的真实凭据
        config_record = await KeyValue.filter(key="settings").first()
        existing_config = config_record.value if config_record and config_record.value else {}

        # 文件数量上限统一口径：maxMultiFileCount 与 maxSendFiles 指的是同一件事，
        # 服务端真正读取的是 maxSendFiles（POST / 也只回传它）。后台表单「保存设置」
        # 是整份配置回传，两个键会同时出现；若值不同必须强制以 maxSendFiles 为准，
        # 否则 config/get 会显示两个不同的数，形成「保存成功但值不一致」的误导。
        if "maxSendFiles" in data:
            data["maxMultiFileCount"] = data["maxSendFiles"]
        elif "maxMultiFileCount" in data:
            data["maxSendFiles"] = data["maxMultiFileCount"]

        # 管理员密码：留空或占位符表示不修改（防止误操作改坏密码）
        admin_token = data.get("admin_token")
        if admin_token is None or str(admin_token).strip() == "" or admin_token == "******":
            data.pop("admin_token", None)
        elif not is_password_hashed(admin_token):
            data["admin_token"] = hash_password(admin_token)

        # 先记录需要在循环后删除的键：迭代过程中直接 pop 会触发
        # RuntimeError: dictionary changed size during iteration。
        drop_keys = []
        for key, value in data.items():
            if key not in settings.default_config:
                continue
            # 跳过脱敏占位符/空值：继承数据库中的真实凭据（空值表示不修改）
            if key in SENSITIVE_KEYS and (value == "******" or value == ""):
                if key in existing_config:
                    data[key] = existing_config[key]
                else:
                    drop_keys.append(key)
                continue
            # 加密敏感字段（跳过已加密值）
            if key in SENSITIVE_KEYS and isinstance(value, str) and value and not is_encrypted(value):
                data[key] = encrypt_value(value)
            if key in [
                "errorCount",
                "errorMinute",
                "maxCollectionFiles",
                "maxSendFiles",
                "maxMultiFileCount",
                "maxTextLength",
                "onedrive_proxy",
                "openUpload",
                "port",
                "s3_proxy",
                "uploadRateLimitCount",
                "uploadMinute",
                "uploadSize",
                "directRelayEnabled",
                "directRelaySpeedLimit",
                "expiredRetentionValue",
                "chunkExpireHours",
            ]:
                try:
                    data[key] = int(value)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"配置项 {key} 必须是整数")
                # 限速/配额/时长类配置不允许负数
                if data[key] < 0 and key in {
                    "errorCount", "errorMinute", "maxCollectionFiles",
                    "maxSendFiles", "maxMultiFileCount", "maxTextLength", "uploadRateLimitCount", "uploadMinute",
                    "uploadSize", "directRelaySpeedLimit", "expiredRetentionValue",
                    "chunkExpireHours",
                }:
                    raise HTTPException(status_code=400, detail=f"配置项 {key} 不能为负数")
                if key == "chunkExpireHours" and data[key] < 1:
                    raise HTTPException(status_code=400, detail="分片会话回收时长不能小于 1 小时")
            elif key in ["opacity"]:
                try:
                    data[key] = float(value)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"配置项 {key} 必须是数字")

        for key in drop_keys:
            data.pop(key, None)

        await KeyValue.filter(key="settings").update(
            value={**existing_config, **{k: v for k, v in data.items() if k in settings.default_config}}
        )
        for k, v in data.items():
            # 敏感字段运行时使用解密后的明文，避免写入密文到内存配置
            if k in SENSITIVE_KEYS and is_encrypted(v):
                settings.__setattr__(k, decrypt_value(v))
            else:
                settings.__setattr__(k, v)
        _sync_ip_limits()
