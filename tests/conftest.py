"""Run against PostgreSQL in a fresh, isolated schema; never change existing tables."""
import importlib
import os
from pathlib import Path
import sys
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))


@pytest.fixture(scope="session")
def database():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.fail("Set TEST_DATABASE_URL to a PostgreSQL database where tests may create a schema")
    schema = f"test_webhooks_{uuid4().hex}"
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    test_dsn = make_dsn(dsn, options=f"-c search_path={schema}")
    os.environ["DATABASE_URL"] = test_dsn
    conn = psycopg2.connect(test_dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute((ROOT / "db/init.sql").read_text())
        yield conn
    finally:
        if "db" in sys.modules:
            sys.modules["db"]._pool.closeall()
        conn.close()
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture(autouse=True)
def seeded_database(database):
    with database, database.cursor() as cur:
        cur.execute("""
            TRUNCATE webhook_events, resident_ledger, leases, residents, units, properties
            RESTART IDENTITY CASCADE
        """)
        cur.execute((ROOT / "db/seed.sql").read_text())
    importlib.import_module("webhooks").ensure_payment_uniqueness()
    yield database


@pytest.fixture
def client(seeded_database):
    main = importlib.import_module("main")
    # No lifespan context: registration is tested independently, with HTTP mocked.
    return TestClient(main.app)
