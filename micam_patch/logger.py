import os
import sys
from loguru import logger
from logging import getLogger, WARNING

LOG_FMT = "<le>{time:YYYY-MM-DD HH:mm:ss.SSS}</le> | <level>{level: <8}</level> | <le>{process: <5}:{thread: <5}</le> | <le>{file}:{line}</le> | <level>{message}</level>"

logger.remove()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
IN_DOCKER = os.getenv("IN_DOCKER", "false")
colorize = True if IN_DOCKER == "false" else False
logger.add(
    sys.stdout,
    level=LOG_LEVEL,
    format=LOG_FMT,
    colorize=colorize,
    enqueue=True,
)

for name in ("miot.lan", "miot.camera", "miot.cloud"):
    log1 = getLogger(name)
    log1.setLevel(WARNING)
