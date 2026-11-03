"""数据模型层：全系统唯一的「事实来源」，几乎所有业务分支都由这里的字段语义决定。

## 问题

FileCodeBox 同时有单文件分享、多文件分享、文本分享、收件箱、实时房间五种形态，
它们共用一套「取件码 + 过期策略」体系。这些语义早期散落在各视图函数里，
后果是**过期判定出现过好几份互相矛盾的实现**（有只看 `expired_at` 的、
有忽略次数的、有把 `forever` 当成"明天过期"的），线上排查时无法判断哪份是对的。

## 方案

- **过期判定收口到模型方法**：`is_expired` / `is_delivery_expired` /
  `is_retrieve_expired`。视图拒绝取件、后台列表筛选、清理任务必须与这些方法
  保持一致，否则会出现"后台显示有效但取件被拒"这类无法复现的工单。
- **过期用两组字段表达**：`expired_at`（时间型）与 `expired_count`（次数型）。
  约定 **`expired_count >= 0` 即次数模式**，此时 `expired_at` 恒为 NULL、
  无时间上限——这条约定靠 `apps/base/utils.py:get_expire_info` 与模型方法共同保证，
  改任一侧都必须同时改另一侧。
- **删除不留空指针**：收件箱与房间用 `on_delete=CASCADE` 级联，唯独
  `CollectionFile.file_code` 用 `SET_NULL`，因为投递记录要留作审计痕迹。

## 刻意不做

**模型层不负责删除物理文件**。秒传/去重会让多条 `FileCodes` 共享同一份物理文件，
删除前必须查引用（`core.file_dedup.is_file_referenced_elsewhere`）；
这个约束无法在模型删除钩子里可靠表达（也会让 `await obj.delete()` 产生隐式 IO），
因此所有删除点都在服务/视图层显式处理。
"""
# @Time    : 2023/8/13 20:43
# @Author  : Lan
# @File    : models.py
# @Software: PyCharm
from typing import Optional

from tortoise.models import Model
from tortoise.contrib.pydantic import pydantic_model_creator

from tortoise import fields, models
from datetime import datetime
from core.utils import get_now


class FileCodes(models.Model):
    """分享主体：一条记录 = 一个取件码，覆盖单文件 / 多文件 / 文本三种分享。

    文本分享不单独建表，直接复用本表（`size=0`、`remark` 存正文），
    这样取件码、过期策略、取件次数三条链路只有一份实现。
    """

    id = fields.IntField(pk=True)
    code = fields.CharField(max_length=255, unique=True, index=True)
    # 文件名被拆成 prefix/suffix 两段存：下载时拼回原名，列表/搜索只用 prefix。
    # 拆开是为了让"按文件名关键字搜索"和"按后缀统计分布"都能走索引，不必解析全名。
    prefix = fields.CharField(max_length=255, default="")
    suffix = fields.CharField(max_length=255, default="")
    uuid_file_name = fields.CharField(max_length=255, null=True)
    file_path = fields.CharField(max_length=255, null=True)
    size = fields.BigIntField(default=0)
    remark = fields.TextField(null=True)
    expired_at = fields.DatetimeField(null=True)
    # 剩余取件次数。** >= 0 即次数模式**，此时 expired_at 为 NULL、无时间上限；
    # < 0 表示时间模式（day/hour/minute/forever），此时本字段不参与判定。
    expired_count = fields.IntField(default=0)
    used_count = fields.IntField(default=0)
    expire_style = fields.CharField(max_length=20, default="day")
    expire_value = fields.IntField(default=1)
    created_at = fields.DatetimeField(auto_now_add=True)
    # 内容 SHA-256，秒传与去重的判定键。注意它是"去重键"而不是"完整性证明"：
    # 相同哈希的多个取件码可能指向同一份物理文件，删除时必须先查引用。
    file_hash = fields.CharField(max_length=64, null=True)
    is_chunked = fields.BooleanField(default=False)
    # 发起者凭证：上传成功时下发，凭它可提前作废（无账号体系下的最小权限设计）。
    # 只有持有该凭证的人能撤销，仅知道取件码的人不行。
    owner_token = fields.CharField(max_length=64, null=True)
    # 提取口令（二次口令）的 PBKDF2 哈希。空值 = 只需取件码即可取件（存量分享行为不变）。
    # 取件码常在聊天/邮件里明文传输，二次口令作为独立信道，泄露取件码不足以取件。
    access_code_hash = fields.CharField(max_length=200, null=True)
    upload_id = fields.CharField(max_length=36, null=True)
    # 决定取件走哪条分支：True 走 FileItem 子项列表（打包/逐个下载），
    # False 走单文件直出。**本字段与 items 的一致性没有数据库约束**，
    # 判定只看它——置位而 items 为空时按"空多文件分享"处理，不回退到单文件。
    is_multi_file = fields.BooleanField(default=False)

    async def is_expired(self):
        """是否过期——**全局唯一的过期判定口径**，后台筛选与清理任务必须与之一致：
        - 次数型（expired_count >= 0，count/forever 语义）：按剩余次数判断，无时间上限
        - 时间型（expired_count < 0，day/hour/minute）：按 expired_at 判断；None 永不过期
        """
        if self.expired_count >= 0:
            return self.expired_count <= 0
        if self.expired_at is None:
            return False
        return self.expired_at < await get_now()

    async def get_file_path(self):
        """物理文件相对路径（存储后端据此定位对象）。

        约定 `file_path` 是目录、`uuid_file_name` 是目录内文件名，两者都可能为 NULL
        （纯文本分享没有物理文件），调用方需自行判断，本方法不做空值检查。
        """
        return f"{self.file_path}/{self.uuid_file_name}"


