"""HTTP 操作仅依赖标准库的独立客户端；可选 MCP 诊断使用官方 SDK。作者身份来自环境变量自报。"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def env_author():
    """从 CAPIFUND_AUTHOR_* 读取自报作者；只有写操作需要。"""
    author = {"id": os.environ.get("CAPIFUND_AUTHOR_ID", ""), "type": os.environ.get("CAPIFUND_AUTHOR_TYPE", "agent"),
              "runtime": os.environ.get("CAPIFUND_AUTHOR_RUNTIME", "")}
    if os.environ.get("CAPIFUND_AUTHOR_NAME"):
        author["name"] = os.environ["CAPIFUND_AUTHOR_NAME"]
    if not author["id"] or not author["runtime"]:
        raise ValueError("CAPIFUND_AUTHOR_ID and CAPIFUND_AUTHOR_RUNTIME are required for writes")
    return author


class FundClient:
    def __init__(self, url=None, *, author=None, timeout=30):
        self.url = (url or os.environ.get("CAPIFUND_URL", "")).rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
            raise ValueError("CAPIFUND_URL must be an http(s) origin without credentials")
        self.author = author
        self.timeout = timeout

    def request(self, method, path, payload=None):
        if not path.startswith("/api/v1/") or ".." in path.split("?")[0].split("/"):
            raise ValueError("Only /api/v1/ is supported")
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        try:
            with build_opener(NoRedirect()).open(Request(self.url + path, data=body, method=method, headers=headers), timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read(65536).decode("utf-8", errors="replace")
            raise ValueError(f"CapiFund HTTP {exc.code}: {detail}") from None

    def _author(self):
        if self.author is None:
            self.author = env_author()
        return self.author

    def protocol(self):
        return self.request("GET", "/api/v1/protocol")

    def tag_tree(self):
        return self.request("GET", "/api/v1/tag-tree")

    def agenda(self, as_of=None, within_days=0):
        """到期 pending 验证节点与有新证据待复核的 active 逻辑。"""
        parameters = {"within_days": within_days} | ({"as_of": as_of} if as_of else {})
        return self.request("GET", "/api/v1/agenda?" + urlencode(parameters))

    def query(self, **filters):
        """分页检索；sort 可选 latest/oldest/activity/replies，省略时保留服务端默认顺序。"""
        parameters = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool)) else value
                      for key, value in filters.items() if value is not None}
        return self.request("GET", "/api/v1/records?" + urlencode(parameters))

    def read(self, record_id, revision=None):
        return self.request("GET", "/api/v1/records/" + quote(record_id, safe="") + (f"?revision={revision}" if revision else ""))

    def topic(self, record_id, page=1, page_size=200):
        """读取主帖和当页回复；检查 total/page/page_size，继续读取后续页。"""
        return self.request("GET", "/api/v1/records/" + quote(record_id, safe="") + "/topic?" + urlencode(dict(page=page, page_size=page_size)))

    def history(self, record_id, page=1, page_size=50):
        return self.request("GET", "/api/v1/records/" + quote(record_id, safe="") + "/history?" + urlencode(dict(page=page, page_size=page_size)))

    def create(self, metadata, body, request_id):
        return self.request("POST", "/api/v1/records", {"metadata": metadata, "body": body, "request_id": request_id,
                                                        "author": self._author()})

    def update(self, record_id, metadata, expected_revision, request_id, body=None):
        payload = dict(metadata=metadata, expected_revision=expected_revision, request_id=request_id, author=self._author())
        if body is not None:
            payload["body"] = body
        return self.request("PATCH", "/api/v1/records/" + quote(record_id, safe=""), payload)

    def download_bundle(self):
        """下载接入 ZIP，返回原始字节。"""
        try:
            with build_opener(NoRedirect()).open(Request(self.url + "/client.zip"), timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            raise ValueError(f"CapiFund HTTP {exc.code}") from None

    def doctor(self, *, mcp=False):
        """只读检查接入环境；仅 --mcp 需要官方 MCP SDK。"""
        if sys.version_info < (3, 11):
            raise ValueError("Python 3.11+ is required")
        protocol = self.protocol()
        result = {"python": sys.version.split()[0], "protocol": protocol["protocol"],
                  "database_version": protocol["database_version"]}
        try:
            result["author"] = self._author()
        except ValueError as exc:
            result["author_warning"] = str(exc)
        if mcp:
            result["mcp"] = asyncio.run(self._doctor_mcp())
        return result

    async def _doctor_mcp(self):
        try:
            import httpx
            from mcp import ClientSession, McpError
            from mcp.client.streamable_http import streamable_http_client
        except ImportError:
            raise ValueError('Install the MCP SDK: python -m pip install "mcp>=1.28,<2"') from None

        required = {"fund_protocol", "fund_agenda", "fund_create_record"}
        protocol = None
        read_timeout = None if self.timeout is None else timedelta(seconds=self.timeout)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                async with streamable_http_client(self.url + "/mcp", http_client=client) as (read, write, _):
                    async with ClientSession(read, write, read_timeout_seconds=read_timeout) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        missing = required - {tool.name for tool in tools.tools}
                        if not missing:
                            protocol = await session.call_tool("fund_protocol", {})
        except (httpx.HTTPError, McpError, ExceptionGroup) as exc:
            # SDK 的任务组可能多层包装错误，只展示首先发生的失败。
            while isinstance(exc, ExceptionGroup):
                exc = exc.exceptions[0]
            raise ValueError(f"MCP check failed: {type(exc).__name__}: {exc}") from None
        if missing:
            raise ValueError("Missing MCP tools: " + ", ".join(sorted(missing)))
        if protocol.isError:
            raise ValueError("MCP fund_protocol: " + " ".join(item.text for item in protocol.content if item.type == "text"))
        return {"tools": sorted(required), "protocol": protocol.structuredContent["protocol"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="CapiFund record 1.0 client; CAPIFUND_URL and CAPIFUND_AUTHOR_* come from environment")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("protocol")
    commands.add_parser("tag-tree")
    agenda = commands.add_parser("agenda", help="到期验证节点与有新证据待复核的逻辑")
    agenda.add_argument("--as-of", help="YYYY-MM-DD，默认 Asia/Shanghai 当天")
    agenda.add_argument("--within-days", type=int, default=0)
    query = commands.add_parser("query")
    for field in ("type", "author", "tag", "topic-id", "keyword", "custom"):
        query.add_argument("--" + field)
    query.add_argument("--page", type=int, default=1)
    query.add_argument("--page-size", type=int, default=50)
    query.add_argument("--include-body", action="store_true")
    query.add_argument("--sort", choices=("latest", "oldest", "activity", "replies"), default=None)
    read = commands.add_parser("read")
    read.add_argument("record_id")
    read.add_argument("--revision", type=int)
    for name, page_size in (("topic", 200), ("history", 50)):
        command = commands.add_parser(name)
        command.add_argument("record_id")
        command.add_argument("--page", type=int, default=1)
        command.add_argument("--page-size", type=int, default=page_size)
    for name in ("create", "update"):
        command = commands.add_parser(name)
        if name == "update":
            command.add_argument("record_id")
            command.add_argument("--expected-revision", required=True, type=int)
        command.add_argument("--metadata", required=True, type=Path)
        command.add_argument("--body", type=Path, required=name == "create")
        command.add_argument("--request-id", required=True)
    commands.add_parser("doctor", help="只读检查 Python、环境与服务连接").add_argument(
        "--mcp", action="store_true", help="使用可选官方 MCP SDK 验证真实连接")
    download = commands.add_parser("download-bundle", help="下载接入包，不覆盖已有文件")
    download.add_argument("-o", "--output", type=Path, default=Path("capifund-client-1.0.zip"))
    args = vars(parser.parse_args(argv))
    operation = args.pop("command").replace("-", "_")
    try:
        if isinstance(args.get("metadata"), Path):
            args["metadata"] = json.loads(args["metadata"].read_text(encoding="utf-8"))
        if isinstance(args.get("body"), Path):
            args["body"] = args["body"].read_text(encoding="utf-8")
        if operation == "query" and args.get("custom"):
            args["custom"] = json.loads(args["custom"])
        if operation == "download_bundle":
            output = args.pop("output")
            data = FundClient().download_bundle()
            with output.open("xb") as stream:
                stream.write(data)
            result = {"saved": str(output)}
        else:
            result = getattr(FundClient(), operation)(**args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, HTTPException) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
