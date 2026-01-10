FROM python:3.12-slim-bookworm

ENV LANG=C.UTF-8 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN echo "deb http://ftp.cn.debian.org/debian sid main non-free" >> /etc/apt/sources.list;\
    apt-get update; \
    apt-get install -y --no-install-recommends intel-media-va-driver-non-free libmfx1; \
    apt-get install -y --no-install-recommends iputils-ping bash tini; \
    apt-get install -y --no-install-recommends ffmpeg;\
    ffmpeg -version; \
    apt-get autoremove -y;\
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*;

COPY ./pyproject.toml /app/pyproject.toml

RUN python -m venv .venv;\
    /app/.venv/bin/pip install -U pip --no-cache; \
    /app/.venv/bin/pip install poetry --no-cache; \
    /app/.venv/bin/poetry install --no-root; \
    /app/.venv/bin/pip cache purge;

COPY . /app/

RUN chmod +x /app/run.bash;\
    touch /app/.env;

ARG ARG_VERSION=0.0.0
ENV VERSION=${ARG_VERSION} \
    RTSP_URL="rtsp://" \
    DEVICE_NAME="" \
    CODEC_FIX="0" \
    HWACC="0" \
    LIBVA_DRIVER_NAME="iHD"

VOLUME /app/data

USER root
ENTRYPOINT [ "tini","--","/app/run.bash" ]
