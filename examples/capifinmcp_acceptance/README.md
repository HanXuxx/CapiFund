# CapiFinMCP 端口验收示例

本目录有两个独立进程，都通过 HTTP 端口调用正在运行的 CapiFinMCP 服务，不替代任一仓库的单元测试：

- `run.py`：逐个调用 CapiFinMCP 的远程工具，再把取到的资料作为证据写入一个临时启动的 CapiFund 实例。不消耗 Codex 额度。
- `agent_e2e.py`：从正式 Agent Pipeline 的 Codex 账号池选用一个账号，让 Codex Agent 自己调用 CapiFinMCP 的全部可见工具，用事件流核对实际调用。

两者都不调用 `gts_api_*`（会消耗 Gangtise 外部额度）。

## 开箱运行

客户端只为本示例安装到 CapiFund 开发环境中，不是 CapiFund 的依赖：

```bash
cd CapiFund
.venv/bin/pip install -e vendor/CapiFinMCP/client
.venv/bin/python examples/capifinmcp_acceptance/run.py
.venv/bin/python examples/capifinmcp_acceptance/agent_e2e.py
```

两个进程默认读取同目录的 `config.yaml.example`；`agent_e2e.py` 另外读取 `codex_config.yaml.example`。需要调整时复制后用 `--config <path>` 或 `--codex-config <path>` 指定。token 从环境变量 `CAPIFIN_MCP_TOKEN` 读取；环境变量为空时，读取 `token_file` 中的 `CAPIFIN_MCP_TOKEN=...` 行。进程只经环境变量把 token 交给 Codex，不打印也不写出 token。

## `run.py` 验收内容

阶段 A 只对 CapiFinMCP 做只读调用和文件下载：

- 认证：不带 token 时返回 401；token 错误时客户端报告认证失败。
- 工具发现：`list_tools` 不包含服务端正文读取、私有数据和已从 MCP 移除的公司研究提交、状态、取消工具；直接调用这些工具时，客户端抛出 `CapiFinClientError`。
- 只读调用：按 resolve、discover、query 的顺序覆盖 A 股、美股、指数、指标、题材、`msci_cnelefmtr`、研报、`tl_document_search`、会议纪要和公司研究材料。`*_dataset_query` 先用同一入口的 `describe_dataset` 取得 `contract_token`；指数直接使用 resolve 返回的 `index_id`。
- 反向检查：解析不存在的名称应返回成功无记录；读取未研究过的 ticker 的公司研究材料应返回空正文。
- 状态判定：只看 `metadata.query_status` 和 `metadata.data_status`。只有 `query_status=success` 且 `data_status=no_rows` 才算成功无记录；样本预期有数据的调用返回无记录也记为失败。
- 下载：`download_source_file` 的五种来源各下载一份，保存到 `downloads/`。PDF 必须以 `%PDF-` 开头，TXT 和 Markdown 必须是非空 UTF-8。
- 覆盖检查：`tool_coverage` 要求 `list_tools` 中除 `gts_api_*` 外的每个工具都至少有一次通过的调用。服务新增工具而本脚本未覆盖时，验收失败。

阶段 B 在本次运行目录下执行 `capifund-admin migrate-database --apply`，再在随机空闲端口启动 CapiFund 子进程。随后通过 `FundClient` 写入一条 active thesis、一条当天到期的 checkpoint，以及若干 evidence。每条 evidence 记录所引用的 CapiFinMCP 工具、参数和结果摘要，并在 `custom.source_tool` 中写入工具名。最后核对 `agenda`：checkpoint 应出现在到期清单中，thesis 应出现在“有新证据待复核”的清单中。核对结束后停止子进程。

## `agent_e2e.py` 测试内容

`codex_config.yaml.example` 指向正式 Pipeline 的 Monitor 状态目录和账号登录目录，默认使用 `niuh` 账号、`gpt-6.1-sol` 和 `medium`。选号规则与 Pipeline 相同：账号必须 active、已登录、额度快照不超过 `settings.json` 中 `codex.refresh_seconds` 的三倍，且每个额度窗口的剩余百分比高于该账号阈值。`account: null` 时按 priority、id 选第一个可调度账号。进程只读取注册表和额度快照，不修改账号，也不占用 Pipeline 容量槽。

Codex 以 `codex exec --ignore-user-config` 运行：仍用账号目录的登录凭据，但不加载账号自带的 MCP 配置，只挂载 CapiFinMCP 的 HTTP 服务，并在 Codex 侧禁用 `gts_api_*`。sandbox 为只读。Prompt 要求 Agent 按 resolve、discover、query 的顺序调用每个可见工具，下载只取一份会议纪要摘要 TXT。

判定依据是 `events.jsonl` 中实际完成的 MCP 调用：每个可见工具至少有一次调用的 `metadata.query_status=success`，且 Agent 没有调用可见范围以外的工具。Codex 内置的 MCP 资源列举（如 `list_mcp_resources`）不计入；CapiFinMCP 不提供 resources，这类调用返回 Method not found 属于预期。每次运行会消耗所选账号的 Codex 额度。

## 输出

每次运行在 `output_root` 下创建目录，`run.py` 为 `<时间戳>/`，`agent_e2e.py` 为 `<时间戳>-agent/`：

- `report.md`、`report.json`：逐项检查结果、耗时，以及每次调用的参数和结果。
- `run.py`：`downloads/` 保存下载文件；`capifund/`、`capifund-server.log` 是测试用 CapiFund 的数据目录和服务日志。
- `agent_e2e.py`：`agent/` 保存 Prompt、输出 schema、Codex 事件流、stderr 和最终回复 `final.json`。

任何检查失败时，进程以非零状态退出。默认输出根目录是 `/data1/capi_fin_agent/tests/capifinmcp-acceptance`，不要改到仓库或 `/tmp` 中。
