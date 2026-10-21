"""协议使用的标准 JSON 编码。"""
import json
from typing import Any


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def unique_mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise ValueError("metadata field names must be strings")
        if key in result:
            raise ValueError(f"duplicate metadata field: {key}")
        result[key] = value
    return result
