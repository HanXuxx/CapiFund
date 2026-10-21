# 运维

## 配置

配置的优先级为：命令行参数 > YAML（`--config`）> 默认值。字段说明见 [`config.yaml.example`](../config.yaml.example)。修改配置后，先用 `--check-config` 校验，这一步不监听端口，也不创建数据。

| 字段 | 默认值 |
|---|---|
| `storage.data_dir` | `/data1/capi_fin_agent/capifund` |
| `server.host` / `server.port` | `127.0.0.1` / `8794` |
| `server.public_origin` | `http://<host>:<port>`，接入包与 MCP Origin 校验都使用它 |
| `server.tokens_file` | `~/.config/capifund/tokens.json`，`/mcp` 和 `/api/v1` 的 Bearer token 登记文件 |

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

## Token

`/mcp` 和 `/api/v1` 要求 `Authorization: Bearer <token>`；只读网页、`/static` 和 `/healthz` 不需要 token。登记文件把客户端名称映射到 token，目录必须是 `0700`、文件必须是 `0600`，服务启动时读不到合法登记文件会拒绝启动。服务每次请求都重新读取登记文件，签发和撤销立即生效，不需要重启。

```bash
capifund-admin --config config.yaml token issue --name capifund-client-zip   # 签发；名称已存在时失败
capifund-admin --config config.yaml token list                               # 只列出名称
capifund-admin --config config.yaml token revoke --name capifund-client-zip  # 撤销
```

命令只输出名称，不输出 token 值。token 不绑定作者，持有者都能以任意自报作者写入。

## 接入包

```bash
capifund-admin --config config.yaml client-bundle --token-name capifund-client-zip --output-dir dist
```

命令按名称从登记文件读取 token，生成 `dist/capi_fund_mcp_client_<版本>.zip`（版本取自 `capifund.__version__`，文件权限 `0600`）。服务不提供下载地址，接入包由维护者在内网分发。zip 内顶层目录与文件同名，包含：

- `AGENTS.md`、`README.md`、`SKILL.md` 和 `protocol-1.0.md`；
- 只依赖标准库的 `client.py` 和三个 metadata 示例；
- `mcp.json`：`capifund` 服务的 `public_origin/mcp` 地址和 `Authorization` 头；
- `client.env`：`CAPIFUND_URL` 和 `CAPIFUND_TOKEN`。

`mcp.json` 和 `client.env` 含共享 token，持有接入包即可读写全部内容。轮换时签发新名称、重新生成并分发接入包，再撤销旧名称。

## 内网部署

本机以用户级 systemd 服务运行，配置为 `~/.config/capifund/config.yaml`：`host 0.0.0.0`、`port 8794`、`public_origin http://192.168.0.115:8794`、`data_dir /data1/capi_fin_agent/capifund`、`tokens_file ~/.config/capifund/tokens.json`。

```bash
python3.12 -m venv ~/deploy/capifund/venv
~/deploy/capifund/venv/bin/python -m pip install .           # 在 CapiFund checkout 中执行，非 editable
~/deploy/capifund/venv/bin/capifund-admin --config ~/.config/capifund/config.yaml migrate-database --apply
~/deploy/capifund/venv/bin/capifund-admin --config ~/.config/capifund/config.yaml token issue --name capifund-client-zip
cp deploy/capifund.service.example ~/.config/systemd/user/capifund.service
systemctl --user daemon-reload && systemctl --user enable --now capifund.service
sudo ufw allow from 192.168.0.0/24 to any port 8794 proto tcp comment 'CapiFund LAN'
sudo ufw allow from 10.8.0.0/24 to any port 8794 proto tcp comment 'CapiFund VPN'
```

服务器防火墙只对内网 `192.168.0.0/24` 和 VPN `10.8.0.0/24` 放行 8794。网页不需要 token，这两个网段内的任何人都能浏览库内容。

更新时停止服务，在 checkout 中重新执行 `pip install .`，有迁移时执行 `migrate-database --apply`，再启动服务。冒烟检查只做只读操作：不带 token 访问 `/mcp` 应返回 `401`；解压正式接入包，执行 `set -a; . ./client.env; set +a` 后运行 `python client.py doctor --mcp`。跨机访问需在实际内网或 VPN 客户端上验证。

## 端到端验收

[examples/mcp_acceptance](../examples/mcp_acceptance/README.md) 启动临时实例，用远程 MCP、接入包和 Codex Agent 做端到端验收，不读写正式库。
