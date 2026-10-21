"""离线迁移、启动版本检查、校验与备份。"""
from __future__ import annotations

import sqlite3

import pytest

from capifund.admin import main as admin_main
from capifund.db.database import Database
from capifund.db.maintenance import backup_database, migrate_database, validate_database
from capifund.db.storage import ServiceLease, StoragePaths


def test_migration_preview_apply_and_repeat(tmp_path):
    paths = StoragePaths(tmp_path / "data")
    preview = migrate_database(paths)
    assert preview == {"applied": False, "target_database_version": 1, "pending_migrations": ["001_initial"]}
    assert not paths.data_dir.exists()
    with pytest.raises(ValueError, match="Database missing"):
        Database(paths).check()
    applied = migrate_database(paths, apply=True)
    assert applied["applied"] and applied["valid"] and applied["journal_mode"] == "wal"
    again = migrate_database(paths, apply=True)
    assert again["already_migrated"] and not again["protocol_snapshot_refresh"]


def test_validation_reports_projection_tampering(paths, write):
    record = write("create", metadata={"type": "thesis", "title": "x", "tags": ["行业"]}, body="正文")
    assert validate_database(paths)["valid"]
    with sqlite3.connect(Database(paths).path) as connection:
        connection.execute("UPDATE records SET title='篡改' WHERE id=?", (record["metadata"]["id"],))
        connection.execute("DELETE FROM record_tags")
    errors = validate_database(paths)["errors"]
    assert any("Record projection mismatch" in error for error in errors)
    assert any("Tag projection mismatch" in error for error in errors)


def test_maintenance_requires_stopped_service(paths):
    with ServiceLease(paths):
        with pytest.raises(ValueError, match="stop the service"):
            migrate_database(paths, apply=True)


def test_backup_outside_data_dir(paths, write, tmp_path):
    write("create", metadata={"type": "idea", "title": "x"}, body="x")
    result = backup_database(paths, tmp_path / "backup")
    restored = StoragePaths(tmp_path / "backup")
    assert result["backup"] == str(restored.data_dir)
    assert validate_database(restored)["records"] == 1
    with pytest.raises(ValueError):
        backup_database(paths, paths.data_dir / "inside")


def test_admin_cli_validate_and_check_config(paths, capsys):
    assert admin_main(["--data-dir", str(paths.data_dir), "validate-database"]) == 0
    assert '"valid": true' in capsys.readouterr().out
    assert admin_main(["--data-dir", str(paths.data_dir), "--public-origin", "http://x/path", "--check-config"]) == 2
