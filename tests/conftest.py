"""Database tests need TEST_DATABASE_URL pointing at a throwaway database
(its tables are truncated). Without it they are skipped.

    docker run -d --name insightflow-pg -e POSTGRES_PASSWORD=devpass -p 5433:5432 postgres:17
    docker exec insightflow-pg createdb -U postgres insightflow_test
    TEST_DATABASE_URL=postgresql://postgres:devpass@localhost:5433/insightflow_test pytest
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = os.environ.get("TEST_DATABASE_URL")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB  # the API reads this lazily on first query


@pytest.fixture(scope="session")
def db_conn():
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
        yield conn
