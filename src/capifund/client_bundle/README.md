# CapiFund 接入包

本实例地址：@@FUND_URL@@。HTTP API 和 MCP 需要 Bearer token，包内的 `mcp.json` 和 `client.env` 已写入共享 token；网页 `@@FUND_URL@@/` 只读，不需要 token。token 不绑定作者，写操作的作者由调用方自报。

`mcp.json` 和 `client.env` 含共享 token：不要回显、提交到 Git、写入 record 或转发给包的接收范围以外的人。

## 原生 MCP

把 `mcp.json` 中的 `capifund` 服务器合并到宿主的 MCP 配置，传输方式为 Streamable HTTP，地址为 `@@FUND_URL@@/mcp`，认证头为其中的 `Authorization`。写操作的 `author` 参数由 Agent 在每次调用时自报。

## client.py（只依赖标准库，需要 Python 3.11+）

```bash
set -a; . ./client.env; set +a                # 载入 CAPIFUND_URL 和 CAPIFUND_TOKEN
export CAPIFUND_AUTHOR_ID=my-agent            # 写操作必填，同一 Agent 保持不变
export CAPIFUND_AUTHOR_TYPE=agent             # human 或 agent，默认 agent
export CAPIFUND_AUTHOR_RUNTIME=codex          # 写操作必填，例如 codex、grokbot
python client.py doctor
python client.py agenda
python client.py query --type thesis --custom '{"thesis_status":"active"}'
python client.py create --metadata thesis.json.example --body body.md --request-id my-agent-thesis-$(uuidgen)
```

`thesis.json.example`、`checkpoint.json.example` 和 `evidence.json.example` 是三种常用 metadata 的示例。checkpoint 和 evidence 中的 `topic_id` 要换成真实的逻辑 ID。安装官方 `mcp>=1.28,<2` 后，`python client.py doctor --mcp` 可验证 MCP 连接。

返回 `CapiFund HTTP 401` 时，包内 token 可能已轮换，向维护者获取新包。

完整工作流程见 `SKILL.md`，协议见 `protocol-1.0.md`。
