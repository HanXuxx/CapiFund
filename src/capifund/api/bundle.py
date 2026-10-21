"""生成 Agent 接入包：客户端、协议、手册，以及内嵌共享 token 的 MCP 和 client.py 配置。"""
from __future__ import annotations

import json
import tempfile
import zipfile
from importlib.resources import files
from pathlib import Path

from .. import __version__
from ..config_validation import validate_origin

BUNDLE_NAME = "capi_fund_mcp_client_" + __version__.replace(".", "_")


def build_bundle(public_origin: str, token: str, output_dir: Path) -> Path:
    validate_origin(public_origin)
    origin = public_origin.rstrip("/")
    configuration = {"mcpServers": {"capifund": {"type": "http", "url": origin + "/mcp",
                                                 "headers": {"Authorization": "Bearer " + token}}}}
    package = files("capifund")
    entries = {"client.py": package.joinpath("client.py").read_bytes(),
               "protocol-1.0.md": package.joinpath("protocols/1.0.md").read_bytes(),
               "mcp.json": (json.dumps(configuration, ensure_ascii=False, indent=2) + "\n").encode(),
               "client.env": f"CAPIFUND_URL={origin}\nCAPIFUND_TOKEN={token}\n".encode()}
    for file in package.joinpath("client_bundle").iterdir():
        if file.is_file():
            data = file.read_bytes()
            entries[file.name] = data.decode().replace("@@FUND_URL@@", origin).encode() if file.name == "README.md" else data
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{BUNDLE_NAME}.zip"
    # 临时文件默认 0600；包内含 token，替换后保持该权限。
    with tempfile.NamedTemporaryFile(dir=output_dir, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(entries):
                archive.writestr(f"{BUNDLE_NAME}/{name}", entries[name])
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