class UploadChunk(models.Model):
    """分片上传的中间态：记录每个分片的落盘位置与合并参数。

    为什么要落库而不是只放内存/临时目录：分片上传可能跨请求、跨进程（多 worker），
    合并阶段必须能读到**其它请求写入**的分片信息，否则并发上传必然丢分片。
    记录本身是短命的——合并成功或会话超时后由清理任务回收。
    """

    id = fields.IntField(pk=True)
    upload_id = fields.CharField(max_length=36, index=True)
    chunk_index = fields.IntField()
    chunk_hash = fields.CharField(max_length=64)
    total_chunks = fields.IntField()
    file_size = fields.BigIntField()
    chunk_size = fields.IntField()
    file_name = fields.CharField(max_length=255)
    save_path = fields.CharField(max_length=512, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    # 合并完成标记：合并是异步/可重试的，靠它避免同一 upload_id 被重复合并
    completed = fields.BooleanField(default=False)


class KeyValue(Model):
    """通用键值表：存放「不适合写在配置文件里」的运行时状态。

    当前承载两类数据：
    - `key="settings"`：后台保存的系统配置（覆盖 `DEFAULT_CONFIG`，启动时读回）
    - `key="sys_start"`：进程启动时刻（后台展示运行时长）

    用 JSONField 而不是给每项建表，是因为配置项的增删非常频繁，
    建列意味着每次都要写迁移；代价是**没有字段级类型约束**，
    类型校验全部交给 `apps/admin/services.py:ConfigService.update_config`。
    """

    id: Optional[int] = fields.IntField(pk=True)
    key: Optional[str] = fields.CharField(
        max_length=255, description="键", index=True, unique=True
    )
    value: Optional[str] = fields.JSONField(description="值", null=True)
    created_at: Optional[datetime] = fields.DatetimeField(
        auto_now_add=True, description="创建时间"
    )


class PresignUploadSession(models.Model):
    """预签名（直传）上传会话：把"客户端直传存储后端"这件事的上下文存下来。

    为什么要会话：直传时服务端不接触文件内容，确认阶段拿不到文件名/大小/
    过期策略，只能靠 upload_id 回查。会话同时充当**上传凭据**——
    没有它客户端无法确认，从而避免任意路径被写入。

    会话有独立过期时间（expires_at），与最终生成的分享过期时间无关：
    前者防"预签名链接被长期滥用"，后者才是用户可见的有效期。
    """

    id = fields.IntField(pk=True)
    upload_id = fields.CharField(max_length=36, unique=True, index=True)
    file_name = fields.CharField(max_length=255)
    file_size = fields.BigIntField()
    save_path = fields.CharField(max_length=512)
    mode = fields.CharField(max_length=10)  # "direct" 或 "proxy"
    expire_value = fields.IntField(default=1)
    expire_style = fields.CharField(max_length=20, default="day")
    remark = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    expires_at = fields.DatetimeField()  # 会话过期时间

    async def is_expired(self):
        """检查会话是否已过期。

        与 FileCodes.is_expired 不同：这里只看 expires_at，不存在次数模式，
        也不存在 NULL 情形（字段非空），因此不做空值兜底。
        """
        return self.expires_at < await get_now()


class FileItem(models.Model):
    """多文件分享中的单个文件项。

    一个 FileCodes（is_multi_file=True）对应 N 个 FileItem。
    拆分的原因是分享粒度：整包共用一个取件码、一份过期策略、一次取件计数，
    而每个子文件保留自己的名字/大小/哈希，供前端列表展示与逐个下载。

    删除父记录时级联删除子项，但**子项的物理文件同样可能被引用于别处**
    （file_hash 去重），故实际删除仍走引用检查。
    """
    id = fields.IntField(pk=True)
    file_code = fields.ForeignKeyField("models.FileCodes", related_name="items", on_delete=fields.CASCADE)
    file_name = fields.CharField(max_length=255)
    file_size = fields.BigIntField(default=0)
    file_hash = fields.CharField(max_length=64, null=True)
    storage_path = fields.CharField(max_length=512)
    uuid_file_name = fields.CharField(max_length=255, null=True)
    sort_order = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)

    async def get_full_path(self):
        """获取完整的存储路径（同 FileCodes.get_file_path，不做空值检查）"""
        return f"{self.storage_path}/{self.uuid_file_name}"


