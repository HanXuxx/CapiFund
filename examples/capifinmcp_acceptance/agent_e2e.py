"""用正式 Pipeline 账号池中的 Codex 账号，让 Agent 经 HTTP 调用 CapiFinMCP 的全部可见工具并下载五种来源文件。

只挂载 CapiFinMCP 的 HTTP 服务（--ignore-user-config 不加载账号自带的 MCP 配置），gts_api_* 在 Codex 侧禁用。
文件由 Agent 在 workspace-write sandbox 中用 capifin-mcp download-file 写入本次运行目录。
判定依据是 Codex 事件流中实际发生的 MCP 调用和磁盘上的文件，而不是 Agent 的自述。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from capifin_mcp_client import CapiFinClient

from run import (DEFAULT_CONFIG, DOWNLOAD_FILES, GTS_API_TOOLS, check_file, load_config, load_token, log, new_run_dir, outcome,
                 write_report)

DEFAULT_CODEX_CONFIG = Path(__file__).with_name("codex_config.yaml.example")
SERVER = "capifinmcp"
# Codex 内置的 MCP 资源调用；CapiFinMCP 不提供 resources，返回 Method not found 属于预期。
CODEX_RESOURCE_CALLS = frozenset({"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"})
OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "calls"],
    "properties": {
        "summary": {"type": "string"},
        "calls": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["tool", "outcome", "note"],
            "properties": {"tool": {"type": "string"}, "outcome": {"type": "string", "enum": ["rows", "no_rows", "failed"]},
                           "note": {"type": "string"}}}},
    },
}
PROMPT = """你在验收 CapiFinMCP 数据服务。MCP 服务器 `{server}` 上可见的工具如下，每个都必须至少成功调用一次：

{tools}

要求：
1. 通过 `{server}` 的 MCP 工具取数。唯一允许的 shell 命令是下面第 5 条的下载命令；不要运行其他命令，不要打印环境变量。
2. 样本：A 股 {a_share_name}（{a_share}），美股 {us_stock}，指数 {index_name}，指标关键词 {indicator_query}，题材关键词 {theme_query}。
3. 按 resolve → discover → query 的顺序调用：先解析身份，再发现能力，再按返回的 capability_id、实体和字段查询。`*_dataset_query` 必须先用同一入口 discover 的 `describe_dataset` 取得 `contract_token` 并原样传回。指数实体使用 resolve 返回的 `index_id`。
4. MCP 工具 `download_source_file` 会把文件分块放进上下文，只调用一次：先用 `tl_meeting_minutes_list` 取得 `mom_id`，再以 `source=tl_meeting_summary`、`document_ref={{"mom_id": "<mom_id>"}}` 调用。
5. 用客户端命令把下面五种来源各下载一份到当前目录的 `downloads/`，引用必须来自本次工具返回值，原样写成 JSON：

   `{download_command} <source> --ref '<document_ref JSON>' --output downloads/<文件名>`

{downloads}

