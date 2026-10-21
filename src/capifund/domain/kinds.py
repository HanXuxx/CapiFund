"""投研保留键：系统行为依赖的 custom 字段及其状态规则；其余 custom 键原样保存。"""
from __future__ import annotations

from datetime import date
from typing import Any

from ..errors import RecordError

THESIS_STATUSES = ("draft", "active", "closed")
CLOSE_REASONS = ("validated", "falsified", "expired", "abandoned")
REPLY_KINDS = ("checkpoint", "evidence")
CHECKPOINT_STATUSES = ("pending", "confirmed", "refuted", "expired")
STANCES = ("support", "against", "neutral")

# fund_protocol 对外公布的保留键清单；值为允许取值或格式说明。
RESERVED = {
    "thesis": {"thesis_status": list(THESIS_STATUSES), "close_reason": list(CLOSE_REASONS)},
    "comment": {"kind": list(REPLY_KINDS)},
    "comment.kind=checkpoint": {"status": list(CHECKPOINT_STATUSES), "due_date": "YYYY-MM-DD"},
    "comment.kind=evidence": {"stance": list(STANCES)},
}


def apply_reserved(metadata: dict[str, Any], *, previous: dict[str, Any] | None, topic: dict[str, Any] | None) -> dict[str, Any]:
    """校验合并后的 metadata；创建时补默认值，修改时要求保留键显式存在。

    previous 为修改前的 custom，创建时为 None；topic 为回复所属主题的 metadata，仅创建回复时需要。
    """
    custom = dict(metadata["custom"])
    record_type = metadata["type"]
    if record_type != "thesis" and {"thesis_status", "close_reason"} & custom.keys():
        raise RecordError("custom.thesis_status and custom.close_reason belong to thesis records")
    if record_type != "comment" and "kind" in custom:
        raise RecordError("custom.kind belongs to replies")
    if record_type == "thesis":
        _thesis(custom, previous)
    elif record_type == "comment":
        _reply(custom, previous, topic)
    return {**metadata, "custom": custom}


def _required(custom, key, previous, default):
    if key not in custom:
        if previous is not None:
            raise RecordError(f"custom.{key} is required")
        custom[key] = default
    return custom[key]


def _thesis(custom, previous):
    status = _required(custom, "thesis_status", previous, "draft")
    if status not in THESIS_STATUSES:
        raise RecordError("custom.thesis_status must be draft, active or closed")
    if previous is not None and previous.get("thesis_status") == "closed" and status != "closed":
        raise RecordError("A closed thesis cannot be reopened; create a new thesis and reference it")
    if status == "closed":
        if custom.get("close_reason") not in CLOSE_REASONS:
            raise RecordError("custom.close_reason must be validated, falsified, expired or abandoned")
    elif "close_reason" in custom:
        raise RecordError("custom.close_reason requires thesis_status closed")


def _reply(custom, previous, topic):
    kind = custom.get("kind")
    if previous is not None and kind != previous.get("kind"):
        raise RecordError("custom.kind cannot change")
    if kind is None:
        return
    if kind not in REPLY_KINDS:
        raise RecordError("custom.kind must be checkpoint or evidence")
    if previous is None and topic["type"] != "thesis":
        raise RecordError("Checkpoint and evidence replies belong to thesis topics")
    if kind == "checkpoint":
        status = _required(custom, "status", previous, "pending")
        if status not in CHECKPOINT_STATUSES:
            raise RecordError("custom.status must be pending, confirmed, refuted or expired")
        if previous is not None and previous.get("status") != "pending" and status != previous.get("status"):
            raise RecordError("A resolved checkpoint cannot change status")
        due_date = custom.get("due_date")
        if not isinstance(due_date, str) or not _is_date(due_date):
            raise RecordError("custom.due_date must be an ISO date YYYY-MM-DD")
    elif custom.get("stance") not in STANCES:
        raise RecordError("custom.stance must be support, against or neutral")


def _is_date(value: str) -> bool:
    try:
        return len(value) == 10 and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False
