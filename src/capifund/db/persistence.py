"""存储访问层：SQLite 正文、修订与查询投影的唯一读写实现。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from .database import Database
from .storage import StoragePaths
from .content_projection import store_body, project_record, refresh_activity
from ..domain.ports import RecordHead
from ..errors import RecordError, StorageFailure
from ..serialization import dumps

CURRENT = "JOIN record_revisions v ON v.record_id=r.id AND v.revision=r.current_revision"


def _head(row) -> RecordHead | None:
    if row is None:
        return None
    return RecordHead(row["id"], row["author_id"], row["current_revision"], row["type"],
                      row["topic_id"], row["reply_to_id"], row["floor"])


class SQLiteSession:
    def __init__(self, connection):
        self._connection = connection

    def protocol(self):
        protocol = self._connection.execute("SELECT value FROM settings WHERE key='protocol'").fetchone()[0]
        specification = self._connection.execute("SELECT specification FROM protocols WHERE version=?", (protocol,)).fetchone()[0]
        return {"protocol": protocol, "database_version": self._connection.execute("PRAGMA user_version").fetchone()[0],
                "specification": specification}

    def head(self, record_id):
        return _head(self._connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())

    def topic_replies(self, topic_id, page, page_size):
        total = self._connection.execute("SELECT COUNT(*) FROM records WHERE topic_id=?", (topic_id,)).fetchone()[0]
        offset = (page - 1) * page_size
        if offset >= total:
            return {"items": [], "total": total}
        rows = self._connection.execute("SELECT * FROM records WHERE topic_id=? ORDER BY floor LIMIT ? OFFSET ?",
                                        (topic_id, page_size, offset))
        return {"items": [_head(row) for row in rows], "total": total}

    def find_records(self, filters, sort, page, page_size):
        source = " FROM records r " + CURRENT
        conditions, parameters = ["1"], []
        if filters["type"]:
            conditions.append("r.type=?")
            parameters.append(filters["type"])
        if filters["author"]:
            conditions.append("(r.author_id=? OR r.author_name=?)")
            parameters.extend([filters["author"], filters["author"]])
        if filters["tag"]:
            conditions.append("r.id IN (SELECT record_id FROM record_tags WHERE tag=? OR (tag>=? AND tag<?))")
            parameters.extend([filters["tag"], filters["tag"] + "/", filters["tag"] + "0"])
        if filters["topic_id"]:
            conditions.append("r.topic_id=?")
            parameters.append(filters["topic_id"])
        for key, value in filters["custom"].items():
            conditions.append("r.id IN (SELECT record_id FROM record_custom WHERE key=? AND value_json=?)")
            parameters.extend([key, value])
        if filters["keyword"]:
            source += " JOIN record_search s ON s.record_id=r.id"
            conditions.append("(instr(s.metadata,?)>0 OR instr(s.body,?)>0)")
            parameters.extend([filters["keyword"], filters["keyword"]])
        where = " WHERE " + " AND ".join(conditions)
        total = self._connection.execute("SELECT COUNT(*)" + source + where, parameters).fetchone()[0]
        offset = (page - 1) * page_size
        if offset >= total:
            return {"items": [], "total": total}
        orders = {"latest": "r.created_at DESC,r.rowid DESC", "oldest": "r.created_at ASC,r.rowid ASC",
                  "activity": "r.last_activity_at DESC,r.rowid DESC", "replies": "r.reply_count DESC,r.created_at DESC,r.rowid DESC"}
        default = "r.floor ASC" if filters["topic_id"] else "r.rowid DESC"
        rows = self._connection.execute("SELECT r.id,v.metadata,r.current_revision AS revision,r.reply_count,r.last_activity_at"
                                        + source + where + " ORDER BY " + (default if sort is None else orders[sort])
                                        + " LIMIT ? OFFSET ?", [*parameters, page_size, offset])
        return {"items": [dict(row, metadata=json.loads(row["metadata"])) for row in rows], "total": total}

    def tag_counts(self):
        rows = self._connection.execute("""WITH RECURSIVE paths(record_id,path,rest) AS (
            SELECT record_id,'',tag||'/' FROM record_tags
            UNION ALL SELECT record_id,path||CASE WHEN path='' THEN '' ELSE '/' END||substr(rest,1,instr(rest,'/')-1),
                substr(rest,instr(rest,'/')+1) FROM paths WHERE rest!=''
            ) SELECT path,COUNT(DISTINCT record_id) AS count FROM paths WHERE path!='' GROUP BY path ORDER BY path""")
        return {row["path"]: row["count"] for row in rows}

    def version(self, record_id, revision):
        row = self._connection.execute("SELECT metadata FROM record_revisions WHERE record_id=? AND revision=?",
                                       (record_id, revision)).fetchone()
        return {"metadata": json.loads(row[0]), "revision": revision} if row else None

    def body(self, record_id, revision):
        return self._connection.execute("""SELECT b.body FROM record_revisions r JOIN record_bodies b ON b.hash=r.body_hash
            WHERE r.record_id=? AND r.revision=?""", (record_id, revision)).fetchone()[0]

    def history(self, record_id, page, page_size):
        rows = self._connection.execute("""SELECT revision,metadata,editor,edited_at,operation FROM record_revisions
            WHERE record_id=? ORDER BY revision DESC LIMIT ? OFFSET ?""", (record_id, page_size, (page - 1) * page_size))
        return [dict(row, metadata=json.loads(row["metadata"]), editor=json.loads(row["editor"])) for row in rows]

    def request(self, editor_id, request_id):
        row = self._connection.execute("SELECT record_id,revision,request_hash FROM record_revisions WHERE editor_id=? AND request_id=?",
                                       (editor_id, request_id)).fetchone()
        return dict(row) if row else None

    def next_floor(self, topic_id):
        return self._connection.execute("SELECT COALESCE(MAX(floor),0)+1 FROM records WHERE topic_id=?", (topic_id,)).fetchone()[0]

    def insert(self, head, metadata):
        self._connection.execute("""INSERT INTO records(id,author_id,current_revision,topic_id,reply_to_id,floor,
            type,title,status,author_name,created_at,updated_at,last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (head.id, head.author_id, head.revision, head.topic_id, head.reply_to_id, head.floor,
             metadata["type"], metadata["title"], metadata["status"], metadata["author"],
             metadata["created_at"], metadata["updated_at"], metadata["created_at"]))

    def append(self, record_id, revision, metadata, body, editor, request_id, fingerprint, operation):
        body = body.replace("\r\n", "\n").replace("\r", "\n")
        digest = store_body(self._connection, body)
        self._connection.execute("""INSERT INTO record_revisions(record_id,revision,metadata,body_hash,editor_id,editor,edited_at,
            request_id,request_hash,operation) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (record_id, revision, dumps(metadata), digest, editor["id"], dumps(editor), metadata["updated_at"],
             request_id, fingerprint, operation))
        self._connection.execute("UPDATE records SET current_revision=? WHERE id=?", (revision, record_id))
        project_record(self._connection, record_id, metadata, body)
        refresh_activity(self._connection, record_id)

    def due_checkpoints(self, cutoff, limit):
        source = """ FROM records r """ + CURRENT + """
            JOIN records t ON t.id=r.topic_id JOIN record_revisions tv ON tv.record_id=t.id AND tv.revision=t.current_revision
            WHERE r.type='comment' AND json_extract(v.metadata,'$.custom.kind')='checkpoint'
              AND json_extract(v.metadata,'$.custom.status')='pending'
              AND json_extract(v.metadata,'$.custom.due_date')<=?
              AND json_extract(tv.metadata,'$.custom.thesis_status')<>'closed'"""
        total = self._connection.execute("SELECT COUNT(*)" + source, (cutoff,)).fetchone()[0]
        rows = self._connection.execute("SELECT v.metadata,r.current_revision AS revision,tv.metadata AS thesis" + source
                                        + " ORDER BY json_extract(v.metadata,'$.custom.due_date'),r.rowid LIMIT ?", (cutoff, limit))
        return {"items": [{"metadata": json.loads(row["metadata"]), "revision": row["revision"],
                           "thesis": json.loads(row["thesis"])} for row in rows], "total": total}

    def theses_with_new_evidence(self, limit):
        source = """ FROM records t JOIN record_revisions tv ON tv.record_id=t.id AND tv.revision=t.current_revision
            JOIN records r ON r.topic_id=t.id """ + CURRENT + """
            WHERE t.type='thesis' AND json_extract(tv.metadata,'$.custom.thesis_status')='active'
              AND r.updated_at>t.updated_at
              AND (json_extract(v.metadata,'$.custom.kind')='evidence'
                   OR (json_extract(v.metadata,'$.custom.kind')='checkpoint'
                       AND json_extract(v.metadata,'$.custom.status') IN ('confirmed','refuted','expired')))"""
        total = self._connection.execute("SELECT COUNT(DISTINCT t.id)" + source).fetchone()[0]
        rows = self._connection.execute("SELECT tv.metadata,t.current_revision AS revision,COUNT(*) AS new_evidence,"
                                        "MAX(r.updated_at) AS latest_evidence_at" + source
                                        + " GROUP BY t.id ORDER BY latest_evidence_at DESC LIMIT ?", (limit,))
        return {"items": [{"metadata": json.loads(row["metadata"]), "revision": row["revision"], "new_evidence": row["new_evidence"],
                           "latest_evidence_at": row["latest_evidence_at"]} for row in rows], "total": total}

    def theses_without_pending_checkpoints(self, limit):
        source = """ FROM records t JOIN record_revisions tv ON tv.record_id=t.id AND tv.revision=t.current_revision
            WHERE t.type='thesis' AND json_extract(tv.metadata,'$.custom.thesis_status')='active'
              AND NOT EXISTS (
                  SELECT 1 FROM records c JOIN record_revisions cv ON cv.record_id=c.id AND cv.revision=c.current_revision
                  WHERE c.topic_id=t.id AND c.type='comment'
                    AND json_extract(cv.metadata,'$.custom.kind')='checkpoint'
                    AND json_extract(cv.metadata,'$.custom.status')='pending'
              )"""
        total = self._connection.execute("SELECT COUNT(*)" + source).fetchone()[0]
        rows = self._connection.execute("SELECT tv.metadata,t.current_revision AS revision" + source
                                        + " ORDER BY t.updated_at DESC,t.rowid DESC LIMIT ?", (limit,))
        return {"items": [{"metadata": json.loads(row["metadata"]), "revision": row["revision"]} for row in rows], "total": total}


class SQLiteFundStorage:
    def __init__(self, paths: StoragePaths, database: Database | None = None):
        self.paths, self.database = paths, database or Database(paths)

    @contextmanager
    def transaction(self, *, write=False):
        try:
            with self.database.connect(write=write) as connection:
                yield SQLiteSession(connection)
        except RecordError:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise StorageFailure("CapiFund storage operation failed") from exc
