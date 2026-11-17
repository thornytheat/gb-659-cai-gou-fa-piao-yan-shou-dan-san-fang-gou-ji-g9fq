import os

import pytest

os.environ["TZ"] = "Asia/Shanghai"


@pytest.fixture(scope="session", autouse=True)
def isolated_database(tmp_path_factory):
    """Keep tests off the runtime database file."""
    import app.store as store

    original = store.DATABASE_PATH
    store.DATABASE_PATH = tmp_path_factory.mktemp("db") / "runs.sqlite3"
    try:
        yield
    finally:
        store.DATABASE_PATH = original
