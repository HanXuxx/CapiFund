"""通过远程 MCP 端到端验收 CapiFund：在临时实例上逐个调用全部 fund_* 工具，并用接入包中的 client.py 复核。

覆盖认证、工具发现、写入与请求防重、修订冲突、待办、只读网页，以及运行目录中不出现 token。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from instance import (DEFAULT_CONFIG, SHANGHAI, Instance, leaked_files, load_config, log, new_run_dir, start_instance,
                      write_report)

FUND_TOOLS = frozenset({"fund_protocol", "fund_tag_tree", "fund_agenda", "fund_query_records", "fund_read_record",
                        "fund_read_topic", "fund_record_history", "fund_create_record", "fund_update_record"})
AUTHOR = {"id": "capifund-acceptance", "type": "agent", "runtime": "capifund-example", "name": "CapiFund 验收进程"}


def client_env(package: Path) -> dict[str, str]:
    """按 README 的 `set -a; . ./client.env` 方式载入包内地址和 token。"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("CAPIFUND_")}
    for line in (package / "client.env").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        env[key] = value
    return env


class Acceptance:
    def __init__(self, instance: Instance, run_dir: Path, session: ClientSession, web: httpx.AsyncClient):
        self.instance, self.run_dir, self.session, self.web = instance, run_dir, session, web
        self.checks: list[dict[str, Any]] = []
        self.covered: set[str] = set()
        self.state: dict[str, Any] = {}
        self.today = datetime.now(SHANGHAI).date().isoformat()

    async def check(self, phase: str, name: str, function: Callable[[], Awaitable[str]]) -> bool:
        started = time.monotonic()
        try:
            detail, ok = await function(), True
        except Exception as exc:  # 单项失败只记入报告，继续执行后续检查
            detail, ok = f"{type(exc).__name__}: {exc}", False
        elapsed = round(time.monotonic() - started, 2)
        self.checks.append({"phase": phase, "name": name, "ok": ok, "detail": detail, "seconds": elapsed})
        log(f"{'PASS' if ok else 'FAIL'} {phase}/{name} ({elapsed}s): {detail}")
        return ok

    async def call(self, tool: str, arguments: dict) -> dict:
        result = await self.session.call_tool(tool, arguments)
        if result.isError:
            raise AssertionError(f"{tool}: {' '.join(item.text for item in result.content if item.type == 'text')}")
        self.covered.add(tool)
        return result.structuredContent

    # 认证与发现

    async def auth_rejected(self) -> str:
        results = []
        for headers in ({}, {"Authorization": "Bearer wrong-token-for-acceptance"}):
            for method, path in (("POST", "/mcp"), ("GET", "/api/v1/protocol")):
                response = await self.web.request(method, path, headers=headers, json={} if method == "POST" else None)
                results.append(response.status_code)
                if response.status_code != 401:
                    raise AssertionError(f"{method} {path} with {list(headers) or 'no header'} returned {response.status_code}")
        return f"/mcp and /api/v1 return {sorted(set(results))} without or with a wrong token"

    async def web_without_token(self) -> str:
        response = await self.web.get("/")
        if response.status_code != 200:
            raise AssertionError(f"/ returned {response.status_code}")
        return "read-only web page needs no token"

    async def tool_discovery(self) -> str:
        visible = {tool.name for tool in (await self.session.list_tools()).tools}
        if visible != FUND_TOOLS:
            raise AssertionError(f"missing={sorted(FUND_TOOLS - visible)} unexpected={sorted(visible - FUND_TOOLS)}")
        return f"{len(visible)} fund_* tools"

    # 读写流程

    async def protocol(self) -> str:
        result = await self.call("fund_protocol", {})
        if result["protocol"] != "1.0":
            raise AssertionError(f"protocol {result['protocol']}")
        return f"protocol {result['protocol']}, database version {result['database_version']}"

    async def tag_tree(self) -> str:
        return f"tag tree: {json.dumps(await self.call('fund_tag_tree', {}), ensure_ascii=False)[:120]}"

    async def agenda_empty(self) -> str:
        result = await self.call("fund_agenda", {"as_of": self.today})
        counts = (result["due_checkpoints"]["total"], result["theses_with_new_evidence"]["total"])
        if counts != (0, 0):
            raise AssertionError(f"agenda of a new instance is not empty: {counts}")
        return "agenda is empty"

    def thesis_metadata(self, status: str) -> dict:
        return {"type": "thesis", "title": "验收逻辑：远程 MCP 读写链路", "tags": ["验收/CapiFund"],
                "custom": {"thesis_status": status, "basket": {"long": ["600519.SH"]}, "acceptance_run": self.run_dir.name}}

    async def thesis_create(self) -> str:
        arguments = {"metadata": self.thesis_metadata("draft"), "body": "本条由 CapiFund 验收进程写入，只用于验证远程读写，不是研究结论。",
                     "request_id": f"{self.run_dir.name}-thesis", "author": AUTHOR}
        self.state["thesis_arguments"] = arguments
        record = await self.call("fund_create_record", arguments)
        self.state["thesis"] = record["metadata"]["id"]
        return f"{self.state['thesis']} revision {record['revision']}"

    async def thesis_retry(self) -> str:
        record = await self.call("fund_create_record", self.state["thesis_arguments"])
        if (record["metadata"]["id"], record["revision"]) != (self.state["thesis"], 1):
            raise AssertionError(f"retry returned {record['metadata']['id']} revision {record['revision']}")
        return "same request_id returned the original record"

    async def checkpoint_create(self) -> str:
        record = await self.call("fund_create_record", {
            "metadata": {"type": "comment", "topic_id": self.state["thesis"], "title": "验收节点：当天到期",
                         "custom": {"kind": "checkpoint", "due_date": self.today}},
            "body": "到期当天应出现在待办的到期验证节点清单中。", "request_id": f"{self.run_dir.name}-checkpoint", "author": AUTHOR})
        self.state["checkpoint"] = record["metadata"]["id"]
        return f"{self.state['checkpoint']} due {self.today}"

    async def thesis_update(self) -> str:
        record = await self.call("fund_update_record", {
            "record_id": self.state["thesis"], "metadata": {"custom": self.thesis_metadata("active")["custom"]},
            "expected_revision": 1, "request_id": f"{self.run_dir.name}-thesis-activate", "author": AUTHOR,
            "body": "验收逻辑转为 active：后续证据应进入待复核清单。"})
        if record["revision"] != 2 or record["metadata"]["custom"]["thesis_status"] != "active":
            raise AssertionError(f"revision {record['revision']} status {record['metadata']['custom']['thesis_status']}")
        return "thesis active at revision 2"

    async def thesis_stale_update(self) -> str:
        result = await self.session.call_tool("fund_update_record", {
            "record_id": self.state["thesis"], "metadata": {"title": "不应写入的标题"}, "expected_revision": 1,
            "request_id": f"{self.run_dir.name}-thesis-stale", "author": AUTHOR})
        text = " ".join(item.text for item in result.content if item.type == "text")
        if not result.isError or not text.startswith("revision_conflict"):
            raise AssertionError(f"stale revision was not rejected: {text[:200]}")
        return text[:120]

    async def evidence_create(self) -> str:
        record = await self.call("fund_create_record", {
            "metadata": {"type": "comment", "topic_id": self.state["thesis"], "title": "验收证据：远程写入可读回",
                         "custom": {"kind": "evidence", "stance": "neutral"}},
            "body": "来源：本验收进程。只用于验证证据写入后进入待复核清单。", "request_id": f"{self.run_dir.name}-evidence", "author": AUTHOR})
        self.state["evidence"] = record["metadata"]["id"]
        return self.state["evidence"]

    async def query_records(self) -> str:
        result = await self.call("fund_query_records", {"type": "thesis", "custom": {"acceptance_run": self.run_dir.name}})
        ids = [item["metadata"]["id"] for item in result["items"]]
        if ids != [self.state["thesis"]]:
            raise AssertionError(f"query returned {ids}")
        return "custom filter returns the thesis"

    async def read_record(self) -> str:
        current = await self.call("fund_read_record", {"record_id": self.state["thesis"]})
        first = await self.call("fund_read_record", {"record_id": self.state["thesis"], "revision": 1})
        if current["revision"] != 2 or first["body"] != self.state["thesis_arguments"]["body"]:
            raise AssertionError(f"current revision {current['revision']}; revision 1 body changed")
        return "current revision 2; revision 1 body preserved"

    async def read_topic(self) -> str:
        result = await self.call("fund_read_topic", {"record_id": self.state["evidence"]})
        kinds = sorted(reply["metadata"]["custom"]["kind"] for reply in result["replies"])
        if result["topic"]["metadata"]["id"] != self.state["thesis"] or kinds != ["checkpoint", "evidence"]:
            raise AssertionError(f"topic {result['topic']['metadata']['id']} replies {kinds}")
        return "reply id resolves to the thesis with checkpoint and evidence"

    async def record_history(self) -> str:
        result = await self.call("fund_record_history", {"record_id": self.state["thesis"]})
        if result["total"] != 2:
            raise AssertionError(f"history total {result['total']}")
        return "2 revisions"

    async def agenda_items(self) -> str:
        result = await self.call("fund_agenda", {"as_of": self.today})
        due = {item["metadata"]["id"] for item in result["due_checkpoints"]["items"]}
        review = {item["metadata"]["id"] for item in result["theses_with_new_evidence"]["items"]}
        if self.state["checkpoint"] not in due or self.state["thesis"] not in review:
            raise AssertionError(f"due={sorted(due)} review={sorted(review)}")
        return "checkpoint is due and thesis awaits review"

    # 接入包 client.py 与网页

    async def client_py(self) -> str:
        package = self.instance.client_py.parent
        env = {**client_env(package), "CAPIFUND_AUTHOR_ID": AUTHOR["id"], "CAPIFUND_AUTHOR_RUNTIME": AUTHOR["runtime"]}
        output_dir = self.run_dir / "client"
        output_dir.mkdir()
        outputs = {}
        for name, arguments in (("doctor", ["doctor"]), ("doctor-mcp", ["doctor", "--mcp"]), ("agenda", ["agenda"]),
                                ("query", ["query", "--type", "thesis"])):
            process = await anyio.run_process([sys.executable, str(self.instance.client_py), *arguments], env=env, check=False)
            (output_dir / f"{name}.json").write_bytes(process.stdout + process.stderr)
            if process.returncode != 0:
                raise AssertionError(f"client.py {' '.join(arguments)} exited {process.returncode}: {process.stderr.decode()[:200]}")
            outputs[name] = json.loads(process.stdout)
        if outputs["doctor-mcp"]["mcp"]["protocol"] != "1.0":
            raise AssertionError(f"doctor --mcp: {outputs['doctor-mcp']}")
        if self.state["checkpoint"] not in json.dumps(outputs["agenda"]) or self.state["thesis"] not in json.dumps(outputs["query"]):
            raise AssertionError("client.py agenda or query does not show the new records")
        return "doctor, doctor --mcp, agenda and query succeeded with client.env"

    async def web_pages(self) -> str:
        expected = {"/": "验收逻辑：远程 MCP 读写链路", "/agenda": "验收节点：当天到期",
                    f"/records/{self.state['thesis']}": "验收证据：远程写入可读回"}
        for path, text in expected.items():
            response = await self.web.get(path)
            if response.status_code != 200 or text not in response.text:
                raise AssertionError(f"{path} returned {response.status_code} without {text!r}")
        return "/, /agenda and /records/{id} show the new records"

    async def tool_coverage(self) -> str:
        missing = FUND_TOOLS - self.covered
        if missing:
            raise AssertionError(f"tools without a passing call: {sorted(missing)}")
        return f"{len(self.covered)} tools covered"

    async def run(self) -> None:
        for phase, name, function in (
            ("Auth", "auth_rejected", self.auth_rejected), ("Auth", "web_without_token", self.web_without_token),
            ("MCP", "tool_discovery", self.tool_discovery), ("MCP", "fund_protocol", self.protocol),
            ("MCP", "fund_tag_tree", self.tag_tree), ("MCP", "agenda_empty", self.agenda_empty),
        ):
            await self.check(phase, name, function)
        # 写入步骤依赖前一步的 ID，失败即停止后续写入。
        for name, function in (("thesis_create", self.thesis_create), ("thesis_retry", self.thesis_retry),
                               ("checkpoint_create", self.checkpoint_create), ("thesis_update", self.thesis_update),
                               ("thesis_stale_update", self.thesis_stale_update), ("evidence_create", self.evidence_create)):
            if not await self.check("MCP", name, function):
                break
        else:
            for phase, name, function in (
                ("MCP", "fund_query_records", self.query_records), ("MCP", "fund_read_record", self.read_record),
                ("MCP", "fund_read_topic", self.read_topic), ("MCP", "fund_record_history", self.record_history),
                ("MCP", "agenda_items", self.agenda_items), ("Client", "client_py", self.client_py), ("Web", "web_pages", self.web_pages),
            ):
                await self.check(phase, name, function)
        await self.check("MCP", "tool_coverage", self.tool_coverage)


