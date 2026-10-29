"""Fixtures for the isolated Compose environment; no application imports or mocks."""

import os

import psycopg2
import pytest
import requests


@pytest.fixture(scope="session")
def urls():
    """Require explicit E2E endpoints rather than guessing development addresses."""
    return os.environ["E2E_APP_URL"], os.environ["E2E_PARTNER_URL"]


@pytest.fixture
def database():
    """Connect only to the explicitly configured disposable E2E database."""
    conn = psycopg2.connect(os.environ["E2E_DATABASE_URL"])
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def query(database):
    """Execute SQL and fetch rows while keeping each assertion in a fresh snapshot."""

    def execute(statement, params=()):
        """Return query rows, or an empty list for a statement without a result."""
        with database.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else []

    return execute


@pytest.fixture
def deliver(urls, sign):
    """Send exact signed bytes through the actual HTTP server."""

    def post(body, signature=None):
        """Use the standard signature unless the test intentionally overrides it."""
        return requests.post(
            f"{urls[0]}/webhooks/bigpms",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-BigPMS-Signature": sign(body) if signature is None else signature,
            },
            timeout=10,
        )

    return post
