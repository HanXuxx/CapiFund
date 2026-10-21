# CapiFund 开发指令

本文件适用于整个 CapiFund 仓库。中文是开发规范和文档的权威语言；代码标识符、工具名、schema 字段和路径保持英文，日志和错误信息使用英文。

## 仓库地图与事实源

- `src/capifund/domain/`：业务规则。`records.py` 管理修订、幂等和关系；`kinds.py` 定义投研保留键；`business.py` 是接入层唯一的调用入口。
- `src/capifund/db/`：SQLite 存储、迁移、查询投影和离线维护。表结构以 `migrations/*.sql` 为准。
- `src/capifund/api/`：HTTP 路由（`http_api.py`）、MCP 工具（`remote_mcp.py`）、Bearer 认证（`auth.py`）和接入包。`web/` 是只读网页。token 登记文件的读写在 `src/capifund/tokens.py`。
- `deploy/`：用户级 systemd unit 示例；`examples/mcp_acceptance/`：临时实例上的远程 MCP 与 Codex Agent 端到端验收。
- `src/capifund/protocols/1.0.md` 是生效协议的规范原文；`client_bundle/SKILL.md` 是唯一的 Agent 工作手册，MCP prompt `capifund_workflow` 直接读取它。
- `vendor/CapiFinMCP`：资料层 submodule。CapiFund 代码不导入其中的任何内容；Agent 在运行时同时连接这两个 MCP。

## 规则

- 不在网页增加写入口；HTTP、MCP 和网页都只调用 `BusinessAPI`。
- 服务端只校验 `kinds.RESERVED` 中登记的保留键，其他 `custom` 字段原样保存。新增保留键属于协议次版本变更，按 [迭代规则](docs/iterate.md) 处理。
- 默认采用满足当前需求的最小实现。不为假设中的需求增加缓存、锁、开关、兼容层或额外校验。
- 真实研究数据只存在于运行数据目录（默认 `/data1/capi_fin_agent/capifund`），不写入仓库或 `/tmp`。测试使用 pytest 的 `tmp_path`。
- 修改接口、协议或行为时，在同一变更中同步更新 `protocols/`、`SKILL.md` 和 `docs/`。
- 未经用户明确要求，不 commit、不 push、不改变暂存状态。

## 部署

本机运行实例不会自动加载新代码。CapiFund 或 `vendor/CapiFinMCP` 的每次改动通过验证后，都要重新部署受影响的实例并做冒烟检查，不让运行中的服务停留在旧代码：

- CapiFund 服务 `capifund.service`：按[运维说明](docs/operations.md#内网部署)备份、停服、删除 `build/` 后 `pip install .`、有迁移或协议文本变化时执行 `migrate-database --apply`，再启动。
- CapiFinMCP HTTP 服务 `capifin-mcp.service`：在 `vendor/CapiFinMCP` 执行 `scripts/deploy_server.py`，步骤见 [server.md](vendor/CapiFinMCP/docs/server.md#更新与回退)。
- `company_research` 的 Scheduler、Maintainer 和 Monitor：它们从 `vendor/CapiFinMCP/.venv` 的 editable 安装运行。新 Run 启动的 stdio MCP 直接读取工作树代码，长驻进程本身要重启才会加载新代码。Run 读取运行空间 `/data1/capi_fin_agent/mcp-company-research/assets/`，包内资产改动后还要同步到运行空间。步骤见 [company-research.md](vendor/CapiFinMCP/docs/company-research.md#本机运行与更新)。

只部署受改动影响的实例：只改 CapiFund 时不重启 CapiFinMCP 的服务，反之亦然。只改 `docs/` 或测试时不需要部署；`SKILL.md`、协议文本，以及 CapiFinMCP `server/` 包内的资产和 Skill 随安装包发布，改动后同样要部署。部署结果和冒烟检查写进交付说明。

## 验证

- `.venv/bin/python -m pytest -q`
- `git diff --check`，并人工审阅 diff。
