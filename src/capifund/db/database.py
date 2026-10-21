"""统一 SQLite 连接、版本检查与离线迁移。"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.resources import files
from typing import Any, Iterator

from .storage import StoragePaths, safe_content_path
from .schema_migrations import MIGRATIONS, run_migrations

SCHEMA_VERSION = MIGRATIONS[-1][0]
PROTOCOL = "1.0"
DATABASE_NAME = "fund.sqlite3"
ACTIVE: ContextVar[tuple[str, sqlite3.Connection, bool] | None] = ContextVar("fund_database", default=None)


def packaged_specification() -> str:
    """当前发行包内的协议规范原文；在线服务只读取数据库中的快照。"""
    return files("capifund").joinpath(f"protocols/{PROTOCOL}.md").read_text(encoding="utf-8")


class Database:
    def __init__(self, storage: StoragePaths, *, pool_size: int = 0):
        self.storage = storage
        self.path = safe_content_path(storage.data_dir, DATABASE_NAME)
        # 多线程进程中每次关闭连接都会解除 WAL 共享内存映射并触发全核 TLB 刷新，常驻服务须复用连接；
        # 离线维护保持 pool_size=0，每个事务独立打开和关闭。
        self.pool_size = pool_size
        self._idle: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        # mode=rw 禁止请求或启动检查意外创建空数据库。
        safe_content_path(self.storage.data_dir, DATABASE_NAME)
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None,
                                     check_same_thread=self.pool_size == 0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _acquire(self) -> sqlite3.Connection:
        with self._lock:
            if self._idle:
                return self._idle.pop()
        return self._open()

    def _release(self, connection: sqlite3.Connection, reusable: bool) -> None:
        if reusable and not connection.in_transaction:
            with self._lock:
                if len(self._idle) < self.pool_size:
                    self._idle.append(connection)
                    return
        connection.close()

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for connection in idle:
            connection.close()

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        active = ACTIVE.get()
        if active is not None and active[0] == str(self.path):
            if write and not active[2]:
                raise ValueError("Cannot upgrade a read transaction")
            yield active[1]
            return
        connection = self._acquire()
        token = None
        reusable = False
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            token = ACTIVE.set((str(self.path), connection, write))
            yield connection
            connection.commit()
            reusable = True
        except BaseException as exc:
            connection.rollback()
            # SQLite 自身报错时不确定连接状态，直接丢弃；业务异常回滚后仍可复用。
            reusable = not isinstance(exc, sqlite3.Error)
            raise
        finally:
            if token is not None:
                ACTIVE.reset(token)
            self._release(connection, reusable)

    def check(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise ValueError("Database missing; stop service and run migrate-database --apply")
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise ValueError(f"Database version {version}; expected {SCHEMA_VERSION}; run offline migration")
            protocol = connection.execute("SELECT value FROM settings WHERE key='protocol'").fetchone()
            if protocol is None or protocol[0] != PROTOCOL:
                raise ValueError("Record protocol requires offline migration")
            snapshot = connection.execute("SELECT specification FROM protocols WHERE version=?", (PROTOCOL,)).fetchone()
            if snapshot is None or not snapshot[0]:
                raise ValueError("Protocol snapshot missing")
            return {"database_version": version, "protocol": protocol[0], "specification": snapshot[0]}

    def initialize(self) -> None:
        self.storage.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 在同目录的临时数据库中完成所有事务，正式路径只在成功后发布。
        temporary = safe_content_path(self.storage.data_dir, f".fund-init-{uuid.uuid4().hex}.sqlite3")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            self.migrate(temporary)
            # link 排他发布，拒绝覆盖已经存在的正式数据库。
            os.link(temporary, self.path)
            descriptor = os.open(self.storage.data_dir, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def migrate(self, path=None):
        connection = sqlite3.connect((path or self.path).as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            run_migrations(connection, self.storage, current)
            connection.execute("UPDATE protocols SET specification=? WHERE version=?", (packaged_specification(), PROTOCOL))
            violations = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
            if violations:
                raise ValueError(f"Foreign key violations after migration: {violations}")
            connection.commit()
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                raise ValueError(f"Cannot enable WAL journal mode: {mode}")
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