class CollectionBox(models.Model):
    """收件箱（文件收集）：一次创建、多人投递、创建者统一取件。

    ## 为什么是三码
    - `collection_code`：管理码，创建者持有，可删箱/改配置
    - `delivery_code`：投件码，对外公开，拿到只能投递
    - `retrieve_code`：取件码，创建者持有，拿到才能取件

    早期只有一码，结果是"任何人拿到链接就能把箱里所有文件下载走"。
    三码分离让**投递权限与取件权限天然隔离**，泄露投件链接不导致文件泄露。

    ## 为什么三套过期字段
    整箱、投件码、取件码各有独立过期时间：常见场景是"收集期一周，
    但收集结束后仍要留几天给创建者取件"，一套过期时间表达不了。
    代价是每个过期判定都要显式选字段，故提供三个 is_*_expired 方法避免写错。
    """
    id = fields.IntField(pk=True)
    # 三码长度受后台 codeLength 配置影响（最大 12），故字段放宽到 12
    collection_code = fields.CharField(max_length=12, unique=True, index=True)
    delivery_code = fields.CharField(max_length=12, unique=True, index=True)
    retrieve_code = fields.CharField(max_length=12, unique=True, index=True, default="")
    title = fields.CharField(max_length=100, default="")
    description = fields.TextField(default="", null=True)
    creator_ip = fields.CharField(max_length=45, default="")
    creator_name = fields.CharField(max_length=50, default="匿名")
    # 已废弃：历史上从未被任何流程校验，前端也没有设置入口。
    # 保留列只为兼容存量数据（读取时被忽略），不要重新启用——
    # 收件箱的访问控制已经由三码分离承担，再加一道口令只会制造"已加密"的错觉。
    password = fields.CharField(max_length=255, default="", null=True)
    max_files = fields.IntField(default=20)
    # 收件箱过期配置（创建后不可更改）
    expire_style = fields.CharField(max_length=20, default="day")
    expire_value = fields.IntField(default=7)
    expired_at = fields.DatetimeField(null=True)
    # 投件码过期配置
    delivery_expire_style = fields.CharField(max_length=20, default="day")
    delivery_expire_value = fields.IntField(default=7)
    delivery_expired_at = fields.DatetimeField(null=True)
    # 取件码过期配置
    retrieve_expire_style = fields.CharField(max_length=20, default="day")
    retrieve_expire_value = fields.IntField(default=7)
    retrieve_expired_at = fields.DatetimeField(null=True)
    delivery_count = fields.IntField(default=-1)  # 最大投递次数，-1 表示不限
    delivery_used_count = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    async def is_expired(self):
        """收件箱整箱是否过期（expired_at 为 NULL 或 forever 时视为不过期）。

        注意与 FileCodes 的区别：这里**用 expire_style 判断**而不是 expired_count，
        收件箱没有次数模式；`expire_style` 由调用方在创建时写入，本方法不校验取值合法性。
        """
        if self.expire_style == "forever":
            return False
        if self.expired_at is None:
            return False
        return self.expired_at < await get_now()

    async def is_delivery_expired(self):
        """投件码是否过期（独立于收件箱过期）"""
        if self.delivery_expire_style == "forever":
            return False
        if self.delivery_expired_at is None:
            return False
        return self.delivery_expired_at < await get_now()

    async def is_retrieve_expired(self):
        """取件码是否过期（独立于收件箱过期）"""
        if self.retrieve_expire_style == "forever":
            return False
        if self.retrieve_expired_at is None:
            return False
        return self.retrieve_expired_at < await get_now()


