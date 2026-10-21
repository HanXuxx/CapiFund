"""离线迁移、校验与在线备份，只处理运行数据；校验只报告差异，不修复。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .database import DATABASE_NAME, PROTOCOL, SCHEMA_VERSION, Database, packaged_specification
from .schema_migrations import MIGRATIONS
from .storage import ServiceLease, StoragePaths
from ..domain.tags import display_tags
from ..serialization import dumps

PROJECTED = (("type", "type"), ("title", "title"), ("status", "status"),
             ("author_name", "author"), ("created_at", "created_at"), ("updated_at", "updated_at"))


def backup_database(paths: StoragePaths, destination: Path) -> dict[str, str]:
    source_path = Database(paths).path
    destination = Path(destination).absolute()
    if destination.resolve().is_relative_to(paths.data_dir.resolve()):
        raise ValueError("Backup destination must be outside the data directory")
    destination.mkdir(mode=0o700)
    target = destination / DATABASE_NAME
    target.touch(mode=0o600, exist_ok=False)
    try:
        with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(target)) as output:
            version = source.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise ValueError(f"Online backup requires database version {SCHEMA_VERSION}; found {version}")
            source.backup(output)
    except (sqlite3.Error, ValueError) as exc:
        target.unlink(missing_ok=True)
        destination.rmdir()
        raise ValueError(f"Database backup failed: {exc}") from exc
    return {"backup": str(destination)}


def validate_database(paths: StoragePaths) -> dict[str, Any]:
    database = Database(paths)
    report = database.check()
    errors = []
    with database.connect() as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            errors.append("SQLite integrity check failed")
        errors.extend(str(tuple(row)) for row in connection.execute("PRAGMA foreign_key_check"))
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if journal_mode != "wal":
            errors.append(f"Unexpected journal mode: {journal_mode}; expected wal")
        for row in connection.execute("SELECT hash,body FROM record_bodies"):
            if hashlib.sha256(row["body"].encode("utf-8")).hexdigest() != row["hash"]:
                errors.append(f"Body hash mismatch: {row['hash']}")
        errors.extend(_validate_records(connection))
        counts = {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                  for name in ("records", "record_revisions", "record_bodies")}
    # 快照与发行包不一致不影响数据完整性，只提示停服后执行 migrate-database --apply 刷新。
    snapshot_current = report["specification"] == packaged_specification()
    warnings = [] if snapshot_current else ["Protocol snapshot differs from the packaged specification; stop service and run migrate-database --apply"]
    return {key: value for key, value in report.items() if key != "specification"} | {
        "valid": not errors, "errors": errors, "warnings": warnings, "protocol_snapshot_current": snapshot_current,
        "journal_mode": journal_mode, **counts}


def _validate_records(connection):
    """从当前修订核对 records、标签、custom、搜索与主题活动投影。"""
    errors, created, children, activity = [], {}, {}, {}
    rows = connection.execute("""SELECT r.*,v.metadata,b.body FROM records r
        LEFT JOIN record_revisions v ON v.record_id=r.id AND v.revision=r.current_revision
        LEFT JOIN record_bodies b ON b.hash=v.body_hash ORDER BY r.rowid""").fetchall()
    for row in rows:
        record_id, label = row["id"], f"{row['id']}@{row['current_revision']}"
        revisions = [entry[0] for entry in connection.execute(
            "SELECT revision FROM record_revisions WHERE record_id=? ORDER BY revision", (record_id,))]
        if revisions != list(range(1, row["current_revision"] + 1)):
            errors.append(f"Broken revision sequence: {record_id}")
        if row["metadata"] is None or row["body"] is None:
            errors.append(f"Missing current revision or body: {label}")
            continue
        try:
            metadata = json.loads(row["metadata"])
            values = tuple(metadata[key] for _, key in PROJECTED)
            tags = set(display_tags(metadata["tags"]))
            custom = {(key, dumps(value)) for key, value in metadata["custom"].items()}
        except (KeyError, TypeError, AttributeError, ValueError):
            errors.append(f"Invalid current metadata: {label}")
            continue
        created[record_id] = metadata["created_at"]
        activity[record_id] = (row["reply_count"], row["last_activity_at"])
        if row["topic_id"]:
            children.setdefault(row["topic_id"], []).append(record_id)
            parent = connection.execute("SELECT topic_id FROM records WHERE id=?", (row["topic_id"],)).fetchone()
            target = connection.execute("SELECT id,topic_id FROM records WHERE id=?", (row["reply_to_id"],)).fetchone()
            if not parent or parent[0] or not row["floor"] or not target or row["topic_id"] not in {target["id"], target["topic_id"]}:
                errors.append(f"Broken reply relationship: {record_id}")
        mismatches = [column for (column, _), value in zip(PROJECTED, values) if row[column] != value]
        if mismatches:
            errors.append(f"Record projection mismatch: {label} ({','.join(mismatches)})")
        if {entry[0] for entry in connection.execute("SELECT tag FROM record_tags WHERE record_id=?", (record_id,))} != tags:
            errors.append(f"Tag projection mismatch: {label}")
        if {tuple(entry) for entry in connection.execute("SELECT key,value_json FROM record_custom WHERE record_id=?", (record_id,))} != custom:
            errors.append(f"Custom projection mismatch: {label}")
        search = connection.execute("SELECT metadata,body FROM record_search WHERE record_id=?", (record_id,)).fetchone()
        if search is None or tuple(search) != (dumps(metadata).casefold(), row["body"].casefold()):
            errors.append(f"Search projection mismatch: {label}")
    for record_id, (count, last_activity) in activity.items():
        replies = children.get(record_id, [])
        if count != len(replies) or last_activity != max([created[record_id], *(created[reply] for reply in replies if reply in created)]):
            errors.append(f"Activity projection mismatch: {record_id}")
    return errors


def migrate_database(paths: StoragePaths, *, apply=False):
    database = Database(paths)

    def inspect_existing():
        with database.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        if not 1 <= version <= SCHEMA_VERSION:
            raise ValueError(f"No migration from database version {version}")
        pending = [name for number, name, _ in MIGRATIONS if number > version]
        if pending:
            return {"applied": False, "database_version": version,
                    "target_database_version": SCHEMA_VERSION, "pending_migrations": pending}
        report = validate_database(paths)
        return {"already_migrated": True, "pending_migrations": [],
                "protocol_snapshot_refresh": not report["protocol_snapshot_current"], **report}

    if not apply:
        if database.path.exists():
            return inspect_existing()
        return {"applied": False, "target_database_version": SCHEMA_VERSION,
                "pending_migrations": [name for _, name, _ in MIGRATIONS]}
    paths.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ServiceLease(paths):
        if not database.path.exists():
            database.initialize()
            return {"applied": True, **validate_database(paths)}
        existing = inspect_existing()
        if existing["pending_migrations"]:
            database.migrate()
            return {"applied": True, **validate_database(paths)}
        if not existing["protocol_snapshot_refresh"]:
            return existing
        # 同一协议版本的规范文字更新只替换生效快照，历史 metadata 不变。
        with database.connect(write=True) as connection:
            connection.execute("UPDATE protocols SET specification=? WHERE version=?", (packaged_specification(), PROTOCOL))
        return {"applied": True, "protocol_snapshot_refreshed": True, **validate_database(paths)}
