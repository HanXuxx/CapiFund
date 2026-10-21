"""通过端口验收 CapiFinMCP，并把取数结果作为证据写入测试用 CapiFund 实例。

阶段 A 调用正在运行的 CapiFinMCP：认证、工具发现、除 gts_api_* 外每个远程工具的只读调用、反向检查和文件下载；
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
# Gangtise 外部工具会消耗外部额度，验收和 Agent 测试都不调用。
GTS_API_TOOLS = frozenset({"gts_api_agent", "gts_api_data", "gts_api_file", "gts_api_kb"})
# HTTP 入口不得公开的工具：服务端正文读取、私有数据，以及已从 MCP 移除的公司研究提交、状态和取消。
REMOVED_TOOLS = frozenset({
    "get_a_share_research_report_documents", "get_us_stock_research_report_documents",
    "gts_local_a_share_announcement_document", "gts_api_private", "tl_meeting_minutes_get", "tl_meeting_minutes_document",
    "submit_company_research", "get_company_research_status", "cancel_company_research",
})
AUTHOR = {"id": "capifinmcp-acceptance", "type": "agent", "runtime": "capifund-example", "name": "CapiFinMCP 验收进程"}


def log(message: str) -> None:
    print(f"[{datetime.now(SHANGHAI).strftime('%H:%M:%S')}] {message}", flush=True)


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


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
    """只有 query_status=success 且 data_status=no_rows 才是成功无记录。"""
    meta = result.get("metadata", {})
    if meta.get("query_status") != "success":
        return f"failed:{meta.get('query_status')}"
    return "no_rows" if meta.get("data_status") == "no_rows" else "rows"


def new_run_dir(config: dict, suffix: str = "") -> Path:
    run_dir = Path(config["output_root"]).expanduser() / (datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S") + suffix)
    run_dir.mkdir(parents=True)
    return run_dir


class Acceptance:
    def __init__(self, config: dict, run_dir: Path):
        self.config, self.run_dir = config, run_dir
        mcp = config["capifinmcp"]
        self.url, self.timeout = mcp["url"], float(mcp.get("timeout_seconds", 120))
        self.client = CapiFinClient(self.url, load_token(mcp), self.timeout)
        self.checks: list[dict[str, Any]] = []
        self.results: dict[str, dict] = {}
        self.queries: dict[str, dict] = {}
        self.visible: set[str] = set()
        self.covered: set[str] = set()

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

    def query(self, key: str, tool: str, arguments: dict, *, expect: str = "rows") -> str:
        result = self.client.call_tool(tool, arguments)
        state = outcome(result)
        self.results[key] = result
        self.queries[key] = {"tool": tool, "arguments": arguments, "summary": result["summary"], "outcome": state}
        if state != expect:
            raise AssertionError(f"{tool} returned {state}, expected {expect}; warnings={result['warnings'][:3]}")
        self.covered.add(tool)
        return f"{tool}: {state}"

    def dataset_query(self, key: str, scope: str, dataset: str, **arguments) -> str:
        """先 describe_dataset 取得 contract_token，再调用同一入口的 dataset_query。"""
        self.query(f"{key}_describe", f"gts_local_{scope}_discover", {"operation": "describe_dataset", "dataset": dataset})
        contract = self.results[f"{key}_describe"]["data"]["dataset"]
        return self.query(key, f"gts_local_{scope}_dataset_query", {
            "dataset": dataset, "contract_token": contract["contract_token"], "fields": contract["default_fields"][:5],
            "limit": 3, **arguments})

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
        self.visible = {tool["name"] for tool in self.client.list_tools()}
        leaked = REMOVED_TOOLS & self.visible
        if leaked or "download_source_file" not in self.visible:
            raise AssertionError(f"leaked={sorted(leaked)} download_source_file={'download_source_file' in self.visible}")
        return f"{len(self.visible)} tools; removed tools absent"

    def removed_tools_rejected(self) -> str:
        accepted = []
        for tool in sorted(REMOVED_TOOLS):
            try:
                self.client.call_tool(tool, {})
                accepted.append(tool)
            except CapiFinClientError as exc:
                if "Unregistered tool" not in str(exc):
                    accepted.append(f"{tool}: {exc}")
        if accepted:
            raise AssertionError(f"not rejected as unregistered: {accepted}")
        return f"{len(REMOVED_TOOLS)} removed tools raise CapiFinClientError"

    def tool_coverage(self) -> str:
        missing = self.visible - GTS_API_TOOLS - self.covered
        if missing:
            raise AssertionError(f"visible tools without a passing check: {sorted(missing)}")
        return f"{len(self.covered)} tools covered; exempt: {sorted(GTS_API_TOOLS & self.visible)}"

    def phase_a(self) -> None:
        s = self.config["samples"]
        ticker, today = s["a_share"], datetime.now(SHANGHAI).date()
        state: dict[str, Any] = {}
        self.check("A", "auth_missing_token", self.auth_missing_token)
        self.check("A", "auth_wrong_token", self.auth_wrong_token)
        self.check("A", "tool_discovery", self.tool_discovery)
        self.check("A", "removed_tools_rejected", self.removed_tools_rejected)

        def a_share_resolve():
            detail = self.query("a_share_resolve", "gts_local_a_share_resolve", {"query": s["a_share_name"], "limit": 3})
            if ticker not in json.dumps(self.results["a_share_resolve"]["data"]["rows"], ensure_ascii=False):
                raise AssertionError(f"{s['a_share_name']} did not resolve to {ticker}")
            return detail

        def index_resolve():
            detail = self.query("index_resolve", "gts_local_index_resolve", {"query": s["index_name"], "limit": 1})
            state["index_id"] = self.results["index_resolve"]["data"]["rows"][0]["index_id"]
            return f"{detail}; index_id={state['index_id']}"

        def indicator_series():
            self.query("indicator_search", "gts_local_indicator", {"operation": "search", "query": s["indicator_query"], "limit": 3})
            file_id = self.results["indicator_search"]["data"]["result"]["candidates"][0]["file_id"]
            return self.query("indicator_series", "gts_local_indicator",
                              {"operation": "get_series", "indicator_ids": [file_id], "limit_per_indicator": 12})

        def minutes():
            self.query("minutes_resolve", "tl_meeting_minutes_resolve", {"query": s["a_share_name"], "kind": "security", "limit": 3})
            candidate = self.results["minutes_resolve"]["data"]["candidates"][0]
            return self.query("minutes_list", "tl_meeting_minutes_list", {
                "security": {"ticker_symbol": candidate["TICKER_SYMBOL"], "market_tag": candidate["MARKET_TAG"]}, "limit": 3})

        def materials_read():
            item = self.results["materials_list"]["data"]["materials"][0]
            state["material"] = {"ticker": item["ticker"], "run_id": item["run_id"], "node_id": item["nodes"][0]}
            return self.query("materials_read", "get_company_research_materials",
                              {"action": "read", "ticker": item["ticker"], "node_id": item["nodes"][0]})

        # 每项为（检查名，调用）；顺序遵循 resolve → discover → query
        steps: list[tuple[str, Callable[[], str]]] = [
            ("a_share_resolve", a_share_resolve),
            ("a_share_resolve_unknown", lambda: self.query(
                "a_share_resolve_unknown", "gts_local_a_share_resolve", {"query": s["unknown_name"]}, expect="no_rows")),
            ("a_share_snapshot", lambda: self.query(
                "a_share_snapshot", "gts_local_a_share_snapshot", {"view_id": "company_core", "entity": ticker, "limit_per_section": 3})),
            ("a_share_discover", lambda: self.query(
                "a_share_discover", "gts_local_a_share_discover", {"operation": "search_capabilities", "query": "公告", "limit": 5})),
            ("a_share_announcements", lambda: self.query(
                "a_share_announcements", "gts_local_a_share_query",
                {"capability_id": "event.disclosures_calendar_and_shareholder_meetings", "entity": ticker, "plan": "summary",
                 "date_from": (today - timedelta(days=365)).isoformat(), "limit": 5})),
            ("a_share_dataset_query", lambda: self.dataset_query("a_share_dataset", "a_share", "ashareann_info", entity=ticker)),
            ("a_share_stock_links", lambda: self.query(
                "a_share_stock_links", "gts_local_a_share_indicator", {"operation": "get_stock_links", "entity": ticker, "limit": 3})),
            ("us_stock_snapshot", lambda: self.query(
                "us_stock_snapshot", "gts_local_us_stock_snapshot", {"view_id": "company_core", "entity": s["us_stock"], "limit_per_section": 3})),
            ("us_stock_discover", lambda: self.query(
                "us_stock_discover", "gts_local_us_stock_discover", {"operation": "list_capabilities", "limit": 50})),
            ("us_stock_query", lambda: self.query(
                "us_stock_query", "gts_local_us_stock_query", {"capability_id": "market.valuation_history", "entity": s["us_stock"], "limit": 3})),
            ("us_stock_dataset_query", lambda: self.dataset_query("us_stock_dataset", "us_stock", "usstk_securityinfo", entity=s["us_stock"])),
            ("index_resolve", index_resolve),
            ("index_snapshot", lambda: self.query(
                "index_snapshot", "gts_local_index_snapshot", {"view_id": "index_profile", "entity": state["index_id"], "limit_per_section": 3})),
            ("index_discover", lambda: self.query("index_discover", "gts_local_index_discover", {"operation": "list_capabilities"})),
            ("index_query", lambda: self.query(
                "index_query", "gts_local_index_query", {"capability_id": "index.composition_and_exposure", "entity": state["index_id"], "limit": 3})),
            ("index_dataset_query", lambda: self.dataset_query("index_dataset", "index", "index_info", entity=state["index_id"])),
            ("indicator_series", indicator_series),
            ("indicator_dataset_query", lambda: self.dataset_query("indicator_dataset", "indicator", "rate_rmbexchange")),
            ("theme_search", lambda: self.query(
                "theme_search", "gts_local_theme", {"operation": "search", "query": s["theme_query"], "limit": 3})),
            ("msci_discover", lambda: self.query("msci_discover", "msci_cnelefmtr", {"operation": "discover"})),
            ("msci_asset", lambda: self.query("msci_asset", "msci_cnelefmtr", {"operation": "asset", "securities": [ticker]})),
            ("research_digests", lambda: self.query(
                "research_digests", "get_a_share_research_report_digests", {"symbol": ticker, "query_mode": "latest_n", "limit": 3})),
            ("domestic_reports", lambda: self.query(
                "domestic_reports", "get_a_share_domestic_stock_research_reports", {"symbol": ticker, "limit": 2})),
            ("us_research_digests", lambda: self.query(
                "us_research_digests", "get_us_stock_research_report_digests", {"symbol": s["us_stock"], "limit": 2})),
            ("document_search", lambda: self.query(
                "document_search", "tl_document_search", {"document_type": "meeting_minutes", "keywords": [s["a_share_name"]], "limit": 3})),
            ("minutes_list", minutes),
            ("materials_list", lambda: self.query("materials_list", "get_company_research_materials", {"action": "list", "limit": 1})),
            ("materials_read", materials_read),
            ("materials_read_unresearched", lambda: self.query(
                "materials_read_unresearched", "get_company_research_materials",
                {"action": "read", "ticker": s["unresearched_ticker"], "node_id": "company_business_governance"}, expect="no_rows")),
        ]
        for name, function in steps:
            self.check("A", name, function)
        self.downloads(state)
        self.check("A", "tool_coverage", self.tool_coverage)

    def download(self, source: str, document_ref: dict, filename: str, kind: str) -> str:
        saved = self.client.download_file(source, document_ref, self.run_dir / "downloads" / filename)
        content = Path(saved["path"]).read_bytes()
        if kind == "pdf" and not content.startswith(b"%PDF-"):
            raise AssertionError(f"{filename} is not a PDF")
        if kind == "text" and not content.decode("utf-8").strip():
            raise AssertionError(f"{filename} is empty")
        self.covered.add("download_source_file")
        self.queries[f"download_{source}"] = {"tool": "download_source_file", "arguments": {"source": source, "document_ref": document_ref},
                                              "summary": f"{saved['size_bytes']} bytes {saved['media_type']}", "outcome": "rows"}
        return f"{saved['size_bytes']} bytes {saved['media_type']} -> {filename}"

    def downloads(self, state: dict) -> None:
        def report():
            reports = self.results["research_digests"]["data"]["reports"]
            return self.download("tl_research_report", reports[0]["report_ref"], "research_report.pdf", "pdf")

        def announcement():
            sources = self.results["a_share_announcements"]["data"]["sources"]
            row = next(source for source in sources if source["dataset"] == "ashareann_info")["result"]["rows"][0]
            return self.download("gts_a_share_announcement",
                                 {"record_id": row["id"], "ann_id": str(row["ann_id"]), "title": row["ann_title"]}, "announcement.pdf", "pdf")

        def mom_id():
            return {"mom_id": str(self.results["minutes_list"]["data"]["items"][0]["mom_id"])}

        self.check("A", "download_research_report", report)
        self.check("A", "download_announcement", announcement)
        self.check("A", "download_meeting_transcript", lambda: self.download("tl_meeting_transcript", mom_id(), "meeting_transcript.txt", "text"))
        self.check("A", "download_meeting_summary", lambda: self.download("tl_meeting_summary", mom_id(), "meeting_summary.txt", "text"))
        self.check("A", "download_company_research", lambda: self.download(
            "company_research", state["material"], "company_research.md", "text"))

    # 阶段 B：CapiFund

    def phase_b(self) -> None:
        data_dir = self.run_dir / "capifund"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        common = ["--data-dir", str(data_dir), "--host", "127.0.0.1", "--port", str(port), "--public-origin", origin]
        server_log = (self.run_dir / "capifund-server.log").open("w")
        server = None

        def start():
            nonlocal server
            subprocess.run([sys.executable, "-m", "capifund.admin", *common, "migrate-database", "--apply"],
                           check=True, capture_output=True, text=True)
            server = subprocess.Popen([sys.executable, "-m", "capifund.cli", *common], stdout=server_log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 30
            while True:
                try:
                    if httpx.get(origin + "/healthz", timeout=2, trust_env=False).status_code == 200:
                        return f"{origin}, data dir {data_dir}"
                except httpx.HTTPError:
                    pass
                if server.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"CapiFund did not start; see {server_log.name}")
                time.sleep(0.2)

        def stop():
            server.terminate()
            server.wait(10)
            return f"exit code {server.returncode}"

        try:
            if self.check("B", "fund_start", start):
                self.fund_records(FundClient(origin, author=AUTHOR))
        finally:
            if server is not None:
                self.check("B", "fund_stop", stop)
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


def write_report(run_dir: Path, title: str, url: str, checks: list[dict], extra: dict, footer: str) -> bool:
    passed = all(check["ok"] for check in checks)
    report = {"run": run_dir.name, "capifinmcp_url": url, "passed": passed, "checks": checks, **extra}
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    lines = [f"# {title} {run_dir.name}", "",
             f"结论：{'全部通过' if passed else '存在失败项'}（{sum(c['ok'] for c in checks)}/{len(checks)}）。服务地址 `{url}`。", "",
             "| 阶段 | 检查 | 结果 | 耗时（秒） | 说明 |", "| --- | --- | --- | --- | --- |"]
    for check in checks:
        detail = str(check["detail"]).replace("|", "\\|").replace("\n", " ")[:200]
        lines.append(f"| {check['phase']} | `{check['name']}` | {'通过' if check['ok'] else '失败'} | {check['seconds']} | {detail} |")
    lines += ["", footer, ""]
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return passed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    run_dir = new_run_dir(config)
    log(f"Run directory: {run_dir}")
    acceptance = Acceptance(config, run_dir)
    acceptance.phase_a()
    acceptance.phase_b()
    passed = write_report(run_dir, "CapiFinMCP 验收报告", acceptance.url, acceptance.checks, {"queries": acceptance.queries},
                          "下载文件位于 `downloads/`，测试用 CapiFund 数据位于 `capifund/`，完整调用参数与摘要见 `report.json`。")
    log(f"{'PASSED' if passed else 'FAILED'}; report: {run_dir / 'report.md'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
