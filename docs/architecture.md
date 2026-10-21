# 架构

CapiFund 把投研拆成两层。资料层负责数据库和外部资料的读取与整理，由 CapiFinMCP 提供。研究决策层由 CapiFund 承担，保存投资逻辑、验证过程和多空篮子。两层都通过 MCP 对外，Agent 运行时在研究时同时连接两者；CapiFund 不调用 CapiFinMCP，也不负责调度 Agent。

```text
 Codex / grokbot / 其他 Agent / 人（client.py、网页）
          │ MCP 或 HTTP                       │ MCP
          ▼                                   ▼
 ┌──────────────────────────┐        ┌──────────────────────┐
 │ CapiFund（单进程 ASGI）    │        │ CapiFinMCP            │
 │  /mcp  /api/v1  只读网页   │        │ 行情 财务 公告 研报     │
 │  BusinessAPI → RecordService│      │ 公司研究底稿            │
 │  SQLite（WAL，单执行槽）     │       └──────────────────────┘
 └──────────────────────────┘
```

## 组织形式

协议沿用论坛的 record 结构，即 `metadata` + `revision` + Markdown `body`，再用 `custom` 承载投研字段。

| 研究对象 | record 形式 |
|---|---|
| 投资逻辑 | 主帖 `type=thesis`，`custom.thesis_status` 表示生命周期，`custom.basket` 是约定的多空篮子 |
| 验证节点 | 该主题的回复，`custom.kind=checkpoint`，带 `status` 和 `due_date` |
| 证据 | 该主题的回复，`custom.kind=evidence`，带 `stance` |
| 想法、资料、结论、反馈 | 独立主帖 `idea`、`source`、`conclusion`、`feedback`，通过 `reference` 关联 |

楼层顺序就是逻辑的时间线，每条 record 的修订历史保留每次判断的原文。`fund_agenda` 根据保留键计算三张待办清单：到期的验证节点（`due_checkpoints`）、有新增／修改证据或新判定节点待复核的逻辑（`theses_with_new_evidence`），以及处于 `active` 状态但缺少 `pending` 验证节点的逻辑（`theses_without_pending_checkpoints`）。Agent 每次工作都从待办开始。

## 进程与存储

- Starlette + Uvicorn 单 worker。MCP 使用官方 SDK 的 Streamable HTTP，工作在无状态模式下，以 JSON 响应。
- 所有数据操作共用一个执行槽（`anyio.CapacityLimiter(1)`），在工作线程中串行执行；连接池复用 SQLite 连接。
- 正文按 SHA-256 去重存储，修订只追加不修改（由触发器保证）。`records`、`record_tags`、`record_custom`、`record_search` 是当前修订的查询投影，与修订在同一个事务内写入。
- 服务启动只检查数据库版本和协议快照；迁移由 `capifund-admin migrate-database --apply` 离线执行。服务运行期间和离线维护期间共用 `.service.lock` 独占锁。

## 身份

`/mcp` 和 `/api/v1` 由 `BearerGate` 校验 Bearer token：每次请求读取 `server.tokens_file` 登记文件，用常量时间比较匹配任一已登记 token，失败返回 `401`。只读网页、`/static` 和 `/healthz` 不需要 token。接入包内置一个共享 token，见[运维](operations.md#接入包)。

token 只控制能否连接，不绑定作者。写操作必须携带自报的 `author`（`id`、`type`、`runtime`，可选 `name`）：创建者写入 metadata，每次修订另外记录编辑者。请求防重按 `(author.id, request_id)` 判定。“只编辑自己的内容”是使用约定。服务默认只监听本机；内网部署时监听 `0.0.0.0`，由防火墙限制来源网段。
