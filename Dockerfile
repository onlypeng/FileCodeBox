# 最终阶段：仅 Python 运行时（前端构建产物已随仓库内置在 frontend_dist，无需联网构建前端）
FROM python:3.12-slim-bookworm
LABEL author="onlypeng"
LABEL maintainer="onlypeng"

WORKDIR /app

# 复制项目文件（通过 .dockerignore 排除不必要的文件；frontend_dist 包含最新前端构建产物）
COPY . .

# 设置时区
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo 'Asia/Shanghai' > /etc/timezone

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 构建期自检：裸 uvicorn 不含 WS 协议实现，缺 websockets/wsproto 时镜像照样能启动、
# 所有 HTTP 接口正常，但每个 /ws/* 升级请求都会落到 404 handler（实时房间永远"已断开"）。
# 这种「局部静默不可用」在容器日志里看不出来，所以在构建阶段就让它直接失败。
RUN python -c "import uvicorn, websockets; print('websocket protocol OK')"

# 环境变量配置
# JWT_SECRET: 签名密钥，设置后修改管理员密码不会使已登录 Token 失效（留空则回退到管理员口令）
# ENCRYPTION_KEY: 敏感配置（S3/WebDAV 等凭据）的加密密钥，强烈建议生产环境配置（留空则从管理员口令派生）
ENV HOST="0.0.0.0" \
    PORT=12345 \
    WORKERS=1 \
    LOG_LEVEL="info" \
    JWT_SECRET="" \
    ENCRYPTION_KEY=""

EXPOSE 12345

# 生产环境启动命令
# 注意：forwarded-allow-ips 仅信任回环地址的转发头，防止客户端伪造 X-Forwarded-For 绕过 IP 限流
# 若部署在反向代理之后，请将代理地址加入 --forwarded-allow-ips 白名单
# 前端：后端启动时自动优先 serve /app/frontend_dist（Vue3 构建产物），缺失时回退 themes/
CMD uvicorn main:app \
    --host $HOST \
    --port $PORT \
    --workers $WORKERS \
    --log-level $LOG_LEVEL \
    --proxy-headers \
    --forwarded-allow-ips "127.0.0.1"