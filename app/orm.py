from uuid import UUID

from dto import PaymentEvent
from psycopg2.extras import Json

from db import dict_cursor, get_conn, put_conn


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
            status = "processed" if cur.fetchone() else "duplicate"

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
