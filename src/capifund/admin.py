"""CapiFund 离线维护：初始化或迁移数据库、校验、在线备份、管理 token 和生成接入包。"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

from .config import add_config_arguments, load_config
from .errors import StorageFailure
from .tokens import read_tokens, write_tokens


def load_registry(path: Path) -> dict[str, str]:
    try:
        return read_tokens(path) if path.exists() else {}
    except (OSError, ValueError):
        # JSON 解析错误可能带出文件内容，不能原样输出。
        raise ValueError(f"cannot read token registry {path}: check JSON and directory 0700 / file 0600") from None


def tokens(path: Path, command: str, name: str | None) -> dict:
    """签发、撤销或列出 token；只返回名称，不返回 token 值。"""
    values = load_registry(path)
    if command == "list":
        return {"tokens_file": str(path), "names": sorted(values)}
    if not name or not name.strip() or any(char in name for char in "\r\n\0"):
        raise ValueError("token name must be non-empty without newline or NUL")
    if command == "issue":
        if name in values:
            raise ValueError(f"token name already exists: {name}")
        values[name] = secrets.token_urlsafe(48)
    else:
        if name not in values:
            raise ValueError(f"token name not found: {name}")
        del values[name]
    write_tokens(path, values)
    return {"tokens_file": str(path), command: name}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_arguments(parser)
    subparsers = parser.add_subparsers(dest="command")
    migration = subparsers.add_parser("migrate-database", help="Preview or initialize/migrate the runtime database")
    migration.add_argument("--apply", action="store_true", help="Apply changes; default is preview only")
    subparsers.add_parser("validate-database", help="Check SQLite integrity, revisions, projections and WAL")
    backup = subparsers.add_parser("backup", help="Back up SQLite while the service is running")
    backup.add_argument("destination", type=Path)
    token = subparsers.add_parser("token", help="Issue, revoke or list Bearer tokens in server.tokens_file")
    token.add_argument("token_command", choices=("issue", "revoke", "list"))
    token.add_argument("--name")
    bundle = subparsers.add_parser("client-bundle", help="Build the agent client ZIP with an embedded shared token")
    bundle.add_argument("--token-name", required=True)
    bundle.add_argument("--output-dir", type=Path, default=Path("dist"))
    args = parser.parse_args(argv)
    if not args.command and not args.check_config:
        parser.error("a command is required unless --check-config is used")
    try:
        config = load_config(args)
        if args.check_config:
            print("CapiFund configuration OK")
            return 0
        from .db.maintenance import backup_database, migrate_database, validate_database
        if args.command == "migrate-database":
            result = migrate_database(config.storage, apply=args.apply)
        elif args.command == "validate-database":
            from .db.storage import ServiceLease
            with ServiceLease(config.storage):
                result = validate_database(config.storage)
        elif args.command == "backup":
            result = backup_database(config.storage, args.destination)
        elif args.command == "token":
            result = tokens(config.tokens_file, args.token_command, args.name)
        else:
            from .api.bundle import build_bundle
            values = load_registry(config.tokens_file)
            if args.token_name not in values:
                raise ValueError(f"token name not found: {args.token_name}")
            result = {"bundle": str(build_bundle(config.public_origin, values[args.token_name], args.output_dir.expanduser()))}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("valid", True) else 2
    except StorageFailure:
        print("CapiFund storage is temporarily unavailable", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
