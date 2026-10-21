"""正文去重与当前修订的查询投影，共用调用方事务。"""
import hashlib
from ..serialization import dumps
from ..domain.tags import display_tags


def store_body(connection, body):
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    connection.execute("INSERT INTO record_bodies(hash,body) VALUES (?,?) ON CONFLICT(hash) DO NOTHING", (digest, body))
    return digest


def project_record(connection, record_id, metadata, body):
    connection.execute("UPDATE records SET type=?,title=?,status=?,author_name=?,created_at=?,updated_at=? WHERE id=?",
                       (metadata["type"], metadata["title"], metadata["status"], metadata["author"],
                        metadata["created_at"], metadata["updated_at"], record_id))
    connection.execute("DELETE FROM record_tags WHERE record_id=?", (record_id,))
    connection.executemany("INSERT INTO record_tags(record_id,tag) VALUES (?,?)", [(record_id, tag) for tag in display_tags(metadata["tags"])])
    connection.execute("DELETE FROM record_custom WHERE record_id=?", (record_id,))
    connection.executemany("INSERT INTO record_custom(record_id,key,value_json) VALUES (?,?,?)",
                           [(record_id, key, dumps(value)) for key, value in metadata["custom"].items()])
    connection.execute("""INSERT INTO record_search(record_id,metadata,body) VALUES (?,?,?)
        ON CONFLICT(record_id) DO UPDATE SET metadata=excluded.metadata,body=excluded.body""",
                       (record_id, dumps(metadata).casefold(), body.casefold()))


def refresh_activity(connection, record_id):
    topic_id = connection.execute("SELECT topic_id FROM records WHERE id=?", (record_id,)).fetchone()[0] or record_id
    connection.execute("""UPDATE records SET
        last_activity_at=MAX(created_at,COALESCE((SELECT MAX(created_at) FROM records WHERE topic_id=?),created_at)),
        reply_count=(SELECT COUNT(*) FROM records WHERE topic_id=?) WHERE id=?""", (topic_id, topic_id, topic_id))
