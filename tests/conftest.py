"""Shared payment fixtures; importing tests never opens database connections."""

import hashlib
import hmac
import json
from uuid import uuid4

import pytest


@pytest.fixture
def payment():
    """Return a unique valid payment, including partner-only metadata."""
    return {
        "event_id": f"test-{uuid4()}",
        "event_type": "payment.received",
        "occurred_at": "2026-07-07T14:30:00Z",
        "data": {"resident_id": "res-101", "amount_cents": 150000, "method": "ach", "seq": 21},
        "partner_metadata": {"source": "test"},
    }


@pytest.fixture
def body(payment):
    """Serialize the fixture once so signing and transmission use identical bytes."""
    return json.dumps(payment).encode()


@pytest.fixture
def sign():
    """Provide a signer accepting raw bytes and an optional shared secret."""

    def signature(body, secret="leaseflow-dev-secret"):
        """Return the hexadecimal HMAC used by the partner protocol."""
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    return signature
