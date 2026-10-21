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

## 验证

- `.venv/bin/python -m pytest -q`
- `git diff --check`，并人工审阅 diff。
