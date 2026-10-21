"""业务层：record 查询、幂等、修订、投研保留键和待办规则。"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..errors import Conflict, NotFound, RecordError
from ..serialization import dumps
from .kinds import RESERVED, apply_reserved
from .ports import FundStorage, RecordHead
from .tags import canonical_tag, clean_tags, tree_for_counts

PROTOCOL = "1.0"
TYPES = ("thesis", "idea", "source", "conclusion", "feedback", "comment")
STATUSES = ("open", "current", "outdated")
EDITABLE = {"title", "status", "tags", "reference", "custom"}
AUTHOR_TYPES = ("human", "agent")
SORTS = ("latest", "oldest", "activity", "replies")
AGENDA_LIMIT = 200
SHANGHAI = timezone(timedelta(hours=8))
IDENTIFIER = re.compile(r"[^\s]{1,100}")


def moment():
    return datetime.now(SHANGHAI).isoformat(timespec="microseconds")


def clean_author(author: Any) -> dict[str, str]:
    """作者由客户端自报，服务端只检查格式并原样记录，不据此授权。"""
    if not isinstance(author, dict) or not {"id", "type", "runtime"} <= author.keys() or author.keys() - {"id", "type", "runtime", "name"}:
        raise RecordError("author requires id, type and runtime, with optional name")
    if not all(isinstance(value, str) for value in author.values()):
        raise RecordError("author fields must be strings")
    if not IDENTIFIER.fullmatch(author["id"]) or not IDENTIFIER.fullmatch(author["runtime"]):
        raise RecordError("author id and runtime must contain 1–100 non-whitespace characters")
    if author["type"] not in AUTHOR_TYPES:
        raise RecordError("author type must be human or agent")
    name = author.get("name", author["id"]).strip()
    if not name or len(name) > 80:
        raise RecordError("author name must contain 1–80 characters")
    return {"id": author["id"], "type": author["type"], "runtime": author["runtime"], "name": name}


def find_record(session, record_id) -> RecordHead:
    if not isinstance(record_id, str) or not record_id:
        raise RecordError("record_id must be a nonempty string")
    head = session.head(record_id)
    if head is None:
        raise NotFound("Record not found")
    return head


def snapshot(session, head, revision=None, *, body=True):
    result = session.version(head.id, revision or head.revision)
    if result is None:
        raise NotFound("Revision not found")
    if body:
        result["body"] = session.body(head.id, result["revision"])
    return result


def request_retry(session, author, request_id, fingerprint):
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        raise RecordError("request_id must contain 1–200 characters")
    previous = session.request(author["id"], request_id)
    if previous is None:
        return None
    if previous["request_hash"] != fingerprint:
        raise Conflict("request_id was already used for a different request")
    return snapshot(session, find_record(session, previous["record_id"]), previous["revision"])


def fingerprint(*parts):
    return hashlib.sha256(dumps(list(parts)).encode()).hexdigest()


class RecordService:
    def __init__(self, storage: FundStorage):
        self._storage = storage

    def protocol(self):
        with self._storage.transaction() as session:
            return {**session.protocol(), "types": list(TYPES), "reserved": RESERVED}

    def read(self, record_id, *, revision=None):
        if revision is not None and (type(revision) is not int or revision < 1):
            raise RecordError("revision must be a positive integer")
        with self._storage.transaction() as session:
            return snapshot(session, find_record(session, record_id), revision)

    def history(self, record_id, *, page=1, page_size=50):
        self._page(page, page_size)
        with self._storage.transaction() as session:
            head = find_record(session, record_id)
            items = [] if (page - 1) * page_size >= head.revision else session.history(head.id, page, page_size)
            return {"items": items, "page": page, "page_size": page_size, "total": head.revision}

    def topic(self, record_id, *, page=1, page_size=200):
        """读取主帖与当页按楼层排序的回复；传入回复 ID 时返回其所属主题。"""
        self._page(page, page_size)
        with self._storage.transaction() as session:
            head = find_record(session, record_id)
            if head.topic_id:
                head = find_record(session, head.topic_id)
            replies = session.topic_replies(head.id, page, page_size)
            return {"topic": snapshot(session, head), "replies": [snapshot(session, item) for item in replies["items"]],
                    "total": replies["total"], "page": page, "page_size": page_size}

    def tag_tree(self):
        with self._storage.transaction() as session:
            return {"tree": tree_for_counts(session.tag_counts())}

    def query(self, *, type=None, author=None, tag=None, topic_id=None, custom=None, keyword="", sort=None,
              page=1, page_size=50, include_body=False):
        self._page(page, page_size)
        if not all(value is None or isinstance(value, str) for value in (type, author, tag, topic_id)) \
                or not isinstance(keyword, str) or not isinstance(include_body, bool):
            raise RecordError("Invalid query filter type")
        if type is not None and type not in TYPES:
            raise RecordError("Unknown type")
        if custom is not None and not isinstance(custom, dict):
            raise RecordError("custom filter must be an object")
        if sort is not None and sort not in SORTS:
            raise RecordError("Unknown sort")
        if tag:
            try:
                tag = canonical_tag(tag)
            except ValueError as exc:
                raise RecordError(str(exc)) from exc
        filters = {"type": type, "author": author, "tag": tag, "topic_id": topic_id, "keyword": keyword.casefold(),
                   "custom": {key: dumps(value) for key, value in (custom or {}).items()}}
        with self._storage.transaction() as session:
            if topic_id:
                find_record(session, topic_id)
            matches = session.find_records(filters, sort, page, page_size)
            items = []
            for item in matches["items"]:
                record = {"metadata": item["metadata"], "revision": item["revision"]}
                if include_body:
                    record["body"] = session.body(item["id"], item["revision"])
                items.append(record)
            return {"items": items, "total": matches["total"], "page": page, "page_size": page_size}

    def agenda(self, *, as_of=None, within_days=0):
        """到期验证节点与有新证据待复核的逻辑；as_of 默认取 Asia/Shanghai 当天。"""
        if as_of is None:
            as_of = datetime.now(SHANGHAI).date().isoformat()
        try:
            day = date.fromisoformat(as_of) if isinstance(as_of, str) and len(as_of) == 10 else None
        except ValueError:
            day = None
        if day is None:
            raise RecordError("as_of must be an ISO date YYYY-MM-DD")
        if type(within_days) is not int or not 0 <= within_days <= 366:
            raise RecordError("within_days must be an integer between 0 and 366")
        cutoff = (day + timedelta(days=within_days)).isoformat()
        with self._storage.transaction() as session:
            return {"as_of": as_of, "cutoff": cutoff, "limit": AGENDA_LIMIT,
                    "due_checkpoints": session.due_checkpoints(cutoff, AGENDA_LIMIT),
                    "theses_with_new_evidence": session.theses_with_new_evidence(AGENDA_LIMIT)}

    @staticmethod
    def _page(page, page_size):
        if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 200:
            raise RecordError("page >= 1 and 1 <= page_size <= 200 required")

    @staticmethod
    def _validate(metadata, *, creating):
        allowed = EDITABLE | ({"type", "topic_id", "reply_to_id"} if creating else set())
        if not isinstance(metadata, dict) or set(metadata) - allowed:
            raise RecordError("Unknown or server-managed metadata field")
        result = json.loads(dumps(metadata))
        if "title" in result and (not isinstance(result["title"], str) or not result["title"].strip() or len(result["title"]) > 160):
            raise RecordError("title must contain 1–160 characters")
        if "status" in result and result["status"] not in STATUSES:
            raise RecordError("Unknown status")
        if "tags" in result:
            try:
                result["tags"] = list(clean_tags(result["tags"]))
            except ValueError as exc:
                raise RecordError(str(exc)) from exc
        if "reference" in result and (not isinstance(result["reference"], list)
                                       or not all(isinstance(value, str) and value for value in result["reference"])):
            raise RecordError("reference must be an array of IDs")
        if "custom" in result and not isinstance(result["custom"], dict):
            raise RecordError("custom must be a JSON object")
        return result

    @staticmethod
    def _body(body):
        if not isinstance(body, str) or not body.strip():
            raise RecordError("body must be nonempty Markdown text")
        return body

    @staticmethod
    def _resolve_relationships(session, metadata, record_type):
        if record_type != "comment":
            if "topic_id" in metadata or "reply_to_id" in metadata:
                raise RecordError("Only comments have relationship fields")
            return None, None, None
        topic = find_record(session, metadata.get("topic_id", ""))
        if topic.topic_id:
            raise RecordError("topic_id must identify a topic")
        target = find_record(session, metadata.get("reply_to_id") or topic.id)
        if target.id != topic.id and target.topic_id != topic.id:
            raise RecordError("reply_to_id belongs to a different topic")
        return topic.id, target.id, snapshot(session, topic, body=False)["metadata"]

    def create(self, *, metadata, body, request_id, author):
        author = clean_author(author)
        digest = fingerprint("create", metadata, body, author)
        metadata = self._validate(metadata, creating=True)
        body = self._body(body)
        record_type = metadata.get("type")
        if record_type not in TYPES or (record_type != "comment" and not metadata.get("title")):
            raise RecordError("type and title are required")
        with self._storage.transaction(write=True) as session:
            retry = request_retry(session, author, request_id, digest)
            if retry is not None:
                return retry
            record_id = "record-" + uuid.uuid4().hex
            topic_id, reply_to, topic = self._resolve_relationships(session, metadata, record_type)
            floor = None
            if record_type == "comment":
                floor = session.next_floor(topic_id)
                metadata.update(topic_id=topic_id, reply_to_id=reply_to, floor=floor)
                metadata.setdefault("title", "回复")
            stamp = moment()
            metadata = {"status": "open" if record_type == "idea" else "current", "tags": [], "reference": [], "custom": {},
                        **metadata, "id": record_id, "protocol": PROTOCOL, "author": author["name"], "author_type": author["type"],
                        "author_id": author["id"], "runtime": author["runtime"], "created_at": stamp, "updated_at": stamp}
            metadata = apply_reserved(metadata, previous=None, topic=topic)
            session.insert(RecordHead(record_id, author["id"], 1, record_type, topic_id, reply_to, floor), metadata)
            session.append(record_id, 1, metadata, body, author, request_id, digest, "create")
            return {"metadata": metadata, "revision": 1, "body": body}

    def update(self, record_id, *, metadata, expected_revision, request_id, author, body=None):
        author = clean_author(author)
        digest = fingerprint("update", record_id, metadata, body, expected_revision, author)
        patch = self._validate(metadata, creating=False)
        with self._storage.transaction(write=True) as session:
            head = find_record(session, record_id)
            retry = request_retry(session, author, request_id, digest)
            if retry is not None:
                return retry
            if type(expected_revision) is not int or expected_revision < 1:
                raise RecordError("expected_revision must be a positive integer")
            if expected_revision != head.revision:
                raise Conflict(f"Revision conflict; current revision is {head.revision}")
            previous = snapshot(session, head)
            merged = {**previous["metadata"], **patch, "protocol": PROTOCOL, "updated_at": moment()}
            merged = apply_reserved(merged, previous=previous["metadata"]["custom"], topic=None)
            body = previous["body"] if body is None else self._body(body)
            revision = head.revision + 1
            session.append(head.id, revision, merged, body, author, request_id, digest, "update")
            return {"metadata": merged, "revision": revision, "body": body}
