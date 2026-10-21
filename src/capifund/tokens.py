"""Agent 接入 token 登记文件：名称映射到明文 token，修改在下一次请求生效。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

DEFAULT_TOKENS_FILE = Path("~/.config/capifund/tokens.json")


def valid_token(value):
    return isinstance(value, str) and bool(value) and value.isascii() and not any(c.isspace() for c in value)


def read_tokens(path: Path) -> dict[str, str]:
    # 登记文件保存明文凭据，读取前先确认只有服务账号可访问。
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise ValueError("Token directory must have mode 0700")
    with path.open(encoding="utf-8") as source:
        if stat.S_IMODE(os.fstat(source.fileno()).st_mode) != 0o600:
            raise ValueError("Token file must have mode 0600")
        values = json.load(source)
    if not isinstance(values, dict) or any(
        not isinstance(name, str) or not name.strip() or not valid_token(token)
        for name, token in values.items()
    ):
        raise ValueError("Token file must map non-empty client names to ASCII tokens without whitespace")
    return values


def write_tokens(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise ValueError("Token directory must have mode 0700")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(values, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
