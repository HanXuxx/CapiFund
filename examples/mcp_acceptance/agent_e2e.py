"""用正式 Pipeline 账号池中的 Codex 账号，让 Agent 经远程 MCP 在临时 CapiFund 实例上读写一条完整的投资逻辑。

只挂载临时实例的 capifund MCP（--ignore-user-config 不加载账号自带的 MCP 配置）；Agent 另在 shell 中运行一次接入包的 client.py。
判定依据是 Codex 事件流中实际发生的调用和事后从库中读回的内容，而不是 Agent 的自述。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from capifund.client import FundClient

from instance import DEFAULT_CONFIG, HERE, SHANGHAI, leaked_files, load_config, log, new_run_dir, start_instance, write_report
from run import FUND_TOOLS, client_env

DEFAULT_CODEX_CONFIG = HERE / "codex_config.yaml.example"
SERVER = "capifund"
# Codex 内置的 MCP 资源调用；CapiFund 不提供 resources，返回 Method not found 属于预期。
CODEX_RESOURCE_CALLS = frozenset({"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"})
OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "thesis_id", "calls"],
    "properties": {
        "summary": {"type": "string"},
        "thesis_id": {"type": "string"},
        "calls": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["tool", "ok", "note"],
            "properties": {"tool": {"type": "string"}, "ok": {"type": "boolean"}, "note": {"type": "string"}}}},
    },
}
PROMPT = """你在验收 CapiFund 投研库的远程 MCP。这是一个临时测试实例，写入的内容只用于验收，不是研究结论。MCP 服务器 `{server}` 上的工具如下，每个都必须至少成功调用一次：

{tools}

