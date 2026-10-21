"""按序迁移：DDL 与数据转换共用调用方持有的单个事务。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from importlib.metadata import version
from importlib.resources import files


def execute_ddl(connection, text):
    statement = ""
    for character in text:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        connection.execute(statement)


def initialize_state(connection, paths):
    # 协议正文由 Database.migrate 在同一事务末尾写入当前发行包的快照。
    connection.execute("INSERT INTO settings VALUES ('protocol', '1.0')")
    connection.execute("INSERT INTO protocols VALUES ('1.0', '')")


MIGRATIONS = (
    (1, "001_initial", initialize_state),
)


def run_migrations(connection, paths, current_version):
    applied_at = datetime.now(timezone.utc).isoformat()
    package_version = version("capifund")
    for number, name, transform in MIGRATIONS:
        if number <= current_version:
            continue
        execute_ddl(connection, files("capifund").joinpath(f"migrations/{name}.sql").read_text(encoding="utf-8"))
        if transform is not None:
            transform(connection, paths)
        connection.execute(f"PRAGMA user_version={number}")
        connection.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (number, applied_at, package_version))
