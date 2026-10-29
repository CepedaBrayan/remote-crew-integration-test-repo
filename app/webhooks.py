"""BigPMS subscription and transactional payment handling."""

import logging
import time

import psycopg2
import requests
from dto import PaymentEvent
from fastapi import HTTPException
from orm import apply_payment
from pydantic import ValidationError
from utils import audit_failure

log = logging.getLogger("leaseflow.webhooks")


def register_subscription(api_url: str, callback_url: str, secret: str):
    """Register the callback URL and signing secret with BigPMS at startup.

    Args:
        api_url: Partner base URL, with or without a trailing slash.
        callback_url: URL reachable by BigPMS for payment delivery.
        secret: Shared HMAC secret; never included in application log messages.

    Each request has a five-second Requests timeout. Up to three attempts are
    made, separated by one- and two-second waits. Registration is idempotent
    for a callback URL, but does not verify future callback reachability.

    Raises:
        requests.RequestException: The last failure after retries are exhausted,
            allowing FastAPI startup to fail rather than silently lose webhooks.
    """
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


def process_webhook(raw_body: bytes) -> dict:
    """Validate an authenticated body and delegate its atomic persistence.

    Args:
        raw_body: Original JSON bytes; the caller must verify the signature first.

    Returns:
        A status dictionary containing "processed" or "duplicate".

    Raises:
        HTTPException: HTTP 422 for invalid data or an unknown resident; HTTP 503
            for storage failures. Rejections receive a best-effort audit receipt.
    """
    try:
        event = PaymentEvent.model_validate_json(raw_body)
    except ValidationError:
        audit_failure(raw_body, "Invalid payment payload")
        raise HTTPException(
            status_code=422,
            detail="Invalid payment payload",
        ) from None

    try:
        status = apply_payment(event)
    except ValueError:
        audit_failure(raw_body, "Unknown resident")
        raise HTTPException(
            status_code=422,
            detail="Unknown resident",
        ) from None
    except psycopg2.Error:
        log.exception(
            "Payment transaction failed: event_id=%s",
            event.event_id,
        )
        audit_failure(raw_body, "Payment transaction failed")
        raise HTTPException(
            status_code=503,
            detail="Payment storage unavailable",
        ) from None

    log.info("Webhook event_id=%s status=%s", event.event_id, status)
    return {"status": status}
