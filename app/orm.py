"""Central SQL persistence boundary for LeaseFlow.

Own all application SQL here: setup, webhook writes, feed ownership/progress and
reconciliation reads. Service modules validate payloads and coordinate HTTP calls.
Functions either own a documented transaction or accept the caller's connection
or cursor; preserve those boundaries when adding queries.
"""

import logging
from contextlib import contextmanager
from uuid import UUID

from dto import FeedPage, PaymentEvent
from psycopg2.extras import Json

from db import dict_cursor, get_conn, put_conn

SOURCE = "bigpms_ledger_events"
# Stable, application-specific session lock; never use Python's randomized hash().
SYNC_LOCK_ID = 742_019_002
sync_log = logging.getLogger("leaseflow.sync")


def ensure_payment_events_uniqueness():
    """Create the unique partner-event index before accepting payments.

    Repeated startup calls leave an existing index in place. Non-null event IDs
    are unique across all ingestion channels; local charges may retain NULL IDs.
    Database errors propagate to prevent startup without duplicate protection.
    """
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS resident_ledger_bigpms_event_id_idx
                ON resident_ledger (bigpms_event_id)
            """)
    finally:
        put_conn(conn)


def record_failure(uuid: UUID, raw_body: bytes, reason: str):
    """Persist a rejected delivery in its own transaction.

    Args:
        uuid: Locally generated receipt ID, independent of any untrusted event ID.
        raw_body: Received bytes, decoded with replacement for invalid UTF-8.
        reason: Human-readable rejection or processing failure description.

    Raises:
        psycopg2.Error: If the audit write fails; the connection is still returned.
    """
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO webhook_events (event_id, event_type, payload, status, error)
                VALUES (%s, 'webhook.rejected', %s, 'failed', %s)
            """,
                (
                    f"rejected-{uuid}",
                    Json({"raw_body": raw_body.decode("utf-8", errors="replace")}),
                    reason,
                ),
            )
    finally:
        put_conn(conn)


def get_health_check_status():
    """Return service health after a successful database round trip.

    Database errors propagate so an unavailable database cannot report healthy.
    """
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return {"status": "ok", "service": "leaseflow-integration"}
    finally:
        put_conn(conn)


def get_residents():
    """Return residents and their unit numbers, ordered by resident ID.

    The dictionary cursor produces JSON-compatible rows. The connection is
    returned to the pool on both successful reads and database errors.
    """
    conn = get_conn()
    try:
        with conn, dict_cursor(conn) as cur:
            cur.execute("""
                SELECT r.id, r.name, u.unit_number
                FROM residents r JOIN units u ON u.id = r.unit_id
                ORDER BY r.id
            """)
            return {"residents": cur.fetchall()}
    finally:
        put_conn(conn)


def insert_payment(cur, event: PaymentEvent) -> str:
    """Insert one validated payment using the caller's transaction.

    Resolve property ownership from the resident. Return processed or duplicate;
    raise ValueError for an unknown resident. Never commit or write webhook audit
    rows: callers decide which other writes must share the transaction. The unique
    event index protects both ingestion channels under concurrent delivery.
    """
    cur.execute(
        """
        SELECT u.property_id
        FROM residents r
        JOIN units u ON u.id = r.unit_id
        WHERE r.id = %s
        """,
        (event.data.resident_id,),
    )
    resident = cur.fetchone()

    if resident is None:
        raise ValueError("Unknown resident")

    cur.execute(
        """
        INSERT INTO resident_ledger (
            property_id,
            resident_id,
            entry_type,
            amount_cents,
            bigpms_event_id,
            occurred_at
        )
        VALUES (%s, %s, 'payment', %s, %s, %s)
        ON CONFLICT (bigpms_event_id) DO NOTHING
        RETURNING id
        """,
        (
            resident[0],
            event.data.resident_id,
            event.data.amount_cents,
            event.event_id,
            event.occurred_at,
        ),
    )
    return "processed" if cur.fetchone() else "duplicate"


def apply_payment(event: PaymentEvent) -> str:
    """Persist an already validated payment and its audit result atomically.

    Args:
        event: PaymentEvent validated by the webhook layer after authentication.

    Returns:
        "processed" when a ledger row was inserted, otherwise "duplicate".

    Raises:
        ValueError: If the resident has no matching unit/property.
        psycopg2.Error: On storage failure; both writes are rolled back.

    The unique index arbitrates concurrent deliveries. Redelivery updates the
    existing audit status but preserves its first stored payload. Model dumping
    preserves extra fields while normalizing typed values such as timestamps.
    """

    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            status = insert_payment(cur, event)

            # Keep one audit row per event and preserve its original payload.
            cur.execute(
                """
                INSERT INTO webhook_events (
                    event_id,
                    event_type,
                    payload,
                    status,
                    processed_at
                )
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (event_id) DO UPDATE
                SET status = EXCLUDED.status,
                    error = NULL,
                    processed_at = now()
                """,
                (
                    event.event_id,
                    event.event_type,
                    Json(event.model_dump(mode="json")),
                    status,
                ),
            )

        return status
    finally:
        put_conn(conn)


# Feed ownership and transactional progress.


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
                sync_log.exception("Could not release sync lock; discarding connection")
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


# Read-only reconciliation snapshot.


def read_reconciliation_snapshot() -> tuple[list[dict], dict]:
    """Aggregate payments and detect data-quality issues in one read-only snapshot.

    Include residents without payments. Detect duplicate charge groups on UTC
    calendar dates and every ledger row with no resident. REPEATABLE READ keeps
    all three queries consistent despite concurrent ingestion. Transaction-local
    settings do not leak to the pool. Database failures propagate after rollback
    and the connection is always returned. No ledger records are modified.
    """
    conn = get_conn()
    try:
        with conn, dict_cursor(conn) as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute("""
                SELECT r.id AS resident_id, r.name,
                       COALESCE(p.total, 0) AS local_payments_cents
                FROM residents r
                LEFT JOIN (
                    SELECT resident_id, SUM(amount_cents) AS total
                    FROM resident_ledger WHERE entry_type = 'payment'
                    GROUP BY resident_id
                ) p ON p.resident_id = r.id
                ORDER BY r.id
            """)
            residents = cur.fetchall()
            cur.execute("""
                SELECT resident_id, charge_code, amount_cents,
                       (occurred_at AT TIME ZONE 'UTC')::date AS occurred_on,
                       COUNT(*) AS count
                FROM resident_ledger
                WHERE entry_type = 'charge'
                GROUP BY resident_id, charge_code, amount_cents,
                         (occurred_at AT TIME ZONE 'UTC')::date
                HAVING COUNT(*) > 1
                ORDER BY resident_id, charge_code NULLS FIRST, amount_cents, occurred_on
            """)
            duplicates = cur.fetchall()
            cur.execute("""
                SELECT l.id AS ledger_id, l.resident_id, l.entry_type, l.amount_cents
                FROM resident_ledger l
                WHERE NOT EXISTS (SELECT 1 FROM residents r WHERE r.id = l.resident_id)
                ORDER BY l.id
            """)
            orphans = cur.fetchall()
        return residents, {"duplicate_charges": duplicates, "orphaned_ledger_entries": orphans}
    finally:
        put_conn(conn)
