"""同一 ASGI 服务的 HTTP、官方 MCP 客户端与只读网页端到端验收。"""
from __future__ import annotations

import io
import zipfile

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from capifund.asgi import create_app
from capifund.config import FundConfig
from conftest import AGENT, OTHER

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(paths):
    app = create_app(FundConfig(paths.data_dir, "127.0.0.1", 8794, "http://localhost"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as http:
            yield http


async def test_mcp_and_http_share_records_and_rules(client):
    async with streamable_http_client("http://localhost/mcp", http_client=client) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert {"fund_agenda", "fund_create_record", "fund_update_record", "fund_read_topic"} <= tools
            arguments = {"metadata": {"type": "thesis", "title": "MCP 逻辑", "custom": {"自由": [1, None]}},
                         "body": "正文", "request_id": "mcp-create", "author": AGENT}
            created = await session.call_tool("fund_create_record", arguments)
            assert not created.isError, created
            record = created.structuredContent
            record_id = record["metadata"]["id"]
            assert (await client.get("/api/v1/records/" + record_id)).json() == record
            assert (await session.call_tool("fund_create_record", arguments)).structuredContent == record

            changed = await client.patch("/api/v1/records/" + record_id, json={
                "metadata": {"custom": {"thesis_status": "active"}}, "expected_revision": 1,
                "request_id": "http-edit", "author": OTHER})
            assert changed.status_code == 200, changed.text
            history = await session.call_tool("fund_record_history", {"record_id": record_id})
            assert [item["editor"]["id"] for item in history.structuredContent["items"]] == ["agent-b", "agent-a"]

            evidence = await client.post("/api/v1/records", json={
                "metadata": {"type": "comment", "topic_id": record_id, "custom": {"kind": "evidence", "stance": "support"}},
                "body": "证据", "request_id": "http-evidence", "author": OTHER})
            assert evidence.status_code == 201, evidence.text
            agenda = await session.call_tool("fund_agenda", {"as_of": "2026-10-21"})
            assert agenda.structuredContent["theses_with_new_evidence"]["total"] == 1

            invalid = await session.call_tool("fund_update_record", {"record_id": record_id, "metadata": {"custom": {}},
                                                                     "expected_revision": 2, "request_id": "bad", "author": AGENT})
            assert invalid.isError and invalid.content[0].text.startswith("invalid_request:")
            wrong_type = await session.call_tool("fund_read_record", {"record_id": record_id, "revision": "1"})
            assert wrong_type.isError
            prompt = await session.get_prompt("capifund_workflow")
            assert "CapiFund Agent 工作手册" in prompt.messages[0].content.text


async def test_http_errors_use_codes(client):
    missing = await client.get("/api/v1/records/record-missing")
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    no_json = await client.post("/api/v1/records", content=b"{}", headers={"content-type": "text/plain"})
    assert no_json.status_code == 400
    no_author = await client.post("/api/v1/records", json={"metadata": {"type": "idea", "title": "x"}, "body": "x", "request_id": "r"})
    assert no_author.status_code == 400 and no_author.json()["code"] == "invalid_request"
    assert (await client.get("/api/v1/records", params={"page": "x"})).status_code == 400


async def test_web_is_read_only_and_escapes_content(client):
    created = await client.post("/api/v1/records", json={
        "metadata": {"type": "thesis", "title": "<script>alert(1)</script>", "custom": {"thesis_status": "active"}},
        "body": "<b>raw</b> **粗体**", "request_id": "web", "author": AGENT})
    record_id = created.json()["metadata"]["id"]
    await client.post("/api/v1/records", json={
        "metadata": {"type": "comment", "topic_id": record_id, "custom": {"kind": "checkpoint", "due_date": "2026-10-01"}},
        "body": "节点", "request_id": "web-checkpoint", "author": AGENT})
    for path in ("/", "/?status=active", "/records", "/records?type=thesis", "/agenda", "/records/" + record_id):
        page = await client.get(path)
        assert page.status_code == 200, path
        assert "<script>alert" not in page.text
    timeline = (await client.get("/records/" + record_id)).text
    assert "&lt;b&gt;raw&lt;/b&gt;" in timeline and "<strong>粗体</strong>" in timeline and "验证节点" in timeline
    assert record_id in (await client.get("/agenda")).text
    for path in ("/", "/records", "/records/" + record_id, "/agenda"):
        assert (await client.post(path)).status_code == 405
    assert (await client.get("/static/fund.css")).status_code == 200


async def test_bundle_and_health(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    response = await client.get("/client.zip")
    names = set(zipfile.ZipFile(io.BytesIO(response.content)).namelist())
    assert {"client.py", "SKILL.md", "README.md", "AGENTS.md", "protocol-1.0.md", "mcp.json", "connection.json",
            "thesis.json.example"} <= names
    readme = zipfile.ZipFile(io.BytesIO(response.content)).read("README.md").decode()
    assert "http://localhost" in readme and "@@FUND_URL@@" not in readme


async def test_client_py_talks_to_running_service(paths):
    """client.py 使用标准库发起真实 HTTP 请求，这里在本地端口启动服务。"""
    import socket
    import threading
    import uvicorn
    from capifund.client import FundClient

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = create_app(FundConfig(paths.data_dir, "127.0.0.1", port, f"http://127.0.0.1:{port}"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        with anyio.fail_after(10):
            while not server.started:
                await anyio.sleep(0.05)
        fund = FundClient(f"http://127.0.0.1:{port}", author=AGENT)
        created = await anyio.to_thread.run_sync(lambda: fund.create({"type": "thesis", "title": "客户端"}, "正文", "client-1"))
        assert (await anyio.to_thread.run_sync(lambda: fund.read(created["metadata"]["id"]))) == created
        doctor = await anyio.to_thread.run_sync(lambda: fund.doctor(mcp=False))
        assert doctor["protocol"] == "1.0" and doctor["author"]["id"] == "agent-a"
        with pytest.raises(ValueError, match="HTTP 409"):
            await anyio.to_thread.run_sync(lambda: fund.create({"type": "thesis", "title": "改了"}, "正文", "client-1"))
    finally:
        server.should_exit = True
        await anyio.to_thread.run_sync(thread.join, 10)
