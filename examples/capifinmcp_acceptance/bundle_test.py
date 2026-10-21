"""解压 CapiFinMCP 客户端 zip 并安装，再用解压包中的客户端运行全量验收和 Codex Agent 验收。

解压目录 client_bundle/ 含共享 token，已被 gitignore；安装使用其中的隔离 HOME，不改动本机 ~/.config/capifin。
服务地址和 token 取自包内 mcp.json 与 client.env；验收报告写入 output_root 下的 <时间戳>-bundle/。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import yaml

from agent_e2e import DEFAULT_CODEX_CONFIG
from run import DEFAULT_CONFIG, load_config, load_token, log, new_run_dir, write_report

HERE = Path(__file__).resolve().parent
BUNDLE_DIR = HERE / "client_bundle"
DEFAULT_BUNDLE = HERE.parents[1] / "vendor/CapiFinMCP/dist/capi_fin_mcp_client_1_0_0.zip"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="提供样本和 output_root；服务地址与 token 以包内为准")
    parser.add_argument("--codex-config", type=Path, default=DEFAULT_CODEX_CONFIG)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    run_dir = new_run_dir(config, "-bundle")
    log(f"Run directory: {run_dir}")
    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str, started: float) -> bool:
        checks.append({"phase": "Bundle", "name": name, "ok": ok, "detail": detail, "seconds": round(time.monotonic() - started, 2)})
        log(f"{'PASS' if ok else 'FAIL'} Bundle/{name}: {detail}")
        return ok

    def finish(url: str) -> int:
        passed = write_report(run_dir, "CapiFinMCP 客户端安装包验收报告", url, checks, {"bundle": str(args.bundle)},
                              "全量验收和 Codex 验收的报告位于本目录下各自的时间戳子目录。")
        log(f"{'PASSED' if passed else 'FAILED'}; report: {run_dir / 'report.md'}")
        return 0 if passed else 1

    started = time.monotonic()
    shutil.rmtree(BUNDLE_DIR, ignore_errors=True)
    BUNDLE_DIR.mkdir()
    with zipfile.ZipFile(args.bundle) as archive:
        archive.extractall(BUNDLE_DIR)
    package = BUNDLE_DIR / args.bundle.stem
    server = json.loads((package / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["capifinmcp"]
    record("extract", True, f"{args.bundle.name} -> {package}; url {server['url']}", started)

    # 本进程和子进程都不使用外部 token，验收只能使用包内 token。
    os.environ.pop("CAPIFIN_MCP_TOKEN", None)
    env = dict(os.environ)
    home = BUNDLE_DIR / "home"
    home.mkdir()
    started = time.monotonic()
    install = subprocess.run([sys.executable, "install.py"], cwd=package, env={**env, "HOME": str(home)},
                             capture_output=True, text=True)
    (run_dir / "install.log").write_text(install.stdout + install.stderr, encoding="utf-8")
    if not record("install", install.returncode == 0, f"exit code {install.returncode}; log install.log", started):
        return finish(server["url"])

    venv = home / ".local/share/capifin/venv"
    site_packages = next(venv.glob("lib/python*/site-packages"))
    sub_env = {**env, "PYTHONPATH": str(site_packages)}
    started = time.monotonic()
    located = subprocess.run([sys.executable, "-c", "import capifin_mcp_client; print(capifin_mcp_client.__file__)"],
                             env=sub_env, capture_output=True, text=True).stdout.strip()
    record("client_from_bundle", located.startswith(str(BUNDLE_DIR)), located, started)

    mcp = {"url": server["url"], "token_file": str(package / "client.env"), "timeout_seconds": config["capifinmcp"].get("timeout_seconds", 120)}
    acceptance_config = run_dir / "config.yaml"
    acceptance_config.write_text(yaml.safe_dump({**config, "capifinmcp": mcp, "output_root": str(run_dir)}, allow_unicode=True),
                                 encoding="utf-8")
    for name, script, extra in (
        ("run", "run.py", []),
        ("agent_e2e", "agent_e2e.py", ["--codex-config", str(args.codex_config), "--client-cli", str(venv / "bin/capifin-mcp")]),
    ):
        started = time.monotonic()
        log(f"Running {script} with the bundle client")
        process = subprocess.run([sys.executable, str(HERE / script), "--config", str(acceptance_config), *extra], env=sub_env)
        record(name, process.returncode == 0, f"exit code {process.returncode}", started)

    started = time.monotonic()
    token = load_token(mcp)
    leaked = [str(path.relative_to(run_dir)) for path in run_dir.rglob("*") if path.is_file() and token.encode() in path.read_bytes()]
    record("no_token_in_outputs", not leaked, f"token found in {leaked}" if leaked else "token absent from run outputs", started)
    return finish(server["url"])


if __name__ == "__main__":
    raise SystemExit(main())
