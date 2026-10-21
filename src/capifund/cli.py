"""CapiFund 服务启动入口：加载配置后以单 worker 运行 ASGI 应用。"""
from __future__ import annotations

import argparse
import sys

from .config import add_config_arguments, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_config_arguments(parser)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args)
        if args.check_config:
            print("CapiFund configuration OK")
            return 0
        from .asgi import create_app
        import uvicorn
        app = create_app(config)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"invalid CapiFund configuration: {exc}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=config.host, port=config.port, workers=1, access_log=args.verbose, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
