"""跨进程协调层：让"多 worker 部署"从固定不可用变成可配置能力。

## 问题

本项目此前有两类**进程内**状态，决定了它只能单进程运行：

1. `ws_manager` 的房间连接表在进程内存里 —— 多 worker 时，房间内文本信令
   （成员上下线、文件卡片、WebRTC 信令、屏幕共享通知）只落在发送者所在进程。
2. 房间缓存配额锁是 `asyncio.Lock` —— 各进程各锁各的，配额校验被并发绕过。

数据库类型只解决"多进程能否并发写"，解决不了这两点。见 core/capabilities.py。

## 方案

把"跨实例"收口到本模块，只暴露三个原语：

- `publish(event)` 事件广播（其它实例订阅后回调 handler）
- `lock(name)` 分布式锁（房间配额校验串行化）
- `state()` 运行状态（供能力报告与后台诊断）

**默认实现 LocalHub 全部 no-op / 进程内锁**：未配置 `redisUrl` 时行为与改造前
逐字节等价，不引入任何新依赖，也不会因为"看起来支持多实例"而误导运维。

配置 `redisUrl` 后切到 RedisHub。依赖 `redis` 包按需安装（未装时启动明确报错
并回落 LocalHub），绝不静默降级。

## 刻意不做的事

**房间文件分片（二进制帧）不跨实例转发**。单帧上限 256KB，走 pub/sub 会把
吞吐放大 1.4 倍（base64）并引入不可控延迟；工程上正确的做法是接入层做会话亲和
（同一房间的 WS 连接粘到同一实例），这也是主流实时服务的通行做法。
跨实例时中继发送方会收到明确提示，而不是"文件传到一半没了"。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

from core.logger import logger

# 集群事件通道（所有实例共用一个通道；文本信令量级远低于文件流，够用）
CLUSTER_CHANNEL = "filecodebox:cluster"
# 实例标识（每个 hub 自己一个）：用于过滤自己发出的消息、以及在心跳里标识"谁还在线"。
# 刻意不做成模块全局 —— 同一进程内可能并存多个 hub（测试模拟多实例时就是如此），
# 全局 id 会让它们把对方的消息当成自己的回音丢掉。

EventHandler = Callable[[dict], Awaitable[None]]


class LocalHub:
    """单实例实现：广播是 no-op，锁是进程内 asyncio.Lock。

    调用方（ConnectionManager）始终先在本地投递，再调用 publish 通知"其它实例"，
    因此单实例下 publish 无事可做 —— 这也是改造前后行为完全一致的原因。
    """

    mode = "local"
    distributed = False

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._failures = 0
        self.instance_id = uuid.uuid4().hex[:12]

    async def start(self) -> None:
        logger.info("集群协调: 本地模式（单实例），未配置 redisUrl")

    async def close(self) -> None:
        return None

    def on_event(self, handler: EventHandler) -> None:
        """单实例下不存在"其它实例"，注册的 handler 永不被调用。"""
        return None

    async def publish(self, event: dict) -> None:
        return None

    @asynccontextmanager
    async def lock(self, name: str, ttl_ms: int = 60000, wait_ms: int = 15000):
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        async with lock:
            yield

    def state(self) -> dict:
        return {"mode": self.mode, "distributed": False, "publish_failures": 0}


class RedisHub(LocalHub):
    """Redis pub/sub 实现。

    容错策略（重要）：Redis 是**协调件**不是主链路。Redis 抖动时：
    - 广播失败：丢文本信令，但成员快照心跳会在恢复后自愈；记计数不抛异常
    - 加锁失败：降级为本地锁继续写（宁可极小概率超配额，也不要上传整体不可用）
    """

    mode = "redis"
    distributed = True

    def __init__(self, url: str, channel: str = CLUSTER_CHANNEL, client=None) -> None:
        super().__init__()
        self._url = url
        self._channel = channel
        # 允许注入已构造好的客户端：测试用 fakeredis 模拟"两个实例连同一个 Redis"，
        # 生产走 from_url(url)
        self._client = client
        self._pubsub = None
        self._reader: Optional[asyncio.Task] = None
        self._handler: Optional[EventHandler] = None

    async def start(self) -> None:
        if self._client is None:
            try:
                import redis.asyncio as aioredis  # 延迟导入：未装 redis 时不拖累单实例部署
            except ImportError as e:  # pragma: no cover - 取决于运行环境
                raise RuntimeError(
                    "配置了 redisUrl 但未安装 redis 包，请执行 pip install redis，"
                    "或将 redisUrl 置空以使用单实例模式"
                ) from e
            self._client = aioredis.from_url(self._url, decode_responses=True)
        # 首次连接失败要直接暴露：配置写错时启动就该失败，而不是运行到一半才发现
        await self._client.ping()
        self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        await self._pubsub.subscribe(self._channel)
        self._reader = asyncio.create_task(self._read_loop())
        logger.info(f"集群协调: Redis 模式 instance={self.instance_id} channel={self._channel}")

    def on_event(self, handler: EventHandler) -> None:
        self._handler = handler

    async def _read_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                message = await self._pubsub.get_message(timeout=1.0)
                if message is None:
                    backoff = 1.0
                    continue
                raw = message.get("data")
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    # 通道上混入非本系统的消息：丢弃即可，不能让解析异常打断订阅循环
                    continue
                if event.get("origin") == self.instance_id:
                    continue  # 自己发出的消息，本地已经投递过
                if self._handler is not None:
                    try:
                        await self._handler(event)
                    except Exception as e:
                        logger.warning(f"处理集群事件失败 type={event.get('t')}: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 订阅连接断开：退避重连，绝不让后台任务死掉（死了就永久不同步）
                self._failures += 1
                logger.warning(f"集群订阅异常，{backoff:.1f}s 后重连: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                try:
                    if self._pubsub is not None:
                        await self._pubsub.aclose()
                    self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
                    await self._pubsub.subscribe(self._channel)
                except Exception as re_e:
                    logger.warning(f"集群重订阅失败: {re_e}")

    async def publish(self, event: dict) -> None:
        if self._client is None:
            return
        payload = {**event, "origin": self.instance_id}
        try:
            await self._client.publish(self._channel, json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            self._failures += 1
            # 不刷屏：只在首次与每 100 次失败时告警
            if self._failures == 1 or self._failures % 100 == 0:
                logger.warning(f"集群广播失败（累计 {self._failures} 次）: {e}")

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        # 优雅下线：通知其它实例立即摘掉本实例的成员，不必等心跳超时
        try:
            await self.publish({"t": "bye"})
        except Exception:
            pass
        try:
            if self._pubsub is not None:
                await self._pubsub.aclose()
            if self._client is not None:
                await self._client.aclose()
        except Exception:
            pass

    @asynccontextmanager
    async def lock(self, name: str, ttl_ms: int = 60000, wait_ms: int = 15000):
        local = self._locks.get(name)
        if local is None:
            local = asyncio.Lock()
            self._locks[name] = local
        # 先拿进程内锁：同进程并发不必几千次往返 Redis
        async with local:
            if self._client is None:
                yield
                return
            key = f"fcb:lock:{name}"
            token = uuid.uuid4().hex
            acquired = await self._acquire(key, token, ttl_ms, wait_ms)
            if not acquired:
                logger.warning(f"分布式锁未取得（Redis 不可用或等待超时），降级为本地锁: {name}")
                yield
                return
            # 持有期间续期：大文件落盘可能远超 ttl，不续期就会在中途"掉锁"，
            # 让另一个实例在配额校验的临界区里进来（正是这把锁要防的事）
            renewer = asyncio.create_task(self._renew_loop(key, token, ttl_ms))
            try:
                yield
            finally:
                renewer.cancel()
                try:
                    await renewer
                except (asyncio.CancelledError, Exception):
                    pass
                await self._release(key, token)

    async def _renew_loop(self, key: str, token: str, ttl_ms: int) -> None:
        interval = max(ttl_ms / 3000.0, 0.5)
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"
        )
        while True:
            await asyncio.sleep(interval)
            try:
                # 只续自己持有的锁；返回 0 说明已被他人持有，直接停下不再续
                if not await self._client.eval(script, 1, key, token, ttl_ms):
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                return

    async def _acquire(self, key: str, token: str, ttl_ms: int, wait_ms: int) -> bool:
        deadline = asyncio.get_event_loop().time() + wait_ms / 1000
        while True:
            try:
                ok = await self._client.set(key, token, nx=True, px=ttl_ms)
                if ok:
                    return True
            except Exception as e:
                self._failures += 1
                logger.warning(f"分布式锁获取异常，按未取得处理: {e}")
                return False
            if asyncio.get_event_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    async def _release(self, key: str, token: str) -> None:
        # 只释放自己持有的锁：TTL 到期被他人抢到后，绝不能误删别人的锁
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        try:
            await self._client.eval(script, 1, key, token)
        except Exception as e:
            logger.warning(f"释放分布式锁失败（将由 TTL 兜底）: {e}")

    def state(self) -> dict:
        return {
            "mode": self.mode,
            "distributed": True,
            "instance_id": self.instance_id,
            "publish_failures": self._failures,
        }


def build_hub(url: str) -> LocalHub:
    """按配置构造协调件。url 为空 → 单实例；否则 → Redis。"""
    url = (url or "").strip()
    if not url:
        return LocalHub()
    return RedisHub(url)


# 全局单例：启动时由 main.lifespan 替换为实际实现（配置项在启动时读取一次，
# 运行期改 redisUrl 需要重启生效 —— Redis 连接不能热切换，这一点在后台提示中写明）
hub: LocalHub = LocalHub()
