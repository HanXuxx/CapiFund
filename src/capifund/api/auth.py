"""/mcp 与 /api/v1 的 Bearer 校验；作者身份仍由请求自报。"""
from __future__ import annotations

import logging
import secrets

from starlette.responses import Response

from ..tokens import read_tokens

logger = logging.getLogger(__name__)


class BearerGate:
    """包装一个 ASGI 应用；每次请求读取登记文件，签发和撤销立即生效。"""

    def __init__(self, tokens_file, app):
        self.tokens_file, self.app = tokens_file, app

    async def __call__(self, scope, receive, send):
        supplied = dict(scope["headers"]).get(b"authorization", b"")
        try:
            tokens = list(read_tokens(self.tokens_file).values())
        except (OSError, ValueError):
            logger.error("CapiFund token registry unreadable or invalid; request denied")
            tokens = []
        accepted = False
        for token in tokens:
            accepted |= secrets.compare_digest(supplied, ("Bearer " + token).encode("ascii"))
        if not accepted:
            await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        await self.app(scope, receive, send)
