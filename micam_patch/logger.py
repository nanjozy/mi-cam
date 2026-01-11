from logging import WARNING, getLogger,INFO

for name in ("miot.lan", "miot.camera", "miot.cloud", "miot.i18n"):
    log1 = getLogger(name)
    log1.setLevel(WARNING)

logging = getLogger("main")
logging.setLevel(INFO)