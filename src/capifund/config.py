"""启动配置：命令行 > YAML > 默认值。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config_validation import ConfigError, validate_hostname, validate_origin
from .db.storage import StoragePaths

DEFAULT_DATA_DIR = Path("/data1/capi_fin_agent/capifund")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8793
SCHEMA = {
    "storage": {"data_dir": str},
    "server": {"host": str, "port": int, "public_origin": str},
}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise ConfigError("configuration keys must be strings")
        if key in result:
            raise ConfigError(f"duplicate configuration key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _read_yaml(path: Path) -> dict[str, dict[str, Any]]:
    try:
        values = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(values, dict):
        raise ConfigError("config must be a mapping")
    for section, fields in values.items():
        if section not in SCHEMA:
            raise ConfigError(f"unknown configuration section: {section}")
        if not isinstance(fields, dict):
            raise ConfigError(f"{section} must be a mapping")
        for key, value in fields.items():
            if key not in SCHEMA[section]:
                raise ConfigError(f"unknown configuration field: {section}.{key}")
            if type(value) is not SCHEMA[section][key]:
                raise ConfigError(f"invalid type for {section}.{key}")
            if isinstance(value, str) and (not value or value != value.strip() or "\x00" in value):
                raise ConfigError(f"invalid value for {section}.{key}")
    return values


@dataclass(frozen=True)
class FundConfig:
    data_dir: Path
    host: str
    port: int
    public_origin: str

    @property
    def storage(self) -> StoragePaths:
        return StoragePaths(self.data_dir)


def add_config_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="Optional YAML configuration file")
    parser.add_argument("--check-config", action="store_true", help="Validate configuration without listening or creating data")
    parser.add_argument("--data-dir", type=Path, help=f"Runtime data directory (default: {DEFAULT_DATA_DIR})")
    parser.add_argument("--host", help=f"Listen host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, help=f"Listen port (default: {DEFAULT_PORT})")
    parser.add_argument("--public-origin", help="Origin that clients use to reach the service")


def load_config(args: argparse.Namespace) -> FundConfig:
    config_path = args.config.expanduser().resolve() if args.config is not None else None
    values = _read_yaml(config_path) if config_path is not None else {}

    def pick(name: str, section: str, key: str, default: Any) -> Any:
        cli = getattr(args, name, None)
        if cli is not None:
            return cli
        if key in values.get(section, {}):
            value = values[section][key]
            if key == "data_dir":
                path = Path(value).expanduser()
                return config_path.parent / path if not path.is_absolute() else path
            return value
        return default

    data_dir = Path(pick("data_dir", "storage", "data_dir", DEFAULT_DATA_DIR)).absolute()
    host = pick("host", "server", "host", DEFAULT_HOST)
    port = pick("port", "server", "port", DEFAULT_PORT)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ConfigError("server.port must be between 0 and 65535")
    validate_hostname(host, "server.host")
    default_host = host if host not in {"0.0.0.0", "::"} else "localhost"
    if ":" in default_host:
        default_host = f"[{default_host}]"
    origin = pick("public_origin", "server", "public_origin", f"http://{default_host}:{port}")
    validate_origin(origin)
    if data_dir.is_symlink() or (data_dir.exists() and not data_dir.is_dir()):
        raise ConfigError(f"storage.data_dir must be a directory, not a symlink: {data_dir}")
    return FundConfig(data_dir, host, port, origin.rstrip("/"))
