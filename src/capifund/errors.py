"""业务 API 的统一错误；接入层只转换呈现形式。"""


class RecordError(ValueError):
    status = 400
    code = "invalid_request"


class NotFound(RecordError):
    status = 404
    code = "not_found"


class Conflict(RecordError):
    status = 409
    code = "revision_conflict"


class ServiceUnavailable(RecordError):
    status = 503
    code = "storage_unavailable"


class StorageFailure(Exception):
    """存储适配器的内部异常，不直接向接入层暴露路径或 SQL。"""
