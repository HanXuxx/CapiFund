# CapiFund 接入入口

本文件随 CapiFund 接入包分发，只约束解压后的接入包。服务地址和共享 token 都已写入包内，接入不需要服务器源码。

1. 先读 `README.md`。运行 Agent 的机器需要能访问 `mcp.json` 中的地址。
2. 把 `mcp.json` 中的 `capifund` 服务按当前宿主支持的方式合并到宿主的 MCP 配置，传输方式为 Streamable HTTP，认证头为其中的 `Authorization`。合并后重新加载 MCP 连接；解压文件本身不会自动注册工具。
3. 宿主无法加载远程 MCP 时，执行 `set -a; . ./client.env; set +a` 后改用包内 `client.py` 提供的等价 HTTP 能力，并向用户说明宿主未加载 MCP。
4. 再完整阅读唯一的业务手册 `SKILL.md`。首次操作前读取服务器生效的协议和标签树，再从待办开始工作。研究内容只写入 CapiFund，不保存进 Git。
5. `mcp.json` 和 `client.env` 含共享 token。不要回显、提交到 Git、写入 record 或转发给他人。
