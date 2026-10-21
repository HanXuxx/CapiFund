"""生成独立接入包：客户端、协议、Agent 手册和 MCP 配置。"""
import io
import json
import zipfile
from importlib.resources import files

from ..config_validation import validate_origin

BUNDLE_NAME = "capifund-client-1.0.zip"


def bundle_bytes(public_origin: str) -> bytes:
    validate_origin(public_origin)
    origin = public_origin.rstrip("/")
    connection = {"fund_url": origin, "mcp_url": origin + "/mcp", "transport": "Streamable HTTP",
                  "download_url": origin + "/client.zip", "minimum_python": "3.11"}
    configuration = {"mcpServers": {"capifund": {"type": "http", "url": origin + "/mcp"}}}
    package = files("capifund")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("client.py", package.joinpath("client.py").read_bytes())
        archive.writestr("protocol-1.0.md", package.joinpath("protocols/1.0.md").read_bytes())
        for file in sorted(package.joinpath("client_bundle").iterdir(), key=lambda item: item.name):
            if file.is_file():
                data = file.read_bytes()
                if file.name == "README.md":
                    data = data.decode().replace("@@FUND_URL@@", origin).encode()
                archive.writestr(file.name, data)
        archive.writestr("connection.json", json.dumps(connection, ensure_ascii=False, indent=2) + "\n")
        archive.writestr("mcp.json", json.dumps(configuration, ensure_ascii=False, indent=2) + "\n")
    return stream.getvalue()
