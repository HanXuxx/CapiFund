"""HTTP 接入：请求体读取与 /api/v1 声明式路由；不做身份认证，作者由请求自报。"""
from __future__ import annotations

import json
from functools import partial
from typing import NamedTuple

import anyio
import anyio.to_thread
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from ..errors import NotFound, RecordError
from ..serialization import unique_mapping
from .bundle import BUNDLE_NAME, bundle_bytes

MAX_REQUEST = 4 * 1024 * 1024


class ApiRoute(NamedTuple):
    method: str
    # 路径段模式；"{name}" 匹配任意单段并作为同名参数，覆盖请求中的同名字段。
    pattern: tuple[str, ...]
    operation: str
    status: int = 200


# GET 与 HEAD 共用读路由；按顺序匹配，先写字面量路径。
READ_ROUTES = (
    ApiRoute("GET", ("protocol",), "protocol"),
    ApiRoute("GET", ("tag-tree",), "tag_tree"),
    ApiRoute("GET", ("agenda",), "agenda"),
    ApiRoute("GET", ("records",), "query"),
    ApiRoute("GET", ("records", "{record_id}"), "read"),
    ApiRoute("GET", ("records", "{record_id}", "history"), "history"),
    ApiRoute("GET", ("records", "{record_id}", "topic"), "topic"),
)

WRITE_ROUTES = (
    ApiRoute("POST", ("records",), "create", 201),
    ApiRoute("PATCH", ("records", "{record_id}"), "update"),
)


def bind(pattern, segments):
    if len(pattern) != len(segments):
        return None
    parameters = {}
    for expected, actual in zip(pattern, segments):
        if expected.startswith("{"):
            parameters[expected[1:-1]] = actual
        elif expected != actual:
            return None
    return parameters


def match_route(routes, method, segments):
    for route in routes:
        if route.method == method:
            parameters = bind(route.pattern, segments)
            if parameters is not None:
                return route, parameters
    raise NotFound("Unknown path")


def read_arguments(query_params):
    arguments = dict(query_params)
    for field in ("page", "page_size", "revision", "within_days"):
        if field in arguments:
            arguments[field] = int(arguments[field])
    if "custom" in arguments:
        arguments["custom"] = json.loads(arguments["custom"], object_pairs_hook=unique_mapping)
    if "include_body" in arguments:
        if arguments["include_body"] not in {"true", "false"}:
            raise RecordError("include_body must be true or false")
        arguments["include_body"] = arguments["include_body"] == "true"
    return arguments


def error_response(exc):
    status, code = getattr(exc, "status", 400), getattr(exc, "code", "invalid_request")
    return JSONResponse({"error": str(exc), "code": code}, status_code=status)


class HttpAccess:
    """网页、HTTP 与 MCP 共用的请求体读取与业务调用。"""

    def __init__(self, business, public_origin):
        self.business, self.public_origin = business, public_origin
        # sqlite3 每条语句都释放并重取 GIL，多线程同时读写时吞吐成倍下降；
        # 数据操作因此共用一个执行槽，排队在事件循环中进行，不占工作线程。
        self.fund_slot = anyio.CapacityLimiter(1)

    async def run(self, function, *args, **kwargs):
        return await anyio.to_thread.run_sync(partial(function, *args, **kwargs), limiter=self.fund_slot)

    async def body(self, request):
        value = bytearray()
        async for chunk in request.stream():
            value.extend(chunk)
            if len(value) > MAX_REQUEST:
                raise RecordError(f"Request exceeds {MAX_REQUEST // (1024 * 1024)} MiB")
        return bytes(value)

    async def call(self, operation, arguments):
        return await self.run(self.business.call, operation, arguments)


class HttpEndpoints:
    """/api/v1 与接入包入口；只做解析与响应转换。"""

    def __init__(self, access):
        self.access = access

    async def api(self, request):
        access = self.access
        try:
            segments = tuple(request.path_params.get("path", "").strip("/").split("/"))
            if request.method in {"GET", "HEAD"}:
                # 参数转换先于路由匹配，未知路径上的非法分页参数仍按 400 返回。
                arguments = read_arguments(request.query_params)
                route, parameters = match_route(READ_ROUTES, "GET", segments)
            else:
                # 要求 JSON 使跨站浏览器请求必须先通过 CORS 预检；服务不返回 CORS 头。
                if not request.headers.get("content-type", "").startswith("application/json"):
                    raise RecordError("JSON required")
                arguments = json.loads(await access.body(request), object_pairs_hook=unique_mapping)
                if not isinstance(arguments, dict):
                    raise RecordError("JSON object required")
                route, parameters = match_route(WRITE_ROUTES, request.method, segments)
            arguments.update(parameters)
            return JSONResponse(await access.call(route.operation, arguments), status_code=route.status)
        except (RecordError, ValueError) as exc:
            return error_response(exc)

    async def bundle(self, request):
        return Response(await run_in_threadpool(bundle_bytes, self.access.public_origin), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{BUNDLE_NAME}"'})

    async def health(self, request):
        return JSONResponse({"status": "ok"})
