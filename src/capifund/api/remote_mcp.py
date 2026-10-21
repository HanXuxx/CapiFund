"""官方 MCP SDK Streamable HTTP；无认证，写操作携带客户端自报的 author。"""
from __future__ import annotations

from importlib.resources import files
from inspect import Parameter, signature
from typing import Any, get_type_hints
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent
from pydantic import ConfigDict, ValidationError, create_model
from starlette.concurrency import run_in_threadpool

from ..domain.business import BusinessAPI
from ..errors import RecordError
from ..serialization import dumps

INSTRUCTIONS = """CapiFund 是以投资逻辑为核心的中心化投研库，逻辑、验证节点、证据和结论只存于这里，不写入 Git。
一个 type=thesis 主题就是一条投资逻辑；它的回复用 custom.kind 区分 checkpoint（验证节点）与 evidence（证据），楼层即时间线。
工作流程：先调用 fund_protocol 读取协议和保留键、fund_tag_tree 读取标签树；用 fund_agenda 获取到期节点与待复核逻辑；
写之前用 fund_query_records 查重，用 fund_read_topic 分页读取整条逻辑的时间线。
写操作必须携带 author={"id","type":"human|agent","runtime"[,"name"]}，作者自报且不做认证，只编辑自己创建的内容。
每次写操作生成新的 request_id，网络失败用相同 request_id 和相同内容重试；修改携带 expected_revision，
冲突时重新读取、合并后用新 request_id 提交。更新时 custom 整体替换，保留键必须显式给出。
行情、财务、公告等资料从 CapiFinMCP 读取，CapiFund 只保存研究判断及其引用。凭据和受限材料不得写入。
完整手册见 prompt capifund_workflow 或接入包 SKILL.md。"""


def workflow_text() -> str:
    """读取打包的 Agent 工作手册正文，去掉 Skill frontmatter。"""
    text = files("capifund").joinpath("client_bundle/SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---\n"):
        text = text.split("\n---\n", 1)[1].lstrip("\n")
    return text


class FundMCP(FastMCP):
    """在 SDK 的兼容性参数转换之前验证原始 JSON 类型。"""
    def __init__(self, *args, **kwargs):
        self.input_models = {}
        self.handlers = {}
        super().__init__(*args, **kwargs)

    def strict_tool(self):
        def register(function):
            hints = get_type_hints(function)
            fields = {name: (hints[name], ... if parameter.default is Parameter.empty else parameter.default)
                      for name, parameter in signature(function).parameters.items()}
            self.input_models[function.__name__] = create_model(function.__name__ + "Input",
                __config__=ConfigDict(strict=True, extra="forbid"), **fields)
            self.handlers[function.__name__] = function
            return self.tool()(function)
        return register

    async def call_tool(self, name, arguments):
        model = self.input_models.get(name)
        if model is not None:
            try:
                model.model_validate(arguments, strict=True)
            except ValidationError as exc:
                raise ToolError("invalid_request: Invalid tool parameter types or fields") from exc
            # 保留原始值分发，避免 SDK 把可空字符串 "null" 再解释为 None。
            result = await self.handlers[name](**arguments)
            return [TextContent(type="text", text=dumps(result))], result
        return await super().call_tool(name, arguments)


def create_mcp(gateway: BusinessAPI, public_origin: str, run=run_in_threadpool) -> FastMCP:
    host = urlsplit(public_origin).netloc
    mcp = FundMCP("CapiFund", instructions=INSTRUCTIONS, stateless_http=True, json_response=True, streamable_http_path="/mcp",
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                      allowed_hosts=[host, "localhost:*", "127.0.0.1:*", "[::1]:*"], allowed_origins=[public_origin]))

    async def call(operation, arguments):
        arguments.pop("call", None)  # locals() 会包含闭包中的分发函数。
        try:
            return await run(gateway.call, operation, arguments)
        except RecordError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc

    @mcp.prompt(name="capifund_workflow", title="CapiFund 投研工作手册")
    def capifund_workflow() -> str:
        """投资逻辑、验证节点、证据的组织方式，以及查重、request_id 重试和修订冲突流程。"""
        return workflow_text()

    @mcp.strict_tool()
    async def fund_protocol() -> dict[str, Any]:
        """读取当前协议、数据库规范快照、record 类型与投研保留键。"""
        return await call("protocol", {})

    @mcp.strict_tool()
    async def fund_tag_tree() -> dict[str, Any]:
        """读取现有标签树及每个标签的 record 数；新建前先复用已有标签。"""
        return await call("tag_tree", {})

    @mcp.strict_tool()
    async def fund_agenda(as_of: str | None = None, within_days: int = 0) -> dict[str, Any]:
        """返回两张待办清单：due_date 不晚于 as_of+within_days 的 pending 验证节点（所属逻辑未关闭），以及有晚于逻辑最近更新的新证据的 active 逻辑。as_of 为 YYYY-MM-DD，默认 Asia/Shanghai 当天。"""
        return await call("agenda", locals())

    @mcp.strict_tool()
    async def fund_query_records(type: str | None = None, author: str | None = None, tag: str | None = None,
                                 topic_id: str | None = None, custom: dict[str, Any] | None = None,
                                 keyword: str = "", page: int = 1, page_size: int = 50, include_body: bool = False,
                                 sort: str | None = None) -> dict[str, Any]:
        """分页查询；默认只返回 metadata。author 匹配作者 ID 或名称；custom 按顶层键做 JSON 等值筛选，如 {"thesis_status":"active"}。keyword 在完整 metadata/正文中忽略大小写做字面子串匹配。sort: latest/oldest/activity/replies；省略时全局入库倒序，topic_id 内楼层升序。"""
        return await call("query", locals())

    @mcp.strict_tool()
    async def fund_read_record(record_id: str, revision: int | None = None) -> dict[str, Any]:
        """读取当前或历史修订及 Markdown 正文。"""
        return await call("read", locals())

    @mcp.strict_tool()
    async def fund_read_topic(record_id: str, page: int = 1, page_size: int = 200) -> dict[str, Any]:
        """读取主帖及当页按楼层排序的回复；按 total/page/page_size 继续读取后续页。传入回复 ID 时返回其所属主题。"""
        return await call("topic", locals())

    @mcp.strict_tool()
    async def fund_record_history(record_id: str, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        """分页读取历史 metadata 与每次修订的自报编辑者；正文使用 fund_read_record 按修订读取。"""
        return await call("history", locals())

    @mcp.strict_tool()
    async def fund_create_record(metadata: dict[str, Any], body: str, request_id: str, author: dict[str, str]) -> dict[str, Any]:
        """创建主帖或 comment；ID、楼层和时间由服务端生成，保留键缺省时填默认值。"""
        return await call("create", locals())

    @mcp.strict_tool()
    async def fund_update_record(record_id: str, metadata: dict[str, Any], expected_revision: int,
                                 request_id: str, author: dict[str, str], body: str | None = None) -> dict[str, Any]:
        """编辑 record；metadata 只含要替换的字段，custom 整体替换且须显式包含保留键。请求号防重，预期修订号防止覆盖他人的更新。"""
        return await call("update", locals())

    return mcp