class CollectionFile(models.Model):
    """收件箱中的一次投递记录。

    投递本身复用 FileCodes 存文件本体（这样投递件也能被秒传/去重），
    本表只记录"谁、何时、投到哪个箱子"以及上传过程中的状态。

    `status` 为 `uploading` 时记录可能还没有 file_code（分片上传中），
    故 file_code 用 SET_NULL：箱子里已删除的文件仍保留投递痕迹，
    否则创建者会看到"文件凭空消失"而无法解释。
    """
    id = fields.IntField(pk=True)
    collection = fields.ForeignKeyField("models.CollectionBox", related_name="files", on_delete=fields.CASCADE)
    file_code = fields.ForeignKeyField("models.FileCodes", related_name="collection_files", null=True, on_delete=fields.SET_NULL)
    uploader_name = fields.CharField(max_length=50, default="", null=True)
    uploader_ip = fields.CharField(max_length=45, default="")
    status = fields.CharField(max_length=20, default="uploading")
    created_at = fields.DatetimeField(auto_now_add=True)
    # 分片上传会话信息（非分片上传为 NULL）
    upload_id = fields.CharField(max_length=36, null=True, index=True)
    file_name = fields.CharField(max_length=255, default="", null=True)
    file_size = fields.BigIntField(default=0, null=True)
    chunk_size = fields.IntField(default=0, null=True)
    total_chunks = fields.IntField(default=0, null=True)


