FROM python:3.12-bookworm

ENV LANG=C.UTF-8 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN echo "deb http://ftp.cn.debian.org/debian sid main" >> /etc/apt/sources.list;\
    apt-get update; \
    apt-get install -y --no-install-recommends iputils-ping bash tini; \
    apt-get install -y --no-install-recommends ffmpeg;\
    ffmpeg -version; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*;

COPY ./pyproject.toml /app/pyproject.toml

RUN pip install -U pip --no-cache; \
    pip install poetry --no-cache; \
    poetry install --no-root; \
    pip cache purge;

COPY . /app/

RUN chmod +x /app/run.bash;

ARG ARG_VERSION=0.0.0
ENV VERSION=${ARG_VERSION} \
    RTSP_URL="rtsp://" \
    DEVICE_NAME=""

VOLUME /app/data

ENTRYPOINT [ "tini","--","/app/run.bash" ]
