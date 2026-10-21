# CapiFund 接入入口

本文件随 client.zip 分发，只约束解压后的接入包。接入所需信息都在包内，不需要服务器源码。

1. 先读 `README.md`，按其中步骤配置地址和自报作者，然后运行 `doctor` 验证接入。
2. 再完整阅读唯一的业务手册 `SKILL.md`。
3. 服务器地址写在 `connection.json` 中。原生 MCP 已连接时使用 `fund_*` 工具；宿主无法加载 MCP 时，改用包内 `client.py` 提供的等价 HTTP 能力。
4. 首次操作前，读取服务器生效的协议和标签树，再从待办开始工作。研究内容只写入 CapiFund，不保存进 Git。
