"""record 修订、幂等、作者自报、投研保留键与待办规则。"""
from __future__ import annotations

import pytest

from capifund.errors import Conflict, NotFound, RecordError
from conftest import AGENT, OTHER


def thesis(write, **custom):
    return write("create", metadata={"type": "thesis", "title": "AI 冲击 SaaS", "custom": custom}, body="逻辑正文")


def reply(write, topic, custom, title="回复"):
    return write("create", metadata={"type": "comment", "topic_id": topic["metadata"]["id"], "title": title, "custom": custom}, body="内容")


def test_create_fills_server_fields_and_reserved_defaults(write, api):
    record = thesis(write, basket={"long": ["688111.SH"]})
    metadata = record["metadata"]
    assert record["revision"] == 1
    assert metadata["custom"] == {"basket": {"long": ["688111.SH"]}, "thesis_status": "draft"}
    assert (metadata["author"], metadata["author_id"], metadata["author_type"], metadata["runtime"]) == ("Agent A", "agent-a", "agent", "codex")
    assert metadata["protocol"] == "1.0" and metadata["status"] == "current"
    assert api.call("read", {"record_id": metadata["id"]}) == record


def test_request_retry_returns_original_and_reuse_with_new_content_conflicts(api):
    arguments = {"metadata": {"type": "idea", "title": "想法"}, "body": "正文", "request_id": "same", "author": AGENT}
    first = api.call("create", arguments)
    assert api.call("create", arguments) == first
    assert first["metadata"]["status"] == "open"
    with pytest.raises(Conflict):
        api.call("create", {**arguments, "body": "改了"})
    # 请求号按作者 ID 隔离。
    assert api.call("create", {**arguments, "author": OTHER})["metadata"]["id"] != first["metadata"]["id"]


def test_update_appends_revision_records_editor_and_checks_expected_revision(write, api):
    record = thesis(write)
    record_id = record["metadata"]["id"]
    updated = write("update", author=OTHER, record_id=record_id, expected_revision=1,
                    metadata={"title": "新标题", "custom": {"thesis_status": "active", "note": 1}})
    assert updated["revision"] == 2 and updated["body"] == "逻辑正文"
    assert updated["metadata"]["author_id"] == "agent-a"
    history = api.call("history", {"record_id": record_id})
    assert history["total"] == 2
    assert [item["editor"]["id"] for item in history["items"]] == ["agent-b", "agent-a"]
    assert api.call("read", {"record_id": record_id, "revision": 1}) == record
    with pytest.raises(Conflict):
        write("update", record_id=record_id, expected_revision=1, metadata={"title": "过期"})


def test_author_is_required_and_validated(api):
    base = {"metadata": {"type": "idea", "title": "x"}, "body": "x", "request_id": "r"}
    with pytest.raises(RecordError):
        api.call("create", base)
    for author in ({"id": "a", "type": "robot", "runtime": "x"}, {"id": "a b", "type": "agent", "runtime": "x"},
                   {"id": "a", "type": "agent"}, {"id": "a", "type": "agent", "runtime": "x", "role": "owner"}):
        with pytest.raises(RecordError):
            api.call("create", {**base, "author": author})


def test_server_managed_fields_are_rejected(write):
    with pytest.raises(RecordError):
        write("create", metadata={"type": "idea", "title": "x", "author": "伪造"}, body="x")
    record = thesis(write)
    with pytest.raises(RecordError):
        write("update", record_id=record["metadata"]["id"], expected_revision=1, metadata={"type": "idea"})


def test_thesis_close_requires_reason_and_is_terminal(write):
    record = thesis(write)
    record_id = record["metadata"]["id"]
    with pytest.raises(RecordError, match="close_reason"):
        write("update", record_id=record_id, expected_revision=1, metadata={"custom": {"thesis_status": "closed"}})
    with pytest.raises(RecordError, match="close_reason"):
        write("update", record_id=record_id, expected_revision=1, metadata={"custom": {"thesis_status": "active", "close_reason": "validated"}})
    closed = write("update", record_id=record_id, expected_revision=1,
                   metadata={"custom": {"thesis_status": "closed", "close_reason": "falsified"}})
    assert closed["metadata"]["custom"]["close_reason"] == "falsified"
    with pytest.raises(RecordError, match="reopened"):
        write("update", record_id=record_id, expected_revision=2, metadata={"custom": {"thesis_status": "active"}})


def test_update_must_carry_reserved_keys_explicitly(write):
    record = thesis(write)
    with pytest.raises(RecordError, match="thesis_status is required"):
        write("update", record_id=record["metadata"]["id"], expected_revision=1, metadata={"custom": {"note": "丢了状态"}})
    # 不替换 custom 时保留键不受影响。
    assert write("update", record_id=record["metadata"]["id"], expected_revision=1,
                 metadata={"title": "只改标题"})["metadata"]["custom"]["thesis_status"] == "draft"


def test_reserved_keys_belong_to_their_record_types(write):
    with pytest.raises(RecordError):
        write("create", metadata={"type": "idea", "title": "x", "custom": {"thesis_status": "active"}}, body="x")
    with pytest.raises(RecordError):
        write("create", metadata={"type": "idea", "title": "x", "custom": {"kind": "evidence"}}, body="x")
    idea = write("create", metadata={"type": "idea", "title": "x"}, body="x")
    with pytest.raises(RecordError, match="thesis topics"):
        reply(write, idea, {"kind": "evidence", "stance": "support"})
    # 不带 kind 的普通回复可以挂在任何主题下。
    assert reply(write, idea, {})["metadata"]["floor"] == 1


