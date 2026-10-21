from __future__ import annotations

import itertools

import pytest

from capifund.db.maintenance import migrate_database
from capifund.db.persistence import SQLiteFundStorage
from capifund.db.storage import StoragePaths
from capifund.domain.business import BusinessAPI
from capifund.tokens import write_tokens

AGENT = {"id": "agent-a", "type": "agent", "runtime": "codex", "name": "Agent A"}
OTHER = {"id": "agent-b", "type": "agent", "runtime": "grokbot"}
TOKEN = "test-token"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def paths(tmp_path):
    paths = StoragePaths(tmp_path / "data")
    migrate_database(paths, apply=True)
    return paths


@pytest.fixture
def tokens_file(tmp_path):
    path = tmp_path / "tokens" / "tokens.json"
    write_tokens(path, {"test": TOKEN})
    return path


@pytest.fixture
def api(paths):
    return BusinessAPI(SQLiteFundStorage(paths))


@pytest.fixture
def write(api):
    """以递增 request_id 调用写操作，测试只关注业务参数。"""
    counter = itertools.count(1)

    def call(operation, author=AGENT, **arguments):
        return api.call(operation, {"request_id": f"req-{next(counter)}", "author": author, **arguments})
    return call