async def run_acceptance(instance: Instance, run_dir: Path) -> list[dict]:
    headers = {"Authorization": "Bearer " + instance.token}
    async with httpx.AsyncClient(base_url=instance.origin, timeout=30, trust_env=False) as web, \
            httpx.AsyncClient(headers=headers, timeout=30, trust_env=False) as client, \
            streamable_http_client(instance.mcp_url, http_client=client) as (read, write, _), \
            ClientSession(read, write) as session:
        await session.initialize()
        acceptance = Acceptance(instance, run_dir, session, web)
        await acceptance.run()
        return acceptance.checks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    run_dir = new_run_dir(load_config(args.config))
    log(f"Run directory: {run_dir}")
    with start_instance(run_dir) as instance:
        checks = anyio.run(run_acceptance, instance, run_dir)
    leaked = leaked_files(run_dir, instance.token)
    checks.append({"phase": "Output", "name": "no_token_in_outputs", "ok": not leaked, "seconds": 0,
                   "detail": f"token found in {leaked}" if leaked else "token absent from run outputs"})
    log(f"{'PASS' if not leaked else 'FAIL'} Output/no_token_in_outputs")
    passed = write_report(run_dir, "CapiFund 远程 MCP 验收报告", instance.mcp_url, checks, {},
                          "临时实例数据位于 `capifund/`，服务日志为 `capifund-server.log`，client.py 输出位于 `client/`。")
    log(f"{'PASSED' if passed else 'FAILED'}; report: {run_dir / 'report.md'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