class Room(models.Model):
    """跨设备实时房间：聊天、文件传输、直连快传（P2P）合并后的统一载体。

    ## 为什么合并
    三种玩法此前各有一套房间码与成员管理，用户要记住三个入口、运维要维护三份清理逻辑。
    合并后**一个房间码即一个协作空间**，`mode` 决定启用哪些能力：
    chat（仅消息）/ transfer（仅文件）/ direct（消息 + 文件 + P2P 媒体）。

    ## 状态存放的边界
    房间的**在线连接**在进程内存里（core/websocket），只有落库的两类数据在这里：
    `save_messages=1` 时消息落 RoomMessage、`cache_enabled=1` 时缓存文件落
    RoomCachedFile。两者都随房间过期被清理——刻意不做"房间没了但历史还在"，
    因为房间码是一次性协作凭证，保留历史等于延长数据寿命，与产品定位冲突。

    ## 字段陷阱
    `save_messages` / `cache_enabled` 是 IntField(0/1) 而非 BooleanField，
    因为存量库已按整型写入；读取侧统一用 `bool()` 转换，不要直接比较 True。
    """
    id = fields.IntField(pk=True)
    # 房间码长度受后台 codeLength 配置影响（最大 12），故字段放宽到 12
    room_code = fields.CharField(max_length=12, unique=True, index=True)
    title = fields.CharField(max_length=100, default="")
    creator_ip = fields.CharField(max_length=45, default="")
    max_members = fields.IntField(default=10)
    mode = fields.CharField(max_length=20, default="direct")
    expire_style = fields.CharField(max_length=20, default="day")
    expire_value = fields.IntField(default=1)
    expired_at = fields.DatetimeField(null=True)
    # 次数模式剩余会话次数（-1 表示非次数模式不限制；expire_style="count" 时有效）
    expired_count = fields.IntField(default=-1)
    # 是否把聊天消息/文件元信息保存到服务器（随房间过期删除，见 RoomMessage/RoomCachedFile）
    save_messages = fields.IntField(default=0)
    # 是否启用房间文件缓存（已接收成员作为多源，可转发给其他/新加入成员收取）
    cache_enabled = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)

    async def is_expired(self):
        """房间是否过期（count 模式：剩余次数用尽即过期；forever 永不过期）。

        与 FileCodes 用 `expired_count` 的正负号区分模式不同，这里直接看
        `expire_style` 字符串——房间的次数模式语义是"剩余会话次数"，
        创建时由调用方同时写 style 与 count，两者不再互相推导。
        """
        if self.expire_style == "count":
            return self.expired_count <= 0
        if self.expire_style == "forever":
            return False
        if self.expired_at is None:
            return False
        return self.expired_at < await get_now()


class RoomMessage(models.Model):
    """房间历史记录（save_messages=1 时落库，随房间过期/删除清理）。
    msg_type ∈ text（聊天）/ file（传输过的文件元信息，历史展示用）。

    文件消息与文件记录不再分家：一条 `msg_type=file` 记录本身就是那条历史，
    额外挂上 `storage_key`/`expires_at` 即可表达"是否还有服务器副本可下载"。
    这样历史列表不需要再 JOIN 缓存表，删除缓存时只需把 storage_key 清空。
    """
    id = fields.IntField(pk=True)
    room_code = fields.CharField(max_length=12, index=True)
    sender = fields.CharField(max_length=64, default="匿名")
    # text：聊天内容；file：transfer_id（供历史文件与缓存文件关联）
    content = fields.TextField(default="")
    msg_type = fields.CharField(max_length=16, default="text")
    file_name = fields.CharField(max_length=255, default="")
    file_size = fields.BigIntField(default=0)
    # ===== 文件消息专用（msg_type=file）=====
    # 消息与文件记录不再分家：文件消息本身就是这条记录，额外挂上缓存信息即可。
    # 内容哈希（去重键）：同一房间内相同内容的文件只落盘一份，重复上传只续期
    file_hash = fields.CharField(max_length=64, default="", index=True)
    # 服务器落盘相对路径（相对 data/room_cache）；空串 = 未缓存到服务器
    storage_key = fields.TextField(default="")
    # 缓存过期时间；NULL = 跟随房间生命周期（房间过期/删除时统一回收）
    expires_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "room_message"


class RoomCachedFile(models.Model):
    """房间缓存文件元信息（cache_enabled=1 时记录：已接收成员可作多源转发给他人/新成员收取）"""
    id = fields.IntField(pk=True)
    room_code = fields.CharField(max_length=12, index=True)
    transfer_id = fields.CharField(max_length=64, index=True)
    owner_id = fields.CharField(max_length=64, default="")
    file_name = fields.CharField(max_length=255, default="")
    file_size = fields.BigIntField(default=0)
    # 记录文件已缓存的成员列表（逗号分隔的 client_id）；接收者完成接收后追加
    cache_holder_ids = fields.TextField(default="")
    # 与 cache_holder_ids 对应的持有者昵称（逗号分隔），供"共享文件"展示用户名
    holder_names = fields.TextField(default="")
    # 服务器落盘相对路径（相对 data/room_cache）；空串 = 仅 P2P 转发、未占服务器磁盘
    storage_key = fields.TextField(default="")
    # 服务器缓存过期时间；NULL = 跟随房间生命周期（房间过期/删除时统一回收）
    expires_at = fields.DatetimeField(null=True)
    # 落盘时间（列表排序与展示）
    cached_at = fields.DatetimeField(null=True)
    # 上传者昵称（owner_id 存 client_id，重连后会变，展示用昵称更稳定）
    uploader_name = fields.CharField(max_length=64, default="")
    # 内容 SHA-256（服务端落盘时流式计算，非客户端声明值）：
    # 下发给客户端用于校验"从多源收取并合并后的文件"是否完整
    file_hash = fields.CharField(max_length=64, default="")
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "room_cached_file"


