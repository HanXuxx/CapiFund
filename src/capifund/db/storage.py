"""运行数据目录的路径边界与服务独占锁。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StoragePaths:
    data_dir: Path

    def __post_init__(self) -> None:
        # 保留末级符号链接，交给 safe_content_path 的安全检查。
        object.__setattr__(self, "data_dir", Path(self.data_dir).absolute())


class ServiceLease:
    """服务和离线维护共用独占锁，避免检查停服后又启动服务的竞争。"""
    def __init__(self, paths: StoragePaths):
        import fcntl
        path = safe_content_path(paths.data_dir, ".service.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("a+b")
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise ValueError("CapiFund is running or under offline maintenance; stop the service first") from None

    def close(self) -> None:
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def safe_content_path(root: Path, relative: Any) -> Path:
    """只接受根目录下的普通路径；所有层级都拒绝符号链接。"""
    root = Path(root).absolute()
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("invalid content path")
    rel = Path(relative)
    if rel.is_absolute() or any(part in {".", ".."} for part in relative.split("/")):
        raise ValueError("content path escapes root")
    candidate = root / rel
    if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents):
        raise ValueError("content path must not contain symbolic links")
    candidate.resolve().relative_to(root.resolve())
    return candidate
