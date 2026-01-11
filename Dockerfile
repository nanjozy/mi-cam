# 1. 使用带有 uv 的基础镜像
FROM mwader/static-ffmpeg:latest AS ffmpeg_image

FROM python:3.12-slim-bookworm

USER root


ENV LANG=C.UTF-8 \
    TZ=Asia/Shanghai

COPY --from=ffmpeg_image /ffmpeg /usr/local/bin/
COPY --from=ffmpeg_image /ffprobe /usr/local/bin/

WORKDIR /app

RUN apt-get update; \
    apt-get install -y --no-install-recommends iputils-ping; \
    ffmpeg -version; \
    apt-get autoremove -y;\
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*;

COPY ./requirements.txt /app/requirements.txt

RUN python -m venv .venv;\
    /app/.venv/bin/pip install -U pip --no-cache; \
    /app/.venv/bin/pip install --no-cache -r requirements.txt; \
    /app/.venv/bin/pip cache purge;

COPY . /app/

RUN chmod +x /app/run.bash;\
    touch /app/.env;

ARG ARG_VERSION=0.0.0
ENV VERSION=${ARG_VERSION} \
    RTSP_URL="rtsp://" \
    DEVICE_NAME="" \
    INPUT_FPS="25" \
    CODEC="hevc"


VOLUME /app/data

USER root
ENTRYPOINT [ "/bin/bash","/app/run.bash" ]
