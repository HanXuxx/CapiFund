"""统一业务 API 中间层；接入层和存储层仅通过此处交换业务请求。"""
from __future__ import annotations
import inspect
from ..errors import RecordError, ServiceUnavailable, StorageFailure
from .ports import FundStorage
from .records import RecordService


class BusinessAPI:
    def __init__(self, storage: FundStorage):
        records = RecordService(storage)
        self._operations = {name: getattr(records, name) for name in (
            "protocol", "tag_tree", "agenda", "query", "read", "history", "topic", "create", "update")}
        self._signatures = {name: inspect.signature(method) for name, method in self._operations.items()}

    def call(self, operation, arguments):
        if not isinstance(arguments, dict):
            raise RecordError("arguments must be an object")
        method = self._operations.get(operation)
        if method is None:
            raise RecordError("Unknown operation")
        try:
            self._signatures[operation].bind(**arguments)
        except TypeError as exc:
            raise RecordError("Invalid operation parameters") from exc
        try:
            return method(**arguments)
        except StorageFailure as exc:
            raise ServiceUnavailable("CapiFund storage is temporarily unavailable") from exc