class IPLimit(models.Model):
    """IP 限流计数（持久化到数据库，多 worker 共享；进程内不再各自计数）。

    为什么不放在进程内存：多 worker 时每个进程各算各的，实际放行量是
    配置值的 N 倍，限流形同虚设。落库后代价是**每次请求多一次 DB 读写**，
    因此只在触发写路径（`add_ip`）时才落库，纯查询路径是一次带索引的点查。

    `last_time` 同时承担"窗口起点"与"可清理标记"两个职责，
    故清理任务直接按 `last_time < now - 窗口` 删除，无需额外字段。
    """
    id = fields.IntField(pk=True)
    # 限流桶：error（取件/下载失败）或 upload（上传/创建）
    bucket = fields.CharField(max_length=20, index=True)
    ip = fields.CharField(max_length=45, index=True)
    count = fields.IntField(default=0)
    last_time = fields.DatetimeField()

    class Meta:
        table = "iplimit"
        unique_together = (("bucket", "ip"),)


class AuditLog(models.Model):
    """审计日志：记录"谁在何时对什么做了什么、结果如何"。

    合规与安全事件溯源需要它（例如：某个取件码被哪个 IP 取走过、管理员何时改过配置）。
    写入统一走 core/audit.record()，该接口保证**不因审计失败影响主流程**。

    刻意不做两件事：
    1. **不记录请求体/文件内容**——只记动作、对象标识与结果；detail 长度被截断到 255，
       避免把敏感值（口令、路径）写进日志表本身。
    2. **不建外键**——审计要能在被审计对象删除后继续存活，
       target_id 因此是自由文本（取件码/房间码），不是引用。
    """
    id = fields.IntField(pk=True)
    # 动作：upload / retrieve / download / delete / login / login_failed /
    #      config_update / room_cache_upload / room_cache_delete ...
    action = fields.CharField(max_length=32, index=True)
    # 对象类型：filecode / collection / room / admin / system
    target_type = fields.CharField(max_length=32, default="", index=True)
    # 对象标识：取件码 / 收件箱码 / 房间码 / 文件 id（系统级操作可为空）
    target_id = fields.CharField(max_length=64, default="", index=True)
    # 结果：success / failed / denied
    result = fields.CharField(max_length=16, default="success")
    # 补充说明（失败原因、文件名等）
    detail = fields.CharField(max_length=255, default="")
    ip = fields.CharField(max_length=64, default="")
    # 操作者标识（管理员操作记 admin，普通操作留空）
    actor = fields.CharField(max_length=64, default="")
    created_at = fields.DatetimeField(auto_now_add=True, index=True)

    class Meta:
        table = "audit_log"


# Tortoise 自动生成的响应模型。当前后台/分享接口都走手写 dict（字段需要裁剪与派生），
# 这些导出在代码内无引用，保留只为兼容可能的外部导入；新增模型不必照此补一个。
file_codes_pydantic = pydantic_model_creator(FileCodes, name="FileCodes")
upload_chunk_pydantic = pydantic_model_creator(UploadChunk, name="UploadChunk")
key_value_pydantic = pydantic_model_creator(KeyValue, name="KeyValue")
presign_upload_session_pydantic = pydantic_model_creator(
    PresignUploadSession, name="PresignUploadSession"
)
file_item_pydantic = pydantic_model_creator(FileItem, name="FileItem")
collection_box_pydantic = pydantic_model_creator(CollectionBox, name="CollectionBox")
collection_file_pydantic = pydantic_model_creator(CollectionFile, name="CollectionFile")
room_pydantic = pydantic_model_creator(Room, name="Room")
