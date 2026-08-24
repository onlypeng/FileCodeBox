# 第一阶段：构建前端主题
FROM node:20-alpine AS frontend-builder

RUN apk add --no-cache git python3 make g++

WORKDIR /build

# 克隆并构建 2024 主题
RUN git clone --depth 1 https://github.com/vastsa/FileCodeBoxFronted.git /build/fronted-2024 && \
    cd /build/fronted-2024 && \
    npm install && \
    npm run build

# 克隆并构建 2023 主题
RUN git clone --depth 1 https://github.com/vastsa/FileCodeBoxFronted2023.git /build/fronted-2023 && \
    cd /build/fronted-2023 && \
    npm install --legacy-peer-deps && \
    npm run build

# 第二阶段：构建最终镜像
FROM python:3.12-slim-bookworm
LABEL author="Lan"
LABEL email="xzu@live.com"

WORKDIR /app

# 复制项目文件（通过 .dockerignore 排除不必要的文件）
COPY . .

# 设置时区
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo 'Asia/Shanghai' > /etc/timezone

# 从构建阶段复制编译好的前端主题
COPY --from=frontend-builder /build/fronted-2024/dist ./themes/2024
COPY --from=frontend-builder /build/fronted-2023/dist ./themes/2023

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

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
CMD uvicorn main:app \
    --host $HOST \
    --port $PORT \
    --workers $WORKERS \
    --log-level $LOG_LEVEL \
    --proxy-headers \
    --forwarded-allow-ips "127.0.0.1"