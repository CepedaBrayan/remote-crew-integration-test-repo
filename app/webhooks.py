"""BigPMS subscription and transactional payment handling."""

import hashlib
import hmac
import json
import logging
import time
from typing import Literal
from uuid import uuid4

import psycopg2
import requests
from fastapi import HTTPException
from psycopg2.extras import Json
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from db import get_conn, put_conn

log = logging.getLogger("leaseflow.webhooks")


class PaymentData(BaseModel):
    resident_id: str = Field(min_length=1)
    amount_cents: int = Field(strict=True, gt=0, le=2_147_483_647)


class PaymentEvent(BaseModel):
    event_id: str = Field(min_length=1)
    event_type: Literal["payment.received"]
    occurred_at: AwareDatetime
    data: PaymentData


def ensure_payment_uniqueness():
    """Protect the ledger across concurrent webhooks and future feed imports."""
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS resident_ledger_bigpms_event_id_idx
                ON resident_ledger (bigpms_event_id)
            """)
    finally:
        put_conn(conn)


def register_subscription(api_url: str, callback_url: str, secret: str):
    """Retry brief startup failures; fail startup if no subscription is registered."""
    for attempt in range(3):
        try:
            response = requests.post(
                f"{api_url.rstrip('/')}/api/v1/webhook-subscriptions",
                json={"callback_url": callback_url, "secret": secret},
                timeout=5,
            )
            response.raise_for_status()
            log.info("BigPMS webhook subscription registered")
            return
        except requests.RequestException:
            if attempt == 2:
                log.error("BigPMS webhook registration failed after 3 attempts")
                raise
            log.warning("BigPMS webhook registration failed; retrying")
            time.sleep(attempt + 1)


def record_failure(raw_body: bytes, reason: str):
    """Use a receipt ID so untrusted event IDs cannot overwrite valid payments."""
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO webhook_events (event_id, event_type, payload, status, error)
                VALUES (%s, 'webhook.rejected', %s, 'failed', %s)
            """, (
                f"rejected-{uuid4()}",
                Json({"raw_body": raw_body.decode("utf-8", errors="replace")}),
                reason,
            ))
    finally:
        put_conn(conn)


def audit_failure(raw_body: bytes, reason: str):
    # If the database is unavailable, preserve the HTTP outcome and log the failure.
    try:
        record_failure(raw_body, reason)
    except psycopg2.Error:
        log.exception("Could not record failed webhook: %s", reason)


def apply_payment(event: PaymentEvent, payload: dict) -> str:
    """Commit the ledger entry and audit result together, or roll both back."""
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT u.property_id
                FROM residents r JOIN units u ON u.id = r.unit_id
                WHERE r.id = %s
            """, (event.data.resident_id,))
            resident = cur.fetchone()
            if resident is None:
                raise ValueError("Unknown resident")

            cur.execute("""
                INSERT INTO resident_ledger
                    (property_id, resident_id, entry_type, amount_cents,
                     bigpms_event_id, occurred_at)
                VALUES (%s, %s, 'payment', %s, %s, %s)
                ON CONFLICT (bigpms_event_id) DO NOTHING
                RETURNING id
            """, (
                resident[0], event.data.resident_id, event.data.amount_cents,
                event.event_id, event.occurred_at,
            ))
            status = "processed" if cur.fetchone() else "duplicate"

            # The supplied schema has one audit row per event, not per delivery.
            cur.execute("""
                INSERT INTO webhook_events
                    (event_id, event_type, payload, status, processed_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (event_id) DO UPDATE
                SET status = EXCLUDED.status, error = NULL, processed_at = now()
            """, (event.event_id, event.event_type, Json(payload), status))
        return status
    finally:
        put_conn(conn)


def process_webhook(raw_body: bytes, signature: str, secret: str) -> dict:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8")):
        audit_failure(raw_body, "Invalid signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        event = PaymentEvent.model_validate_json(raw_body)
    except ValidationError:
        audit_failure(raw_body, "Invalid payment payload")
        raise HTTPException(status_code=422, detail="Invalid payment payload") from None

    # Keep the original JSON, including partner fields not needed by our ledger.
    payload = json.loads(raw_body)
    try:
        status = apply_payment(event, payload)
    except ValueError:
        audit_failure(raw_body, "Unknown resident")
        raise HTTPException(status_code=422, detail="Unknown resident") from None
    except psycopg2.Error:
        log.exception("Payment transaction failed: event_id=%s", event.event_id)
        audit_failure(raw_body, "Payment transaction failed")
        raise HTTPException(status_code=503, detail="Payment storage unavailable") from None

    log.info("Webhook event_id=%s status=%s", event.event_id, status)
    return {"status": status}
