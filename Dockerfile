# 1. 使用带有 uv 的基础镜像
FROM mwader/static-ffmpeg:latest AS ffmpeg_image
FROM ghcr.io/astral-sh/uv:alpine AS builder

# 2. 启用编译字节码并安装 Python 3.14t (Free-threaded)
ENV UV_COMPILE_BYTECODE=1
# cp314t 代表 Python 3.14 free-threaded
RUN uv python install 3.14t

# 3. 创建虚拟环境并安装依赖
WORKDIR /app
COPY requirements.txt .

# 这一步会自动下载兼容 cp314t-musllinux 的 wheels
RUN apk add gcc musl-dev linux-headers --no-cache;\
    uv venv .venv --python 3.14t; \
    uv pip install -r requirements.txt

# 4. 最终运行时镜像 (保持极小体积)
FROM alpine:3.21

COPY --from=ffmpeg_image /ffmpeg /usr/local/bin/
COPY --from=ffmpeg_image /ffprobe /usr/local/bin/

RUN ffmpeg -version;
# 安装运行所需的系统库 (libstdc++ 等通常是 numpy 需要的)
RUN apk add --no-cache libstdc++ iputils-ping;

WORKDIR /app
# 从 builder 阶段复制 Python 环境和虚拟环境
COPY --from=builder /root/.local/share/uv/python /root/.local/share/uv/python
COPY --from=builder /app/.venv /app/.venv

# 将 uv 管理的 python 路径加入 PATH
ENV PATH="/app/.venv/bin:$PATH"

# 验证是否为无 GIL 版本 (输出应包含 "Free-threaded: True" 或类似标识)
RUN python -c "import sys; print(f'Free-threading: {sys._is_gil_enabled() == False}')"


WORKDIR /app

COPY . /app/

RUN ls -la;\
    chmod +x /app/run.sh;\
    touch /app/.env;

ARG ARG_VERSION=0.0.0
ENV LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    VERSION=${ARG_VERSION} \
    RTSP_URL="rtsp://" \
    DEVICE_NAME="" \
    INPUT_FPS="25"

VOLUME /app/data

USER root
ENTRYPOINT [ "/bin/sh","/app/run.sh" ]
