# 迭代规则

## 协议版本

record 协议版本采用 `major.minor` 形式，与整数形式的数据库版本分开管理。

- 次版本升级只做向后兼容的增加，例如新增 `type`、新增 `kind`、在 `kinds.RESERVED` 中登记新的保留键或新取值。常见做法是先让 Agent 用自由 `custom` 键试用一段时间，确认有系统行为依赖它时，再升级为保留键。旧 record 不改写，新规则只作用于此后的写入。
- 主版本升级用于不兼容变更，例如重命名、删除字段或改变语义。必须同时写出旧版本到新版本的转换规则，并在 `schema_migrations.MIGRATIONS` 中追加一个离线迁移步骤。转换代码只在第一次真正需要时编写。
- 每次协议升级都要同步修改三处：新增或修改 `protocols/<版本>.md`，更新 `db/database.py` 的 `PROTOCOL`，更新 `SKILL.md`。快照随迁移写入数据库。

只修改同一版本规范的文字时，停服后执行 `migrate-database --apply` 刷新快照即可，历史 metadata 不变。

## 数据库迁移

在 `migrations/` 追加按序编号的 SQL 文件，再在 `MIGRATIONS` 中登记一项 `(编号, 名称, 可选转换函数)`。迁移只能离线执行，顺序为：停服、预览、执行、校验、启动。详见 [运维](operations.md)。

## 后续阶段

当前完成的是阶段 0 和阶段 1：仓库骨架、record 协议 1.0、投研保留键、待办接口、HTTP/MCP 接入、只读网页和客户端。后续阶段开始前需要先确认范围：

- 阶段 2，决策与模拟组合：从 `thesis` 的多空篮子和调仓回复投影出模拟持仓，由独立的 mark CLI 通过 `capifin_mcp_client` 读取后复权价格并估值。前置阶段在协议 `1.0` 下先通过普通回复的自由键 `custom.rebalance`（包含 `effective_date`、`benchmark`、`legs[{symbol, side, weight, mode}]`）试用调仓记录，待阶段 2 估值 CLI 启用时通过次版本协议升级登记为 `custom.kind="rebalance"` 保留键。
- 阶段 3，组织形式：根据实际使用情况，决定哪些自由 `custom` 键升级为保留键，以及是否需要多逻辑之间的组合视图。
