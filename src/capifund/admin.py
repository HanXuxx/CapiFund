"""CapiFund 离线维护：初始化或迁移数据库、校验、在线备份和生成接入包。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import add_config_arguments, load_config
from .errors import StorageFailure


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_arguments(parser)
    subparsers = parser.add_subparsers(dest="command")
    migration = subparsers.add_parser("migrate-database", help="Preview or initialize/migrate the runtime database")
    migration.add_argument("--apply", action="store_true", help="Apply changes; default is preview only")
    subparsers.add_parser("validate-database", help="Check SQLite integrity, revisions, projections and WAL")
    backup = subparsers.add_parser("backup", help="Back up SQLite while the service is running")
    backup.add_argument("destination", type=Path)
    bundle = subparsers.add_parser("client-bundle", help="Build the client ZIP for this instance")
    bundle.add_argument("output", type=Path)
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
        else:
            from .api.bundle import bundle_bytes
            with args.output.open("xb") as stream:
                stream.write(bundle_bytes(config.public_origin))
            result = {"bundle": str(args.output)}
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
