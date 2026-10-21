# CapiFund 远程 MCP 端到端验收

本目录的进程各自启动一个临时 CapiFund 实例，通过带 Bearer token 的远程 MCP 读写，并用该实例生成的接入包复核。它们不替代 `tests/` 中的单元测试，也不读写正式库：

- `run.py`：脚本逐个调用全部 `fund_*` 工具，再用接入包中的 `client.py` 和只读网页复核。不消耗 Codex 额度。
- `agent_e2e.py`：从正式 Agent Pipeline 的 Codex 账号池选用一个账号，让 Codex Agent 自己经远程 MCP 建立并更新一条投资逻辑，用事件流和库内内容核对。

## 开箱运行

```bash
cd CapiFund
.venv/bin/python examples/mcp_acceptance/run.py
.venv/bin/python examples/mcp_acceptance/agent_e2e.py
```

两个进程默认读取同目录的 `config.yaml.example`；`agent_e2e.py` 另外读取 `codex_config.yaml.example`。需要调整时复制后用 `--config <path>` 或 `--codex-config <path>` 指定。

## 临时实例

`instance.py` 是两个进程共用的准备步骤：

1. 清空本目录下的 `client_bundle/`。该目录保存临时 token 登记文件、接入包 zip 和解压后的接入包，含 token，已被 gitignore。
2. 在本次运行目录的 `capifund/` 下执行 `capifund-admin migrate-database --apply`，用 `token issue --name acceptance` 签发临时 token，再用 `client-bundle` 生成接入包并解压。
3. 在 `127.0.0.1` 的随机空闲端口启动 CapiFund。服务地址和 token 取自解压包内的 `mcp.json`，`client.py` 的地址和 token 取自包内 `client.env`。
4. 验收结束后停止服务。

## `run.py` 验收内容

- 认证：`/mcp` 和 `/api/v1` 不带 token 或 token 错误时返回 401；只读网页不需要 token。
- 工具发现：`list_tools` 恰好是全部 `fund_*` 工具。
- 读写流程：读取协议、标签树和空待办；创建 draft thesis，并用同一 `request_id` 重试，确认返回原记录；创建当天到期的 checkpoint；携带正确 `expected_revision` 把 thesis 改为 active，再用过期修订号更新，确认返回 `revision_conflict`；最后创建 evidence。
- 读回：`fund_query_records` 按 `custom` 筛选到这条 thesis；`fund_read_record` 读取当前修订和修订 1；`fund_read_topic` 用 evidence 的 ID 读回整条逻辑；`fund_record_history` 返回两个修订；`fund_agenda` 中 checkpoint 已到期、thesis 有新证据待复核。
- 接入包：载入包内 `client.env` 后，`client.py` 的 `doctor`、`doctor --mcp`、`agenda` 和 `query` 都成功，输出包含新记录。
- 网页：`/`、`/agenda` 和 `/records/{id}` 显示新写入的内容。
- 覆盖与泄露：每个 `fund_*` 工具至少成功调用一次；运行目录中不出现 token。

## `agent_e2e.py` 测试内容

`codex_config.yaml.example` 指向正式 Pipeline 的 Monitor 状态目录和账号登录目录，默认使用 `niuh` 账号、`gpt-6.1-sol` 和 `medium`。选号规则与 Pipeline 相同：账号必须 active、已登录、额度快照不超过 `settings.json` 中 `codex.refresh_seconds` 的三倍，且每个额度窗口的剩余百分比高于该账号阈值。`account: null` 时按 priority、id 选第一个可调度账号。进程只读取注册表和额度快照，不修改账号，也不占用 Pipeline 容量槽。

Codex 以 `codex exec --ignore-user-config` 运行：仍用账号目录的登录凭据，但不加载账号自带的 MCP 配置，只挂载临时实例的 `capifund` MCP，token 经环境变量 `CAPIFUND_TOKEN` 传入。Prompt 要求 Agent 调用每个 `fund_*` 工具，以 `codex-acceptance-<运行目录名>` 为自报作者建立 draft thesis、当天到期的 checkpoint，把 thesis 更新为 active，再添加 evidence 并读回；另外在 shell 中运行一次包内 `client.py agenda`。sandbox 使用 `workspace-write` 并允许联网，shell 只继承 `PATH`、`HOME`、`LANG`、`LC_*`、`CAPIFUND_URL` 和 `CAPIFUND_TOKEN`。

判定依据如下：

- `events.jsonl` 中每个 `fund_*` 工具至少有一次完成的调用，且 Agent 没有调用其他工具。Codex 内置的 MCP 资源列举（如 `list_mcp_resources`）不计入。
- `client.py agenda` 命令以退出码 0 完成。
- 事后用 token 从库中读回：该作者恰好有一条 thesis，状态为 active、修订号至少为 2，回复恰好是一条 checkpoint 和一条 evidence。
- 运行目录中不出现 token；最终回复符合输出 schema。

每次运行会消耗所选账号的 Codex 额度。

## 输出

每次运行在 `output_root` 下创建目录，`run.py` 为 `<时间戳>/`，`agent_e2e.py` 为 `<时间戳>-agent/`：

- `report.md`、`report.json`：逐项检查结果和耗时；`agent_e2e.py` 另外记录每次 MCP 调用的参数和状态。
- `capifund/`、`capifund-server.log`：临时实例的数据目录和服务日志。
- `run.py`：`client/` 保存 `client.py` 各命令的输出。
- `agent_e2e.py`：`agent/` 保存 Prompt、输出 schema、Codex 事件流、stderr 和最终回复 `final.json`。

任何检查失败时，进程以非零状态退出。默认输出根目录是 `/data1/capi_fin_agent/tests/capifund-acceptance`，不要改到仓库或 `/tmp` 中。