要求：
1. 先调用 `fund_protocol` 和 `fund_tag_tree`，再调用 `fund_agenda`。
2. 所有写操作使用 author={author}，每次写操作使用新的 request_id，格式为 `{author_id}-<用途>-<序号>`。
3. 用 `fund_create_record` 建立一条 `thesis_status=draft` 的投资逻辑，标签为 `验收/Agent`，`custom.basket` 为 {{"long": ["600519.SH"]}}。
4. 在这条逻辑下创建一条当天（{today}）到期的 checkpoint（`custom.kind=checkpoint`、`custom.due_date`），然后用 `fund_update_record` 携带正确的 `expected_revision` 把逻辑改为 `thesis_status=active`；custom 整体替换，保留 basket。
5. 再创建一条 evidence（`custom.kind=evidence`、`custom.stance=neutral`），正文说明来源是本次验收。
6. 用 `fund_query_records`（按作者 `{author_id}` 筛选）、`fund_read_record`、`fund_read_topic` 和 `fund_record_history` 读回这条逻辑，再调用一次 `fund_agenda` 确认节点和逻辑出现在待办中。
7. 唯一允许的 shell 命令是 `{client_command}`，运行一次即可。它从环境变量读取地址和 token；不要运行其他命令，不要打印环境变量。
8. 最后按输出 schema 返回逻辑 ID，并逐个报告每次工具调用和 client 命令的结果；client 命令以 `client.py agenda` 作为 `tool`。
"""


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
    # client 命令需要访问实例端口；shell 只继承运行 client.py 所需的环境变量。
    shell_env = json.dumps(["PATH", "HOME", "LANG", "LC_*", "CAPIFUND_URL", "CAPIFUND_TOKEN"])
    return [
        codex["codex_bin"], "exec", "--ephemeral", "--json", "--ignore-user-config", "--ignore-rules",
        "--sandbox", "workspace-write", "--skip-git-repo-check", "--cd", str(agent_dir),
        "--output-schema", str(agent_dir / "output_schema.json"), "-m", codex["model"],
        "-c", f'model_reasoning_effort="{codex["reasoning_effort"]}"',
        "-c", "sandbox_workspace_write.network_access=true",
        "-c", "shell_environment_policy.ignore_default_excludes=true",
        "-c", f"shell_environment_policy.include_only={shell_env}",
        "-c", f'mcp_servers.{SERVER}.url="{url}"',
        "-c", f'mcp_servers.{SERVER}.bearer_token_env_var="CAPIFUND_TOKEN"',
        "-c", f'mcp_servers.{SERVER}.default_tools_approval_mode="approve"',
        "-c", f"mcp_servers.{SERVER}.tool_timeout_sec=120",
        "-",
    ]


def completed_items(events_path: Path, item_type: str) -> list[dict]:
    items = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == item_type:
            items.append(item)
    return items


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--codex-config", type=Path, default=DEFAULT_CODEX_CONFIG)
    args = parser.parse_args(argv)
    codex = load_config(args.codex_config)
    run_dir = new_run_dir(load_config(args.config), "-agent")
    agent_dir = run_dir / "agent"
    agent_dir.mkdir()
    log(f"Run directory: {run_dir}")
    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str, seconds: float = 0.0) -> None:
        checks.append({"phase": "Agent", "name": name, "ok": ok, "detail": detail, "seconds": round(seconds, 2)})
        log(f"{'PASS' if ok else 'FAIL'} Agent/{name}: {detail}")

    account, home = select_account(codex)
    record("account", True, f"{account} ({home})")
    author = {"id": f"codex-acceptance-{run_dir.name}", "type": "agent", "runtime": "codex"}
    today = datetime.now(SHANGHAI).date().isoformat()
    with start_instance(run_dir) as instance:
        client_command = f"{sys.executable} {instance.client_py} agenda"
        prompt = PROMPT.format(server=SERVER, tools="\n".join(f"- `{name}`" for name in sorted(FUND_TOOLS)),
                               author=json.dumps(author, ensure_ascii=False), author_id=author["id"], today=today,
                               client_command=client_command)
        (agent_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        (agent_dir / "output_schema.json").write_text(json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, indent=2), encoding="utf-8")

        started = time.monotonic()
        log(f"Starting Codex ({codex['model']}, {codex['reasoning_effort']}) for {len(FUND_TOOLS)} tools")
        # token 只经环境变量传给 Codex 和 Agent 的 shell，取自接入包的 client.env。
        env = {**client_env(instance.client_py.parent), "CODEX_HOME": str(home)}
        with (agent_dir / "events.jsonl").open("w") as events, (agent_dir / "codex-stderr.log").open("w") as stderr:
            process = subprocess.run(codex_command(codex, instance.mcp_url, agent_dir), input=prompt, text=True,
                                     stdout=events, stderr=stderr, env=env, timeout=float(codex["timeout_seconds"]))
        record("codex_exit", process.returncode == 0, f"exit code {process.returncode}", time.monotonic() - started)

        events_path = agent_dir / "events.jsonl"
        calls = [{"tool": item["tool"], "status": item.get("status"), "arguments": item.get("arguments")}
                 for item in completed_items(events_path, "mcp_tool_call") if item.get("server") == SERVER]
        for name in sorted(FUND_TOOLS):
            states = [call["status"] for call in calls if call["tool"] == name]
            record(f"tool/{name}", "completed" in states, ", ".join(states) or "not called")
        forbidden = sorted({call["tool"] for call in calls} - FUND_TOOLS - CODEX_RESOURCE_CALLS)
        record("no_hidden_tool_calls", not forbidden, f"unexpected calls: {forbidden}" if forbidden else "only fund_* tools were called")
        commands = [item for item in completed_items(events_path, "command_execution") if str(instance.client_py) in item["command"]]
        record("client_py_agenda", any(item.get("exit_code") == 0 for item in commands),
               ", ".join(f"exit {item.get('exit_code')}" for item in commands) or "not run")

        # 事后从库中读回 Agent 以自报作者写入的内容。
        fund = FundClient(instance.origin, token=instance.token)
        theses = fund.query(type="thesis", author=author["id"])["items"]
        try:
            thesis = theses[0]
            topic = fund.topic(thesis["metadata"]["id"])
            kinds = sorted(reply["metadata"]["custom"].get("kind", "") for reply in topic["replies"])
            ok = (len(theses) == 1 and thesis["revision"] >= 2 and thesis["metadata"]["custom"]["thesis_status"] == "active"
                  and kinds == ["checkpoint", "evidence"])
            record("database_state", ok, f"{len(theses)} thesis, revision {thesis['revision']}, "
                                         f"status {thesis['metadata']['custom']['thesis_status']}, replies {kinds}")
        except (IndexError, KeyError) as exc:
            record("database_state", False, f"agent thesis not found or incomplete: {type(exc).__name__}: {exc}")

    leaked = leaked_files(run_dir, instance.token)
    record("no_token_in_outputs", not leaked, f"token found in {leaked}" if leaked else "token absent from run outputs")
    messages = [item.get("text", "") for item in completed_items(events_path, "agent_message")]
    try:
        final = json.loads(messages[-1])
        (agent_dir / "final.json").write_text(json.dumps(final, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record("final_output", True, f"thesis {final['thesis_id']}: {final['summary'][:160]}")
    except (IndexError, ValueError, KeyError) as exc:
        record("final_output", False, f"final message is not schema JSON: {exc}")

    passed = write_report(run_dir, "CapiFund Codex Agent 端到端报告", instance.mcp_url, checks,
                          {"account": account, "model": codex["model"], "reasoning_effort": codex["reasoning_effort"],
                           "author": author, "calls": calls},
                          "Codex 事件流、Prompt、输出 schema 和最终回复位于 `agent/`，临时实例数据位于 `capifund/`。")
    log(f"{'PASSED' if passed else 'FAILED'}; report: {run_dir / 'report.md'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
