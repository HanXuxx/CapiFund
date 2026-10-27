# CapiFund

CapiFund 是以投资逻辑为核心的中心化投研库。一条投资逻辑（`thesis`）包含核心假设、传导链、多空篮子和证伪条件。验证节点（`checkpoint`）和证据（`evidence`）作为它的回复按时间排列，逻辑结束时以明确的原因关闭。CapiFund 只保存研究判断及其组织形式；行情、财务、公告等资料由 [CapiFinMCP](vendor/CapiFinMCP) 提供。任何能使用 MCP 或 HTTP 的 Agent 方案（Codex、grokbot 等）以及人类，都可以直接读写同一个库。

| 读者 | 入口 |
|---|---|
| 了解设计 | [架构](docs/architecture.md) |
| 部署与维护 | [运维](docs/operations.md) |
| 演进协议与功能 | [迭代规则](docs/iterate.md) |
| Agent 使用 | [工作手册](src/capifund/client_bundle/SKILL.md)、[协议 1.0](src/capifund/protocols/1.0.md) |
| 远程 MCP 端到端验收 | [临时实例、接入包与 Codex Agent 验收](examples/mcp_acceptance/README.md) |

## 快速开始

```bash
git submodule update --init
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/capifund-admin --data-dir /data1/capi_fin_agent/capifund migrate-database --apply
.venv/bin/capifund-admin token issue --name local
.venv/bin/capifund --data-dir /data1/capi_fin_agent/capifund
```

服务默认监听 `127.0.0.1:8794`，各入口如下：

- 只读网页：`/`，不需要 token
- HTTP API：`/api/v1/`，需要 Bearer token
- MCP：`/mcp`，需要 Bearer token

token 登记在 `~/.config/capifund/tokens.json`（目录 `0700`、文件 `0600`），由 `capifund-admin token` 管理。token 不绑定作者，写操作由客户端自报作者；内置共享 token 的 Agent 接入包由 `capifund-admin client-bundle` 生成，见[运维](docs/operations.md)。

## 开发验证

```bash
.venv/bin/python -m pytest -q
```

`vendor/deepseek-harness` 是固定版本的 DSH submodule，与 CapiFinMCP 内的 DSH 副本指向同一 commit；CapiFund 代码不使用它，CapiFinMCP 公司研究的 DSH 执行器见[公司研究](vendor/CapiFinMCP/docs/company-research.md#dsh-执行器)。

CapiFinMCP 的真实数据验收位于 [vendor/CapiFinMCP/examples/acceptance](vendor/CapiFinMCP/examples/acceptance/README.md)。

## 来源

record 协议、存储分层和 HTTP/MCP 接入方式复制自 CapiAgent 论坛（`~/CapiAgent/apps/capi_forum`，commit `81c57a2c8472ea6b00ea9a6e6e2619b1cdd69447`），并做了删减：去掉了身份、Passkey、图片、Credit、审计和删除恢复，`water` 类型改为 `thesis`，网页改为只读，同时增加了投研保留键和待办接口。两个项目此后各自独立演进。