6. `get_company_research_materials` 先 `list`（limit=1），再 `read` 返回的 ticker 和第一个 node。
7. 返回值中只有 `metadata.query_status=success` 才算成功；`data_status=no_rows` 表示成功但无记录。失败时调整参数重试一次。
8. 最后按输出 schema 逐个报告每个工具和每次下载的结果；下载以来源名作为 `tool`。
"""
# 下载引用的取得方式，按 DOWNLOAD_FILES 的来源列出。
DOWNLOAD_HINTS = {
    "tl_research_report": "`get_a_share_research_report_digests` 返回的 `reports[i].report_ref`",
    "gts_a_share_announcement": ("`gts_local_a_share_query` 查询 `event.disclosures_calendar_and_shareholder_meetings` 后，"
                                 "`ashareann_info` 行映射为 `{\"record_id\": id, \"ann_id\": \"<ann_id 字符串>\", \"title\": ann_title}`"),
    "tl_meeting_transcript": "`tl_meeting_minutes_list` 返回的 `{\"mom_id\": \"<mom_id>\"}`",
    "tl_meeting_summary": "`tl_meeting_minutes_list` 返回的 `{\"mom_id\": \"<mom_id>\"}`",
    "company_research": "`get_company_research_materials` list 返回的 `{\"ticker\": ..., \"run_id\": ..., \"node_id\": <nodes 之一>}`",
}


def select_account(config: dict) -> tuple[str, Path]:
    """与正式 Pipeline 的 account_scheduling_reason 相同：active、已登录、额度快照新鲜、各窗口剩余高于阈值。"""
    state = Path(config["account_state_root"]).expanduser()
    accounts = json.loads((state / "codex_accounts.json").read_text(encoding="utf-8"))["accounts"]
    usage = json.loads((state / "codex_usage.json").read_text(encoding="utf-8"))["accounts"]
    refresh_seconds = json.loads((state / "settings.json").read_text(encoding="utf-8"))["codex"]["refresh_seconds"]
    now = datetime.now(timezone.utc)

    def reason(account: dict) -> str | None:
        snapshot = usage.get(account["id"]) or {}
        threshold = float(account["low_quota_remaining_percent"])
        if not account["active"]:
            return "inactive"
        if not snapshot.get("authenticated"):
            return "not_authenticated"
        if (now - datetime.fromisoformat(snapshot["refreshed_at"])).total_seconds() > refresh_seconds * 3:
            return "usage_stale"
        windows = snapshot.get("windows") or []
        if not windows:
            return "usage_unavailable"
        if any(float(w.get("remaining_percent", 0)) <= 0 for w in windows):
            return "quota_exhausted"
        if any(float(w.get("remaining_percent", 0)) < threshold for w in windows):
            return "low_quota"
        return None

    wanted = config.get("account")
    if wanted:
        account = next((a for a in accounts if a["id"] == wanted), None)
        if account is None:
            raise ValueError(f"account {wanted} is not in the pool")
        if reason(account):
            raise ValueError(f"account {wanted} is not schedulable: {reason(account)}")
    else:
        account = next((a for a in sorted(accounts, key=lambda a: (a["priority"], a["id"])) if not reason(a)), None)
        if account is None:
            raise ValueError("no schedulable account in the pool")
    return account["id"], Path(config["accounts_root"]).expanduser() / account["id"]


def codex_command(codex: dict, url: str, agent_dir: Path) -> list[str]:
    disabled = json.dumps(sorted(GTS_API_TOOLS))
    # 下载命令需要写当前目录并访问 MCP 端口；shell 只继承运行客户端所需的环境变量。
    shell_env = json.dumps(["PATH", "HOME", "LANG", "LC_*", "CAPIFIN_MCP_TOKEN"])
    return [
        codex["codex_bin"], "exec", "--ephemeral", "--json", "--ignore-user-config", "--ignore-rules",
        "--sandbox", "workspace-write", "--skip-git-repo-check", "--cd", str(agent_dir),
        "--output-schema", str(agent_dir / "output_schema.json"), "-m", codex["model"],
        "-c", f'model_reasoning_effort="{codex["reasoning_effort"]}"',
        "-c", "sandbox_workspace_write.network_access=true",
        "-c", "shell_environment_policy.ignore_default_excludes=true",
        "-c", f"shell_environment_policy.include_only={shell_env}",
        "-c", f'mcp_servers.{SERVER}.url="{url}"',
        "-c", f'mcp_servers.{SERVER}.bearer_token_env_var="CAPIFIN_MCP_TOKEN"',
        "-c", f'mcp_servers.{SERVER}.default_tools_approval_mode="approve"',
        "-c", f"mcp_servers.{SERVER}.disabled_tools={disabled}",
        "-c", f"mcp_servers.{SERVER}.tool_timeout_sec=300",
        "-",
    ]


def mcp_calls(events_path: Path) -> list[dict]:
    calls = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        item = event.get("item") or {}
        if event.get("type") != "item.completed" or item.get("type") != "mcp_tool_call" or item.get("server") != SERVER:
            continue
        state = f"failed:{item.get('status')}"
        if item.get("status") == "completed" and item.get("result"):
            texts = [c["text"] for c in item["result"].get("content", []) if c.get("type") == "text"]
            try:
                state = outcome(json.loads(texts[0]))
            except (IndexError, ValueError):
                state = "failed:unparsed_result"
        calls.append({"tool": item["tool"], "arguments": item.get("arguments"), "outcome": state, "error": item.get("error")})
    return calls


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--codex-config", type=Path, default=DEFAULT_CODEX_CONFIG)
    parser.add_argument("--client-cli", type=Path, default=Path(sys.executable).with_name("capifin-mcp"),
                        help="Agent 下载文件使用的 capifin-mcp 可执行文件")
    args = parser.parse_args(argv)
    config, codex = load_config(args.config), load_config(args.codex_config)
    mcp = config["capifinmcp"]
    run_dir = new_run_dir(config, "-agent")
    agent_dir = run_dir / "agent"
    agent_dir.mkdir()
    log(f"Run directory: {run_dir}")
    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str, seconds: float = 0.0) -> None:
        checks.append({"phase": "Agent", "name": name, "ok": ok, "detail": detail, "seconds": round(seconds, 2)})
        log(f"{'PASS' if ok else 'FAIL'} Agent/{name}: {detail}")

    token = load_token(mcp)
    account, home = select_account(codex)
    record("account", True, f"{account} ({home})")
    visible = {tool["name"] for tool in CapiFinClient(mcp["url"], token, float(mcp.get("timeout_seconds", 120))).list_tools()}
    targets = sorted(visible - GTS_API_TOOLS)
    (agent_dir / "output_schema.json").write_text(json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, indent=2), encoding="utf-8")
    (agent_dir / "downloads").mkdir()
    # 客户端配置只含地址和 token 环境变量名，token 经 Codex 继承的环境变量传入。
    client_config = agent_dir / "client.yaml"
    client_config.write_text(yaml.safe_dump({"url": mcp["url"], "token_env": "CAPIFIN_MCP_TOKEN",
                                             "timeout_seconds": mcp.get("timeout_seconds", 120)}), encoding="utf-8")
    download_command = f"{args.client_cli} --config {client_config} download-file"
    downloads = "\n".join(f"   - `{source}` → `downloads/{filename}`：{DOWNLOAD_HINTS[source]}"
                          for source, (filename, _) in DOWNLOAD_FILES.items())
    prompt = PROMPT.format(server=SERVER, tools="\n".join(f"- `{name}`" for name in targets), download_command=download_command,
                           downloads=downloads, **config["samples"])
    (agent_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    started = time.monotonic()
    log(f"Starting Codex ({codex['model']}, {codex['reasoning_effort']}) for {len(targets)} tools")
    with (agent_dir / "events.jsonl").open("w") as events, (agent_dir / "codex-stderr.log").open("w") as stderr:
        process = subprocess.run(codex_command(codex, mcp["url"], agent_dir), input=prompt, text=True, stdout=events, stderr=stderr,
                                 env={**os.environ, "CODEX_HOME": str(home), "CAPIFIN_MCP_TOKEN": token},
                                 timeout=float(codex["timeout_seconds"]))
    record("codex_exit", process.returncode == 0, f"exit code {process.returncode}", time.monotonic() - started)

    calls = mcp_calls(agent_dir / "events.jsonl")
    for name in targets:
        states = [call["outcome"] for call in calls if call["tool"] == name]
        record(f"tool/{name}", any(not state.startswith("failed") for state in states), ", ".join(states) or "not called")
    forbidden = sorted({call["tool"] for call in calls} - set(targets) - CODEX_RESOURCE_CALLS)
    record("no_hidden_tool_calls", not forbidden, f"unexpected calls: {forbidden}" if forbidden else "only visible tools were called")
    for source, (filename, kind) in DOWNLOAD_FILES.items():
        path = agent_dir / "downloads" / filename
        try:
            check_file(path, kind)
            record(f"download/{source}", True, f"{path.stat().st_size} bytes -> {filename}")
        except (OSError, UnicodeError, AssertionError) as exc:
            record(f"download/{source}", False, f"{type(exc).__name__}: {exc}")
    # token 进入了 Agent 的 shell 环境，因此核对 Agent 产物中没有出现 token。
    leaked = [path.name for path in agent_dir.rglob("*") if path.is_file() and token.encode() in path.read_bytes()]
    record("no_token_in_outputs", not leaked, f"token found in {leaked}" if leaked else "token absent from agent outputs")

    messages = [json.loads(line)["item"].get("text", "") for line in (agent_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("item", {}).get("type") == "agent_message"]
    try:
        final = json.loads(messages[-1])
        (agent_dir / "final.json").write_text(json.dumps(final, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record("final_output", True, final["summary"][:200])
    except (IndexError, ValueError, KeyError) as exc:
        record("final_output", False, f"final message is not schema JSON: {exc}")

    passed = write_report(run_dir, "CapiFinMCP Codex Agent 端到端报告", mcp["url"], checks,
                          {"account": account, "model": codex["model"], "reasoning_effort": codex["reasoning_effort"], "calls": calls},
                          "Codex 事件流位于 `agent/events.jsonl`，Agent 下载的文件位于 `agent/downloads/`，Prompt、输出 schema 和最终回复位于 `agent/`。")
    log(f"{'PASSED' if passed else 'FAILED'}; report: {run_dir / 'report.md'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
