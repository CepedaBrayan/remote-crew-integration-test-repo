"""PostgreSQL boundaries for feed ownership, durable pages and run auditing."""

import logging
from contextlib import contextmanager

from dto import FeedPage
from orm import insert_payment

from db import get_conn, put_conn

SOURCE = "bigpms_ledger_events"
# Stable, application-specific session lock; never use Python's randomized hash().
SYNC_LOCK_ID = 742_019_002
log = logging.getLogger("leaseflow.sync")


@contextmanager
def sync_session():
    """Own the feed across processes without holding transactions during HTTP waits.

    Yield a pooled connection while holding a session-level advisory lock, or None
    after auditing an overlapping attempt as failed. Commit each short SQL unit.
    Always release the lock before returning the connection; discard the session
    if unlocking fails so a pooled connection cannot retain a stale lock.
    """
    conn = get_conn()
    locked = False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_ID,))
            locked = cur.fetchone()[0]
            if not locked:
                cur.execute(
                    """INSERT INTO sync_runs
                        (source, cursor_start, cursor_end, events_fetched, events_applied,
                         events_skipped, status, error, finished_at)
                       SELECT source, cursor_seq, cursor_seq, 0, 0, 0, 'failed',
                              'Skipped: another sync owns the advisory lock', now()
                       FROM sync_state WHERE source = %s""",
                    (SOURCE,),
                )
        yield conn if locked else None
    finally:
        if locked:
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_ID,))
            except Exception:
                log.exception("Could not release sync lock; discarding connection")
                conn.close()
        put_conn(conn)


def begin_sync_run(conn) -> tuple[int, int]:
    """Recover abandoned runs and record the durable starting cursor under ownership.

    A previous running row is necessarily abandoned once this session owns the
    exclusive advisory lock. Preserve its committed counters and cursor range.
    Return the new run ID and cursor; fail if the seeded state row is missing.
    """
    with conn, conn.cursor() as cur:
        cur.execute("SELECT cursor_seq FROM sync_state WHERE source = %s", (SOURCE,))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Missing seeded sync_state row")
        cursor = row[0]
        cur.execute(
            """UPDATE sync_runs SET status = 'failed', finished_at = now(),
                      error = 'Interrupted before completion; recovered by next lock owner'
               WHERE source = %s AND status = 'running'""",
            (SOURCE,),
        )
        cur.execute(
            """INSERT INTO sync_runs
                  (source, cursor_start, cursor_end, events_fetched, events_applied, events_skipped)
               VALUES (%s, %s, %s, 0, 0, 0) RETURNING id""",
            (SOURCE, cursor, cursor),
        )
        run_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE sync_state SET last_run_at = now(), last_status = 'running' WHERE source = %s",
            (SOURCE,),
        )
    return run_id, cursor


def apply_feed_page(conn, run_id: int, cursor: int, page: FeedPage) -> tuple[int, int]:
    """Commit a validated page's payments, cursor and run counters as one unit.

    Duplicates count as skipped, including repetitions within the page. Any lookup,
    INSERT, cursor or audit failure rolls back the entire page. Return applied and
    skipped counts only after commit. The caller owns the advisory lock and has
    validated paging metadata before entering this transaction.
    """
    with conn, conn.cursor() as cur:
        cur.execute("SELECT cursor_seq FROM sync_state WHERE source = %s FOR UPDATE", (SOURCE,))
        if cur.fetchone() != (cursor,):
            raise RuntimeError("Durable cursor changed outside the sync owner")
        applied = sum(
            insert_payment(cur, event.as_payment()) == "processed" for event in page.events
        )
        skipped = len(page.events) - applied
        cur.execute(
            "UPDATE sync_state SET cursor_seq = %s WHERE source = %s",
            (page.next_cursor, SOURCE),
        )
        cur.execute(
            """UPDATE sync_runs SET cursor_end = %s,
                      events_fetched = events_fetched + %s,
                      events_applied = events_applied + %s,
                      events_skipped = events_skipped + %s
               WHERE id = %s""",
            (page.next_cursor, len(page.events), applied, skipped, run_id),
        )
    return applied, skipped


def finish_sync_run(run_id: int, status: str, error: str | None = None) -> None:
    """Finish a run without changing its committed cursor or counters.

    Use a fresh pooled connection so a failed worker connection can still be
    audited. If PostgreSQL is unavailable, the caller logs the failure and the
    next owner recovers the remaining running row.
    """
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sync_runs SET status = %s, error = %s, finished_at = now() WHERE id = %s AND status = 'running'",
                (status, error, run_id),
            )
            if cur.rowcount:
                cur.execute(
                    "UPDATE sync_state SET last_run_at = now(), last_status = %s WHERE source = %s",
                    (status, SOURCE),
                )
    finally:
        put_conn(conn)
