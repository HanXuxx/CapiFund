"""通过端口验收 CapiFinMCP，并把取数结果作为证据写入测试用 CapiFund 实例。

阶段 A 调用正在运行的 CapiFinMCP（认证、工具发现、各入口只读查询、文件下载）；
阶段 B 在空闲端口启动独立 CapiFund 子进程，经 HTTP 写入 thesis、checkpoint、evidence 并核对待办。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml
from capifin_mcp_client import CapiFinClient, CapiFinClientError

from capifund.client import FundClient

SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_CONFIG = Path(__file__).with_name("config.yaml.example")
# 远程入口必须公开的代表性工具，以及必须被正文读取替换掉的工具。
REQUIRED_TOOLS = {
    "gts_local_a_share_resolve", "gts_local_a_share_query", "gts_local_a_share_discover", "gts_local_a_share_snapshot",
    "gts_local_us_stock_query", "gts_local_us_stock_snapshot", "gts_local_index_resolve", "gts_local_index_snapshot",
    "gts_local_indicator", "gts_local_theme", "msci_cnelefmtr", "get_a_share_research_report_digests",
    "tl_meeting_minutes_resolve", "tl_meeting_minutes_list", "download_source_file",
}
EXCLUDED_TOOLS = {
    "get_a_share_research_report_documents", "get_us_stock_research_report_documents",
    "gts_local_a_share_announcement_document", "gts_api_private", "tl_meeting_minutes_get", "tl_meeting_minutes_document",
}
PASS_STATUSES = {"success", "rows", "resolved", "available", "ready"}
AUTHOR = {"id": "capifinmcp-acceptance", "type": "agent", "runtime": "capifund-example", "name": "CapiFinMCP 验收进程"}


def log(message: str) -> None:
    print(f"[{datetime.now(SHANGHAI).strftime('%H:%M:%S')}] {message}", flush=True)


def load_token(config: dict) -> str:
    if os.environ.get("CAPIFIN_MCP_TOKEN"):
        return os.environ["CAPIFIN_MCP_TOKEN"]
    path = Path(config["token_file"]).expanduser()
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.strip().partition("=")
        if key == "CAPIFIN_MCP_TOKEN" and value:
            return value.strip().strip("\"'")
    raise ValueError(f"CAPIFIN_MCP_TOKEN not found in {path}")


def outcome(result: dict) -> str:
    """按 CapiFin 语义归类：只有 query_status=success 且 data_status=no_rows 才是成功无记录。"""
    data, meta = result.get("data"), result.get("metadata", {})
    data = data if isinstance(data, dict) else {}
    query_status = meta.get("query_status") or data.get("query_status")
    if query_status:
        if query_status != "success":
            return f"failed:{query_status}"
        return "no_rows" if (meta.get("data_status") or data.get("data_status")) == "no_rows" else "rows"
    status = data.get("status") or meta.get("status")
    if status in PASS_STATUSES:
        return "rows"
    return "no_rows" if status == "no_rows" else f"failed:{status}"


class Acceptance:
    def __init__(self, config: dict, run_dir: Path):
        self.config, self.run_dir = config, run_dir
        mcp = config["capifinmcp"]
        self.url, self.timeout = mcp["url"], float(mcp.get("timeout_seconds", 120))
        self.client = CapiFinClient(self.url, load_token(mcp), self.timeout)
        self.checks: list[dict[str, Any]] = []
        self.results: dict[str, dict] = {}
        self.queries: dict[str, dict] = {}

    def check(self, phase: str, name: str, function: Callable[[], str]) -> bool:
        started = time.monotonic()
        try:
            detail, ok = function(), True
        except Exception as exc:  # 单项失败只记入报告，继续执行后续检查
            detail, ok = f"{type(exc).__name__}: {exc}", False
        elapsed = round(time.monotonic() - started, 2)
        self.checks.append({"phase": phase, "name": name, "ok": ok, "detail": detail, "seconds": elapsed})
        log(f"{'PASS' if ok else 'FAIL'} {phase}/{name} ({elapsed}s): {detail}")
        return ok

    def query(self, key: str, tool: str, arguments: dict) -> str:
        result = self.client.call_tool(tool, arguments)
        state = outcome(result)
        self.results[key] = result
        self.queries[key] = {"tool": tool, "arguments": arguments, "summary": result["summary"], "outcome": state}
        if state.startswith("failed"):
            raise AssertionError(f"{tool} {state}; warnings={result['warnings'][:3]}")
        if state == "no_rows":
            raise AssertionError(f"{tool} returned no rows for a sample expected to have data")
        return f"{tool}: {result['summary'][:120]}"

    # 阶段 A：CapiFinMCP

    def auth_missing_token(self) -> str:
        response = httpx.post(self.url, json={}, timeout=10, trust_env=False)
        if response.status_code != 401:
            raise AssertionError(f"expected 401 without token, got {response.status_code}")
        return "401 without token"

    def auth_wrong_token(self) -> str:
        try:
            CapiFinClient(self.url, "wrong-token-for-acceptance", 30).list_tools()
        except CapiFinClientError as exc:
            if "authentication" not in str(exc):
                raise AssertionError(f"unexpected client error: {exc}") from None
            return str(exc)
        raise AssertionError("a wrong token was accepted")

    def tool_discovery(self) -> str:
        names = {tool["name"] for tool in self.client.list_tools()}
        missing, leaked = REQUIRED_TOOLS - names, EXCLUDED_TOOLS & names
        if missing or leaked:
            raise AssertionError(f"missing={sorted(missing)} leaked={sorted(leaked)}")
        return f"{len(names)} tools; required present, body readers excluded"

    def phase_a(self) -> None:
        samples = self.config["samples"]
        ticker = samples["a_share"]
        today = datetime.now(SHANGHAI).date()
        self.check("A", "auth_missing_token", self.auth_missing_token)
        self.check("A", "auth_wrong_token", self.auth_wrong_token)
        self.check("A", "tool_discovery", self.tool_discovery)

        def resolve_a_share():
            detail = self.query("a_share_resolve", "gts_local_a_share_resolve", {"query": samples["a_share_name"], "limit": 3})
            codes = {json.dumps(row, ensure_ascii=False) for row in self.results["a_share_resolve"]["data"]["rows"]}
            if not any(ticker in code for code in codes):
                raise AssertionError(f"{samples['a_share_name']} did not resolve to {ticker}")
            return detail
        self.check("A", "a_share_resolve", resolve_a_share)
        self.check("A", "a_share_snapshot", lambda: self.query(
            "a_share_snapshot", "gts_local_a_share_snapshot", {"view_id": "company_core", "entity": ticker, "limit_per_section": 3}))
        self.check("A", "a_share_discover", lambda: self.query(
            "a_share_discover", "gts_local_a_share_discover", {"operation": "search_capabilities", "query": "公告", "limit": 5}))
        self.check("A", "a_share_announcements", lambda: self.query(
            "a_share_announcements", "gts_local_a_share_query",
            {"capability_id": "event.disclosures_calendar_and_shareholder_meetings", "entity": ticker, "plan": "summary",
             "date_from": (today - timedelta(days=365)).isoformat(), "limit": 5}))
        self.check("A", "us_stock_snapshot", lambda: self.query(
            "us_stock_snapshot", "gts_local_us_stock_snapshot",
            {"view_id": "company_core", "entity": samples["us_stock"], "limit_per_section": 3}))

        def index_profile():
            self.query("index_resolve", "gts_local_index_resolve", {"query": samples["index_name"], "limit": 1})
            index_code = self.results["index_resolve"]["data"]["rows"][0]["index_code"]
            return self.query("index_snapshot", "gts_local_index_snapshot",
                              {"view_id": "index_profile", "entity": index_code, "limit_per_section": 3})
        self.check("A", "index_profile", index_profile)

        def indicator_series():
            self.query("indicator_search", "gts_local_indicator", {"operation": "search", "query": samples["indicator_query"], "limit": 3})
            file_id = self.results["indicator_search"]["data"]["result"]["candidates"][0]["file_id"]
            return self.query("indicator_series", "gts_local_indicator",
                              {"operation": "get_series", "indicator_ids": [file_id], "limit_per_indicator": 12})
        self.check("A", "indicator_series", indicator_series)
        self.check("A", "theme_search", lambda: self.query(
            "theme_search", "gts_local_theme", {"operation": "search", "query": samples["theme_query"], "limit": 3}))
        self.check("A", "msci_discover", lambda: self.query("msci_discover", "msci_cnelefmtr", {"operation": "discover"}))
        self.check("A", "research_digests", lambda: self.query(
            "research_digests", "get_a_share_research_report_digests", {"symbol": ticker, "query_mode": "latest_n", "limit": 3}))

        def minutes():
            self.query("minutes_resolve", "tl_meeting_minutes_resolve", {"query": samples["a_share_name"], "kind": "security", "limit": 3})
            candidate = self.results["minutes_resolve"]["data"]["candidates"][0]
            return self.query("minutes_list", "tl_meeting_minutes_list", {
                "security": {"ticker_symbol": candidate["TICKER_SYMBOL"], "market_tag": candidate["MARKET_TAG"]}, "limit": 3})
        self.check("A", "minutes_list", minutes)
        self.downloads()

    def download(self, source: str, document_ref: dict, filename: str) -> str:
        saved = self.client.download_file(source, document_ref, self.run_dir / "downloads" / filename)
        if saved["size_bytes"] <= 0:
            raise AssertionError(f"empty file: {saved['path']}")
        self.queries[f"download_{source}"] = {"tool": "download_source_file", "arguments": {"source": source, "document_ref": document_ref},
                                              "summary": f"{saved['size_bytes']} bytes {saved['media_type']}", "outcome": "rows"}
        return f"{saved['size_bytes']} bytes {saved['media_type']} -> {Path(saved['path']).name}"

    def downloads(self) -> None:
        def report():
            reports = self.results["research_digests"]["data"]["reports"]
            return self.download("tl_research_report", reports[0]["report_ref"], "research_report.pdf")

        def announcement():
            sources = self.results["a_share_announcements"]["data"]["sources"]
            row = next(source for source in sources if source["dataset"] == "ashareann_info")["result"]["rows"][0]
            return self.download("gts_a_share_announcement",
                                 {"record_id": row["id"], "ann_id": str(row["ann_id"]), "title": row["ann_title"]}, "announcement.pdf")

        def mom_id():
            return {"mom_id": str(self.results["minutes_list"]["data"]["items"][0]["mom_id"])}

        self.check("A", "download_research_report", report)
        self.check("A", "download_announcement", announcement)
        self.check("A", "download_meeting_transcript", lambda: self.download("tl_meeting_transcript", mom_id(), "meeting_transcript.txt"))
        self.check("A", "download_meeting_summary", lambda: self.download("tl_meeting_summary", mom_id(), "meeting_summary.txt"))

    # 阶段 B：CapiFund

    def phase_b(self) -> None:
        data_dir = self.run_dir / "capifund"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        common = ["--data-dir", str(data_dir), "--host", "127.0.0.1", "--port", str(port), "--public-origin", origin]
        subprocess.run([sys.executable, "-m", "capifund.admin", *common, "migrate-database", "--apply"],
                       check=True, capture_output=True, text=True)
        server_log = (self.run_dir / "capifund-server.log").open("w")
        server = subprocess.Popen([sys.executable, "-m", "capifund.cli", *common], stdout=server_log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if httpx.get(origin + "/healthz", timeout=2, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if server.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"CapiFund did not start; see {server_log.name}")
                time.sleep(0.2)
            log(f"CapiFund started at {origin} with data dir {data_dir}")
            self.fund_records(FundClient(origin, author=AUTHOR))
        finally:
            server.terminate()
            server.wait(10)
            server_log.close()

    def fund_records(self, fund: FundClient) -> None:
        samples, run_id = self.config["samples"], self.run_dir.name
        today = datetime.now(SHANGHAI).date().isoformat()
        state: dict[str, str] = {}

        def thesis():
            body = (f"本条由 CapiFinMCP 验收进程（run `{run_id}`）自动写入，只用于验证两层联动，不是研究结论。\n\n"
                    f"跟踪对象：{samples['a_share_name']}（{samples['a_share']}）。证据来自 CapiFinMCP 端口调用，见各 evidence 回复。")
            record = fund.create({"type": "thesis", "title": f"验收示例：{samples['a_share_name']} 资料链路", "tags": ["验收/CapiFinMCP"],
                                  "custom": {"thesis_status": "active", "basket": {"long": [samples["a_share"]]}, "acceptance_run": run_id}},
                                 body, f"{run_id}-thesis")
            state["thesis"] = record["metadata"]["id"]
            return state["thesis"]

        def checkpoint():
            record = fund.create({"type": "comment", "topic_id": state["thesis"], "title": "核验最新公告与研报是否改变跟踪判断",
                                  "custom": {"kind": "checkpoint", "due_date": today}},
                                 "验收示例节点：到期当天应出现在待办的到期验证节点清单中。", f"{run_id}-checkpoint")
            state["checkpoint"] = record["metadata"]["id"]
            return f"{state['checkpoint']} due {today}"

        def evidence():
            written = 0
            for key in ("a_share_snapshot", "a_share_announcements", "research_digests", "minutes_list", "download_tl_research_report"):
                query = self.queries.get(key)
                if not query or query["outcome"] != "rows":
                    continue
                body = (f"来源：CapiFinMCP 工具 `{query['tool']}`。\n\n参数：\n\n```json\n"
                        f"{json.dumps(query['arguments'], ensure_ascii=False, indent=2)}\n```\n\n结果摘要：{query['summary']}\n")
                fund.create({"type": "comment", "topic_id": state["thesis"], "title": f"资料：{query['tool']}",
                             "custom": {"kind": "evidence", "stance": "neutral", "source_tool": query["tool"]}},
                            body, f"{run_id}-evidence-{key}")
                written += 1
            if not written:
                raise AssertionError("no successful CapiFinMCP query to cite")
            return f"{written} evidence replies"

        def agenda():
            result = fund.agenda(as_of=today)
            due = {item["metadata"]["id"] for item in result["due_checkpoints"]["items"]}
            review = {item["metadata"]["id"] for item in result["theses_with_new_evidence"]["items"]}
            if state["checkpoint"] not in due or state["thesis"] not in review:
                raise AssertionError(f"agenda missing records: due={sorted(due)} review={sorted(review)}")
            return "checkpoint is due and thesis awaits review"

        for name, function in (("fund_thesis", thesis), ("fund_checkpoint", checkpoint), ("fund_evidence", evidence), ("fund_agenda", agenda)):
            if not self.check("B", name, function):
                break

    def write_report(self) -> bool:
        passed = all(check["ok"] for check in self.checks)
        report = {"run": self.run_dir.name, "capifinmcp_url": self.url, "passed": passed, "checks": self.checks, "queries": self.queries}
        (self.run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        lines = [f"# CapiFinMCP 验收报告 {self.run_dir.name}", "",
                 f"结论：{'全部通过' if passed else '存在失败项'}（{sum(c['ok'] for c in self.checks)}/{len(self.checks)}）。服务地址 `{self.url}`。", "",
                 "| 阶段 | 检查 | 结果 | 耗时（秒） | 说明 |", "| --- | --- | --- | --- | --- |"]
        for check in self.checks:
            detail = str(check["detail"]).replace("|", "\\|").replace("\n", " ")[:200]
            lines.append(f"| {check['phase']} | `{check['name']}` | {'通过' if check['ok'] else '失败'} | {check['seconds']} | {detail} |")
        lines += ["", "下载文件位于 `downloads/`，测试用 CapiFund 数据位于 `capifund/`，完整调用参数与摘要见 `report.json`。", ""]
        (self.run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
        return passed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    run_dir = Path(config["output_root"]).expanduser() / datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S")
    run_dir.mkdir(parents=True)
    log(f"Run directory: {run_dir}")
    acceptance = Acceptance(config, run_dir)
    acceptance.phase_a()
    acceptance.check("B", "fund_service", lambda: acceptance.phase_b() or "test CapiFund stopped")
    passed = acceptance.write_report()
    log(f"{'PASSED' if passed else 'FAILED'}; report: {run_dir / 'report.md'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
