"""标签规范化与 SQL 计数树组装；展示容忍历史层级，写入校验保持严格。"""
from __future__ import annotations
import re
from typing import Any

TAG_SPLIT_RE = re.compile(r"[\s,，]+")


def _tag(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Tag must be a string")
    value = value.strip().lstrip("#").strip().casefold()
    parts = value.split("/")
    if not 1 <= len(parts) <= 3:
        raise ValueError("Tags have at most three levels separated by /")
    for part in parts:
        if not part or part in {".", ".."} or "\\" in part:
            raise ValueError("Tag levels must not be empty or contain path traversal")
        if len(part) > 40:
            raise ValueError("Each tag level must not exceed 40 characters")
        if any(character.isspace() or ord(character) < 32 for character in part):
            raise ValueError("Tags must not contain whitespace or control characters")
    return "/".join(parts)


def clean_tags(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = TAG_SPLIT_RE.split(value)
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError("Tags must be a string or a list of strings")
    result = tuple(dict.fromkeys(_tag(item) for item in value if item.strip()))
    if len(result) > 12:
        raise ValueError("At most 12 tags per record")
    return result


def display_tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip().lstrip("#").strip().casefold()
        if not tag or any(not part or part in {".", ".."} for part in tag.split("/")) or any(c.isspace() or ord(c) < 32 for c in tag):
            continue
        result.append(tag)
    return tuple(dict.fromkeys(result))


def canonical_tag(tag: str) -> str:
    tags = display_tags([tag])
    if not tags:
        raise ValueError("Invalid tag path")
    return tags[0]


def tree_for_counts(counts: dict[str, int]) -> list[dict[str, Any]]:
    nodes = {path: {"path": path, "name": path.rsplit("/", 1)[-1], "count": count, "children": []}
             for path, count in counts.items()}
    result = []
    for path in sorted(nodes):
        parent, _, _ = path.rpartition("/")
        (nodes[parent]["children"] if parent else result).append(nodes[path])
    return result
