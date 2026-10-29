"""Read-only, set-based ledger queries for one consistent local report snapshot."""

from db import dict_cursor, get_conn, put_conn


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
