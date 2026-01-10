import uuid


def get_device_id():
    return uuid.uuid5(uuid.NAMESPACE_DNS, str(uuid.getnode())).hex
