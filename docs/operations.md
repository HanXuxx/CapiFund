# 运维

## 配置

配置的优先级为：命令行参数 > YAML（`--config`）> 默认值。字段说明见 [`config.yaml.example`](../config.yaml.example)。修改配置后，先用 `--check-config` 校验，这一步不监听端口，也不创建数据。

| 字段 | 默认值 |
|---|---|
| `storage.data_dir` | `/data1/capi_fin_agent/capifund` |
| `server.host` / `server.port` | `127.0.0.1` / `8794` |
| `server.public_origin` | `http://<host>:<port>`，接入包与 MCP Origin 校验都使用它 |

## 初始化与迁移

迁移只能在服务停止后执行：

```bash
capifund-admin --config config.yaml migrate-database           # 预览待执行的迁移
capifund-admin --config config.yaml migrate-database --apply   # 初始化或迁移，并输出校验结果
capifund-admin --config config.yaml validate-database          # 只读校验
capifund --config config.yaml                                  # 启动
```

服务启动时只检查数据库版本和协议快照，发现不一致就拒绝启动，不会自动迁移。服务和离线维护共用数据目录下的 `.service.lock`，两者不能同时运行。

`validate-database` 检查以下内容：

- SQLite 完整性和外键；
- WAL 模式；
- 正文 hash；
- 修订序列和回复关系；
- 当前修订与各查询投影是否一致。

它只报告问题，不做修复。

## 备份

```bash
capifund-admin --config config.yaml backup /path/outside/data_dir/capifund-YYYYMMDD
```

服务运行期间也可以执行在线备份，备份目标必须位于数据目录之外。备份保留 WAL 模式。恢复时，在服务停止的状态下，把备份目录中的 `fund.sqlite3` 放回数据目录，执行 `validate-database` 确认无误，然后启动服务。

## 接入包

服务通过 `/client.zip` 提供接入包，也可以离线生成：`capifund-admin --config config.yaml client-bundle capifund-client.zip`。接入包包含：

- `client.py`
- 协议
- `SKILL.md`
- metadata 示例
- `mcp.json`
- `connection.json`

接入包不包含任何凭据。
