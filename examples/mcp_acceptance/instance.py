"""为端到端验收启动独立的临时 CapiFund 实例，并构建、解压该实例的接入包。

数据目录和服务日志写入本次运行目录；token 登记文件、zip 和解压后的接入包含 token，只放在被 gitignore 的 client_bundle/。
实例只监听 127.0.0.1 的随机端口，不读写正式库和 ~/.config/capifund。
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import httpx
import yaml

HERE = Path(__file__).resolve().parent
BUNDLE_DIR = HERE / "client_bundle"
DEFAULT_CONFIG = HERE / "config.yaml.example"
SHANGHAI = timezone(timedelta(hours=8))
TOKEN_NAME = "acceptance"


@dataclass(frozen=True)
class Instance:
    origin: str
    mcp_url: str
    token: str
    client_py: Path
    data_dir: Path


def log(message: str) -> None:
    print(f"[{datetime.now(SHANGHAI).strftime('%H:%M:%S')}] {message}", flush=True)


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def new_run_dir(config: dict, suffix: str = "") -> Path:
    run_dir = Path(config["output_root"]).expanduser() / (datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S") + suffix)
    run_dir.mkdir(parents=True)
    return run_dir


def leaked_files(root: Path, token: str) -> list[str]:
    return [str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and token.encode() in path.read_bytes()]


@contextmanager
def start_instance(run_dir: Path) -> Iterator[Instance]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    data_dir = run_dir / "capifund"
    shutil.rmtree(BUNDLE_DIR, ignore_errors=True)
    BUNDLE_DIR.mkdir()
    common = ["--data-dir", str(data_dir), "--tokens-file", str(BUNDLE_DIR / "tokens" / "tokens.json"),
              "--host", "127.0.0.1", "--port", str(port), "--public-origin", origin]

    def admin(*arguments: str) -> None:
        subprocess.run([sys.executable, "-m", "capifund.admin", *common, *arguments], check=True, capture_output=True, text=True)

    admin("migrate-database", "--apply")
    admin("token", "issue", "--name", TOKEN_NAME)
    admin("client-bundle", "--token-name", TOKEN_NAME, "--output-dir", str(BUNDLE_DIR))
    archive = next(BUNDLE_DIR.glob("capi_fund_mcp_client_*.zip"))
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(BUNDLE_DIR)
    package = BUNDLE_DIR / archive.stem
    server = json.loads((package / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["capifund"]
    instance = Instance(origin, server["url"], server["headers"]["Authorization"].removeprefix("Bearer "),
                        package / "client.py", data_dir)
    log(f"Bundle extracted to {package}")

    with (run_dir / "capifund-server.log").open("w") as server_log:
        process = subprocess.Popen([sys.executable, "-m", "capifund.cli", *common], stdout=server_log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if httpx.get(origin + "/healthz", timeout=2, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"CapiFund did not start; see {server_log.name}")
                time.sleep(0.2)
            log(f"CapiFund started at {origin}, data dir {data_dir}")
            yield instance
        finally:
            process.terminate()
            process.wait(10)
            log(f"CapiFund stopped with exit code {process.returncode}")


def write_report(run_dir: Path, title: str, url: str, checks: list[dict], extra: dict, footer: str) -> bool:
    passed = all(check["ok"] for check in checks)
    report = {"run": run_dir.name, "capifund_url": url, "passed": passed, "checks": checks, **extra}
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    lines = [f"# {title} {run_dir.name}", "",
             f"结论：{'全部通过' if passed else '存在失败项'}（{sum(c['ok'] for c in checks)}/{len(checks)}）。服务地址 `{url}`。", "",
             "| 阶段 | 检查 | 结果 | 耗时（秒） | 说明 |", "| --- | --- | --- | --- | --- |"]
    for check in checks:
        detail = str(check["detail"]).replace("|", "\\|").replace("\n", " ")[:200]
        lines.append(f"| {check['phase']} | `{check['name']}` | {'通过' if check['ok'] else '失败'} | {check['seconds']} | {detail} |")
    lines += ["", footer, ""]
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return passed
