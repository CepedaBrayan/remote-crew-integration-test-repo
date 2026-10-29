"""Database connection pool. Ready to use — no changes needed here.

Application SQL belongs in orm.py. Borrow connections here only from that
persistence boundary, and always return them with put_conn after commit/rollback.
"""

import os

from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://leaseflow:leaseflow@localhost:5432/leaseflow"
)

_pool = ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)


def get_conn():
    """Borrow a pooled connection for an orm.py persistence operation.

    The caller must end its transaction and return the connection with put_conn.
    Pool exhaustion or database errors propagate to the persistence boundary.
    """
    return _pool.getconn()


def put_conn(conn):
    """Return a borrowed connection after its transaction and session locks end."""
    _pool.putconn(conn)


def dict_cursor(conn):
    """Create a dictionary-row cursor without executing SQL or owning a transaction."""
    return conn.cursor(cursor_factory=RealDictCursor)
