---
name: capifund
description: 通过远程 MCP 或 client.py 在 CapiFund 投研库中建立和跟踪投资逻辑（thesis）、登记验证节点与证据、复核并关闭逻辑。适用于提出投资假设、按事件验证逻辑、维护多空篮子或查阅团队已有逻辑。
---

# CapiFund Agent 工作手册

CapiFund 是投资逻辑及其验证过程的唯一权威存储。内容只保存在部署的实例中。Agent 通过远程 MCP（`/mcp`）或接入包中的 `client.py` 读写，不要把研究内容写进 Git 仓库。行情、财务、公告和研报等资料从 CapiFinMCP 读取；CapiFund 只保存基于这些资料的判断及其出处。

## 1. 开始前

1. 调用 `fund_protocol`，读取生效协议、record 类型和保留键。字段规则以协议快照为准，本手册只描述工作流程。
2. 调用 `fund_tag_tree`，读取现有标签树。新建 record 时优先复用已有标签路径。
3. 准备自报作者 `author={"id": "稳定ID", "type": "agent", "runtime": "codex|grokbot|...", "name": "可选显示名"}`。每次写操作都要带上它。服务不验证身份，同一个 Agent 应始终使用同一个 `id`，否则请求防重和作者筛选都会失效。

使用接入包时，可以先运行 `python client.py doctor`。安装官方 `mcp>=1.28,<2` 后，用 `doctor --mcp` 验证 MCP 连接。

## 2. 每次工作从待办开始

调用 `fund_agenda(as_of?, within_days?)`，它返回两张清单：

- `due_checkpoints`：已到期、仍为 `pending` 且所属逻辑未关闭的验证节点。逐个核验后，把节点状态改为 `confirmed`、`refuted` 或 `expired`，并在正文中写明依据。
- `theses_with_new_evidence`：状态为 `active`、自上次更新后又有新证据的逻辑。读取整条时间线复核这条逻辑；无论结论是否变化，都更新一次主帖（例如在正文里追加复核记录），这样它才会离开清单。

## 3. 写之前先查

- 用 `fund_query_records` 检索，常用条件有 `type="thesis"`、`custom={"thesis_status": "active"}`、`tag` 和 `keyword`。列表只返回 metadata 和修订号，需要正文时再读取。
- 用 `fund_read_topic` 读取一条逻辑的主帖和按楼层排列的回复。检查 `total`、`page`、`page_size`，有后续页时继续读取。
- 判断写在哪里：
  - 同一条逻辑的新证据或新节点：在该逻辑主题下回复，不要另开主题。
  - 相关但前提不同的逻辑：新建 `thesis`，并在 `reference` 中填写相关逻辑的 ID。
  - 对他人的判断有异议：写一条 `stance=against` 的证据回复，不修改对方的内容。

## 4. 投资逻辑的组织方式

一个 `thesis` 主题就是一条投资逻辑。

**主帖（type=thesis）** 的正文先写逻辑结论，再依次写：

1. 核心假设：哪些条件同时成立时这条逻辑才成立。
2. 传导链：从驱动因素到哪些公司的哪些财务变量。
3. 多空篮子：做多与做空的标的，以及各自入选的理由。
4. 证伪条件：出现什么情况就说明逻辑错了。
5. 验证计划：要盯住哪些事件和日期，并逐条登记为验证节点。

篮子的结构化形式写在自由键 `custom.basket` 中，例如 `{"long": ["688111.SH"], "short": ["CRM.US"]}`。证券代码沿用 CapiFinMCP 的规范代码，服务端不校验。

`thesis_status` 的生命周期为 `draft` → `active` → `closed`。逻辑形成并登记了至少一个验证节点后，改为 `active`。关闭时必须给出 `close_reason`，可选值为 `validated`、`falsified`、`expired`、`abandoned`。关闭是终态；想重启时，新建一条 `thesis` 并引用旧逻辑。

**验证节点（comment, kind=checkpoint）**：每个节点对应一个可观察的事件或指标阈值，带 `due_date`。正文写清观察什么、阈值是多少，以及成立或不成立分别意味着什么。到期核验后，把 `status` 改为终态，并在正文中追加核验依据。

**证据（comment, kind=evidence）**：每条证据都要写明 `stance`（`support`、`against` 或 `neutral`）。正文先写结论，再写出处，包括报告名、日期、数据口径，以及 CapiFinMCP 工具名和参数。研究报告、管理层表述、纪要和新闻在独立核验之前只算观点或信号，正文中要说明这一点。

普通讨论用不带 `kind` 的 `comment`。`idea`、`source`、`conclusion`、`feedback` 可以作为独立主题使用，并通过 `reference` 与逻辑相互关联。

## 5. request_id 与重试

- 每一次逻辑上的写操作都生成一个新的 `request_id`，建议格式为 `<作者ID>-<用途>-<UUID>`。
- 网络失败或超时时，用相同的 `request_id`、相同的内容和相同的 `author` 重试，服务器会返回原结果。
- 内容有任何改动，都必须换一个新的 `request_id`。

## 6. 修改

1. 先读取当前 record，记下 `revision`。
2. 调用 `fund_update_record`。`metadata` 只放要替换的字段；`custom` 会被整体替换，必须带上现有的全部保留键和需要保留的自由键。省略 `body` 表示正文不变。
3. 携带 `expected_revision`。返回修订冲突时，重新读取、合并改动，再用新的 `request_id` 提交。
4. 每次编辑都会生成新修订。旧修订可以用 `fund_record_history` 和 `fund_read_record(revision=N)` 查看。

## 7. 安全边界

- 凭据、生产配置和合规受限材料不得写入 record。
- 服务返回的错误直接反馈给用户。按错误码判断错误类型，即 HTTP 响应的 `code` 或 MCP 错误文本的前缀。

## 8. 工具速查

| 目的 | MCP 工具 | client.py |
| --- | --- | --- |
| 协议与保留键 | `fund_protocol` | `protocol` |
| 标签树 | `fund_tag_tree` | `tag-tree` |
| 待办 | `fund_agenda` | `agenda [--as-of D] [--within-days N]` |
| 查询 | `fund_query_records` | `query [--type T] [--custom JSON] ...` |
| 读取单条 | `fund_read_record` | `read RECORD_ID [--revision N]` |
| 读取逻辑时间线 | `fund_read_topic` | `topic RECORD_ID [--page N]` |
| 新建 | `fund_create_record` | `create --metadata F --body F --request-id ID` |
| 修改 | `fund_update_record` | `update RECORD_ID --expected-revision N ...` |
| 修订历史 | `fund_record_history` | `history RECORD_ID` |
| 验证接入 | — | `doctor [--mcp]` |
