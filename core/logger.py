"""全局 logger。

刻意只挂 StreamHandler 到 stdout：进程跑在容器/systemd 下时，日志应交由上层
采集（docker logs / journald），自己在应用里做文件轮转会与它冲突——尤其是多
worker 部署时多个进程写同一个日志文件必然互相覆盖。

也因此这里不做按级别分文件、不做结构化 JSON：为了容器里 `docker logs` 直接可读。
需要集中的地方由部署侧处理。
"""
import logging
import sys


def setup_logger():
    # 用独立命名而不是 root logger：避免第三方库（uvicorn/aiohttp）配置把我们
    # 的格式改掉，也让 core.* 之外的日志不会被我们自己的 handler 重复输出。
    _logger = logging.getLogger('FileCodeBox')
    _logger.setLevel(logging.INFO)

    # 创建控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    # 设置日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(formatter)

    # 这里刻意不设置 propagate=False：core.utils 里的加密告警等使用的是
    # logging.getLogger(__name__)，靠冒泡到 root 由部署侧的 basicConfig 兜住。
    # 同理若重复调用 setup_logger（例如 pytest 多次导入），会重复 addHandler
    # 导致每条日志输出两遍 —— 调用方应当只导入、不重复调用。
    _logger.addHandler(console_handler)

    return _logger


# 模块导入即完成配置（有副作用）：core.* 各处 `from core.logger import logger`
# 拿到的就是这个实例，没有延迟初始化的入口
logger = setup_logger()
