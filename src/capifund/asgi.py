"""装配入口：只读网页、HTTP、MCP 只调用统一业务 API；HTTP 与 MCP 需要 Bearer token。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.resources import files

from starlette.applications import Starlette
from starlette.routing import Mount, Route, request_response
from starlette.staticfiles import StaticFiles

from .api.auth import BearerGate
from .api.http_api import HttpAccess, HttpEndpoints
from .api.remote_mcp import create_mcp
from .db.database import Database
from .db.persistence import SQLiteFundStorage
from .db.storage import ServiceLease
from .domain.business import BusinessAPI
from .tokens import read_tokens
from .web.views import WebEndpoints

# 数据操作串行于一个执行槽，少量空闲连接即可覆盖。
CONNECTION_POOL_SIZE = 4


def create_app(config):
    try:
        read_tokens(config.tokens_file)
    except (OSError, ValueError):
        # JSON 解析错误可能带出文件内容，不能原样输出。
        raise ValueError(f"server.tokens_file must be valid JSON with directory 0700 / file 0600: {config.tokens_file}") from None
    paths = config.storage
    database = Database(paths, pool_size=CONNECTION_POOL_SIZE)
    database.check()
    business = BusinessAPI(SQLiteFundStorage(paths, database))
    access = HttpAccess(business)
    mcp = create_mcp(business, config.public_origin, access.run)
    http, web = HttpEndpoints(access), WebEndpoints(access)

    @asynccontextmanager
    async def lifespan(app):
        with ServiceLease(paths):
            database.check()
            try:
                async with mcp.session_manager.run():
                    yield
            finally:
                database.close()

    read = ["GET", "HEAD"]
    application = Starlette(routes=[
        Route("/healthz", http.health),
        Route("/mcp", BearerGate(config.tokens_file, mcp.streamable_http_app()), methods=["GET", "POST", "DELETE"]),
        Route("/api/v1/{path:path}", BearerGate(config.tokens_file, request_response(http.api)),
              methods=["GET", "HEAD", "POST", "PATCH"]),
        Mount("/static", StaticFiles(directory=str(files("capifund").joinpath("static")))),
        Route("/", web.theses, methods=read),
        Route("/agenda", web.agenda, methods=read),
        Route("/records", web.records, methods=read),
        Route("/records/{record_id}", web.topic, methods=read),
    ], lifespan=lifespan)
    application.state.business = business
    application.state.mcp = mcp
    return application
