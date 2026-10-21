"""配置使用的纯地址校验，不读取配置来源。"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def validate_hostname(value: str, field: str) -> None:
    try:
        ipaddress.ip_address(value)
        return
    except ValueError:
        pass
    if re.fullmatch(r"[0-9.]+", value):
        raise ConfigError(f"{field} contains an invalid IP address")
    if len(value) > 253 or not all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
        for part in value.split(".")
    ):
        raise ConfigError(f"{field} must be a hostname or IP address")


def validate_origin(value: str) -> None:
    try:
        if any(ord(char) <= 32 or ord(char) >= 127 for char in value):
            raise ValueError("origin must contain printable ASCII without whitespace")
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or "?" in value or "#" in value
            or parsed.netloc.endswith(":")
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError("expected an http(s) origin without credentials, path or query")
        validate_hostname(parsed.hostname, "server.public_origin hostname")
    except ValueError as exc:
        raise ConfigError(f"invalid server.public_origin: {exc}") from exc
