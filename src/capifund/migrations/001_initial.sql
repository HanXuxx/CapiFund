CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE protocols (version TEXT PRIMARY KEY, specification TEXT NOT NULL) STRICT;
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    package_version TEXT NOT NULL
) STRICT;
CREATE TABLE records (
    id TEXT PRIMARY KEY,
    author_id TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    topic_id TEXT REFERENCES records(id),
    reply_to_id TEXT REFERENCES records(id),
    floor INTEGER,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    author_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    reply_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(topic_id, floor)
) STRICT;
CREATE INDEX records_created ON records(type, created_at);
CREATE INDEX records_activity ON records(last_activity_at);
CREATE INDEX records_author ON records(author_id);
CREATE TABLE record_bodies (hash TEXT PRIMARY KEY, body TEXT NOT NULL) STRICT;
CREATE TABLE record_revisions (
    record_id TEXT NOT NULL REFERENCES records(id),
    revision INTEGER NOT NULL,
    metadata TEXT NOT NULL,
    body_hash TEXT NOT NULL REFERENCES record_bodies(hash),
    editor_id TEXT NOT NULL,
    editor TEXT NOT NULL,
    edited_at TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('create','update')),
    PRIMARY KEY(record_id, revision)
) STRICT;
CREATE UNIQUE INDEX requests ON record_revisions(editor_id, request_id);
CREATE TRIGGER record_revisions_no_update BEFORE UPDATE ON record_revisions BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
CREATE TRIGGER record_revisions_no_delete BEFORE DELETE ON record_revisions BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
CREATE TABLE record_tags (
    record_id TEXT NOT NULL REFERENCES records(id),
    tag TEXT NOT NULL,
    PRIMARY KEY(record_id, tag)
) STRICT;
CREATE INDEX record_tags_tag ON record_tags(tag);
CREATE TABLE record_custom (
    record_id TEXT NOT NULL REFERENCES records(id),
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    PRIMARY KEY(record_id, key)
) STRICT;
CREATE INDEX record_custom_value ON record_custom(key, value_json);
CREATE TABLE record_search (
    record_id TEXT PRIMARY KEY REFERENCES records(id),
    metadata TEXT NOT NULL,
    body TEXT NOT NULL
) STRICT;