def test_checkpoint_rules(write):
    topic = thesis(write)
    with pytest.raises(RecordError, match="due_date"):
        reply(write, topic, {"kind": "checkpoint", "due_date": "2026-13-01"})
    checkpoint = reply(write, topic, {"kind": "checkpoint", "due_date": "2026-12-01"})
    record_id = checkpoint["metadata"]["id"]
    assert checkpoint["metadata"]["custom"]["status"] == "pending"
    with pytest.raises(RecordError, match="kind cannot change"):
        write("update", record_id=record_id, expected_revision=1, metadata={"custom": {"kind": "evidence", "stance": "support"}})
    write("update", record_id=record_id, expected_revision=1,
          metadata={"custom": {"kind": "checkpoint", "status": "refuted", "due_date": "2026-12-01"}})
    with pytest.raises(RecordError, match="resolved"):
        write("update", record_id=record_id, expected_revision=2,
              metadata={"custom": {"kind": "checkpoint", "status": "pending", "due_date": "2026-12-01"}})


def test_evidence_requires_stance_and_replies_form_timeline(write, api):
    topic = thesis(write)
    with pytest.raises(RecordError, match="stance"):
        reply(write, topic, {"kind": "evidence"})
    first = reply(write, topic, {"kind": "evidence", "stance": "support"}, "证据一")
    second = write("create", metadata={"type": "comment", "topic_id": topic["metadata"]["id"],
                                       "reply_to_id": first["metadata"]["id"], "custom": {"kind": "evidence", "stance": "against"}}, body="反驳")
    assert (first["metadata"]["floor"], second["metadata"]["floor"]) == (1, 2)
    assert second["metadata"]["title"] == "回复"
    timeline = api.call("topic", {"record_id": second["metadata"]["id"]})
    assert timeline["topic"]["metadata"]["id"] == topic["metadata"]["id"]
    assert [item["metadata"]["floor"] for item in timeline["replies"]] == [1, 2]
    with pytest.raises(NotFound):
        reply(write, {"metadata": {"id": "record-missing"}}, {})


def test_query_filters_by_custom_keyword_and_tag(write, api):
    active = write("create", metadata={"type": "thesis", "title": "算力资本开支", "tags": ["行业/计算机"],
                                       "custom": {"thesis_status": "active"}}, body="GPU 需求")
    thesis(write)
    found = api.call("query", {"type": "thesis", "custom": {"thesis_status": "active"}})
    assert [item["metadata"]["id"] for item in found["items"]] == [active["metadata"]["id"]]
    assert api.call("query", {"keyword": "gpu"})["total"] == 1
    assert api.call("query", {"tag": "行业"})["total"] == 1
    assert api.call("query", {"author": "agent-a"})["total"] == 2


def test_agenda_lists_due_checkpoints_and_theses_with_new_evidence(write, api):
    topic = write("create", metadata={"type": "thesis", "title": "跟踪中", "custom": {"thesis_status": "active"}}, body="x")
    due = reply(write, topic, {"kind": "checkpoint", "due_date": "2026-10-20"}, "已到期")
    reply(write, topic, {"kind": "checkpoint", "due_date": "2026-11-30"}, "未到期")
    closed = write("create", metadata={"type": "thesis", "title": "已关闭",
                                       "custom": {"thesis_status": "draft"}}, body="x")
    reply(write, closed, {"kind": "checkpoint", "due_date": "2026-10-01"})
    write("update", record_id=closed["metadata"]["id"], expected_revision=1,
          metadata={"custom": {"thesis_status": "closed", "close_reason": "abandoned"}})

    agenda = api.call("agenda", {"as_of": "2026-10-21"})
    assert [item["metadata"]["id"] for item in agenda["due_checkpoints"]["items"]] == [due["metadata"]["id"]]
    assert agenda["due_checkpoints"]["items"][0]["thesis"]["title"] == "跟踪中"
    assert api.call("agenda", {"as_of": "2026-10-21", "within_days": 60})["due_checkpoints"]["total"] == 2
    assert agenda["theses_with_new_evidence"]["total"] == 0

    reply(write, topic, {"kind": "evidence", "stance": "against"})
    reply(write, topic, {"kind": "evidence", "stance": "support"})
    pending = api.call("agenda", {"as_of": "2026-10-21"})["theses_with_new_evidence"]
    assert pending["total"] == 1 and pending["items"][0]["new_evidence"] == 2
    # 复核后更新逻辑主帖，离开待复核清单。
    write("update", record_id=topic["metadata"]["id"], expected_revision=1, metadata={}, body="复核：维持判断")
    assert api.call("agenda", {"as_of": "2026-10-21"})["theses_with_new_evidence"]["total"] == 0
    with pytest.raises(RecordError):
        api.call("agenda", {"as_of": "2026/10/21"})


def test_protocol_exposes_types_and_reserved_keys(api):
    protocol = api.call("protocol", {})
    assert protocol["protocol"] == "1.0" and "thesis" in protocol["types"]
    assert protocol["reserved"]["comment"]["kind"] == ["checkpoint", "evidence"]
    assert protocol["specification"].startswith("# CapiFund record 协议 1.0")


def test_unknown_operation_and_parameters_are_rejected(api):
    with pytest.raises(RecordError):
        api.call("delete", {})
    with pytest.raises(RecordError):
        api.call("read", {"record_id": "x", "extra": 1})
