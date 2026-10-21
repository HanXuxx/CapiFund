# CapiFinMCP 端口验收示例

本示例是一个独立进程：它通过 HTTP 端口调用正在运行的 CapiFinMCP 服务，再把取到的资料作为证据写入一个临时启动的 CapiFund 实例。它验证两层服务的真实联动，不替代任一仓库的单元测试。

## 验收内容

阶段 A 只对 CapiFinMCP 做只读调用和文件下载：

- 认证：不带 token 时直接返回 401；token 错误时客户端报告认证失败。
- 工具发现：`list_tools` 包含各入口的代表工具和 `download_source_file`，并且不公开会在服务端返回正文的工具（例如 `get_a_share_research_report_documents`、`tl_meeting_minutes_document`）。
- 只读查询：A 股按 resolve、snapshot、discover、公告目录的顺序调用；美股调用 snapshot；指数先 resolve 再读取 `index_profile`；指标先 search 再 `get_series`；另外覆盖题材搜索、`msci_cnelefmtr` discover、A 股研报摘要，以及会议纪要的 resolve 和 list。只有 `query_status=success` 且 `data_status=no_rows` 才算成功无记录；样本预期有数据，因此无记录也记为失败。
- 下载：从上面的查询结果中各取一条，分别下载研报 PDF、公告 PDF、会议纪要音转文 TXT 和摘要 TXT，保存到本次运行的 `downloads/`。

阶段 A 不调用 `gts_api_*` 外部 fallback，也不调用 `submit_company_research`、`cancel_company_research` 等会改变状态的工具。

阶段 B 先在本次运行目录下执行 `capifund-admin migrate-database --apply`，再在随机空闲端口启动 CapiFund 子进程。随后通过 `FundClient` 写入一条 active thesis、一条当天到期的 checkpoint，以及若干 evidence。每条 evidence 记录所引用的 CapiFinMCP 工具、参数和结果摘要，并在 `custom.source_tool` 中写入工具名。最后核对 `agenda`：checkpoint 应出现在到期清单中，thesis 应出现在“有新证据待复核”的清单中。核对结束后停止子进程。

## 运行

客户端只为本示例安装到 CapiFund 开发环境中，不是 CapiFund 的依赖：

```bash
cd CapiFund
.venv/bin/pip install -e vendor/CapiFinMCP/client
.venv/bin/python examples/capifinmcp_acceptance/run.py
```

默认读取同目录的 `config.yaml.example`。需要调整服务地址或样本时，复制该文件后用 `--config <path>` 指定。token 从环境变量 `CAPIFIN_MCP_TOKEN` 读取；环境变量为空时，读取 `token_file` 中的 `CAPIFIN_MCP_TOKEN=...` 行。进程不会打印或写出 token。

## 输出

每次运行在 `output_root` 下创建 `<时间戳>/` 目录：

- `report.md`、`report.json`：逐项检查结果、耗时，以及每次调用的参数和摘要。
- `downloads/`：下载得到的研报、公告和会议纪要文件。
- `capifund/`、`capifund-server.log`：测试用 CapiFund 的数据目录和服务日志。

任何检查失败时，进程以非零状态退出。默认输出根目录是 `/data1/capi_fin_agent/tests/capifinmcp-acceptance`，不要改到仓库或 `/tmp` 中。
