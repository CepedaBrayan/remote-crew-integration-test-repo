"""Isolate unit tests from PostgreSQL and the partner's HTTP server."""

import importlib
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope="session")
def modules():
    """Import application modules with the eager database pool replaced by a mock."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
    with patch("psycopg2.pool.ThreadedConnectionPool"):
        return SimpleNamespace(
            **{
                name: importlib.import_module(name)
                for name in ("auth", "dto", "orm", "utils", "webhooks", "sync_store", "sync", "main")
            }
        )


@pytest.fixture
def database(modules, monkeypatch):
    """Provide a mocked transaction/cursor and track connection release."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    acquire = MagicMock(return_value=conn)
    release = MagicMock()
    monkeypatch.setattr(modules.orm, "get_conn", acquire)
    monkeypatch.setattr(modules.orm, "put_conn", release)
    monkeypatch.setattr(
        modules.orm, "dict_cursor", MagicMock(return_value=conn.cursor.return_value)
    )
    return SimpleNamespace(conn=conn, cur=cur, acquire=acquire, release=release)
