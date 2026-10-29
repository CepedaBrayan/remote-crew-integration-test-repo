"""Exercise real partner registration, HTTP delivery and PostgreSQL transactions."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
import time

import pytest
import requests


def test_startup_registration_and_real_partner_delivery(urls, query):
    """The untouched mock must deliver a new payment through the registered callback."""
    health = requests.get(f"{urls[0]}/api/health", timeout=5)
    assert health.status_code == 200
    residents = requests.get(f"{urls[0]}/api/v1/residents", timeout=5)
    assert len(residents.json()["residents"]) == 8
    response = requests.get(f"{urls[1]}/api/v1/webhook-subscriptions", timeout=5)
    response.raise_for_status()
    assert any(
        s["callback_url"] == "http://app:8000/webhooks/bigpms"
        for s in response.json()["subscriptions"]
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        rows = query("""
            SELECT l.bigpms_event_id FROM resident_ledger l
            JOIN webhook_events w ON w.event_id = l.bigpms_event_id
            WHERE l.bigpms_event_id LIKE 'evt-2%%'
              AND w.status IN ('processed', 'duplicate')
        """)
        if rows:
            return
        time.sleep(0.2)
    pytest.fail("No partner-generated payment reached the ledger within 20 seconds")


def test_valid_payment_and_redelivery(deliver, query, body, payment):
    """A valid HTTP request stores one payment; redelivery updates only audit status."""
    response = deliver(body)
    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
    response = deliver(body)
    assert response.status_code == 200
    assert response.json() == {"status": "duplicate"}
    assert query(
        """
        SELECT property_id, amount_cents FROM resident_ledger WHERE bigpms_event_id=%s
    """,
        (payment["event_id"],),
    ) == [("prop-001", 150000)]
    status, payload = query(
        "SELECT status, payload FROM webhook_events WHERE event_id=%s", (payment["event_id"],)
    )[0]
    assert status == "duplicate"
    assert payload == payment


def test_concurrent_http_deliveries(deliver, query, body, payment):
    """Eight simultaneous requests must commit exactly one ledger payment."""
    barrier = Barrier(8)

    def send(_):
        """Release all clients together to exercise the database's unique index."""
        barrier.wait(timeout=10)
        return deliver(body)

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(send, range(8)))
    assert all(response.status_code == 200 for response in responses)
    statuses = [response.json()["status"] for response in responses]
    assert statuses.count("processed") == 1
    assert statuses.count("duplicate") == 7
    assert query(
        """
        SELECT count(*), sum(amount_cents) FROM resident_ledger WHERE bigpms_event_id=%s
    """,
        (payment["event_id"],),
    ) == [(1, 150000)]


def test_seeded_payment_is_not_reimported(deliver, query, payment):
    """Ledger uniqueness also covers a payment imported before webhooks existed."""
    payment["event_id"] = "evt-1001"
    response = deliver(json.dumps(payment).encode())
    assert response.status_code == 200
    assert response.json() == {"status": "duplicate"}
    assert query("SELECT count(*) FROM resident_ledger WHERE bigpms_event_id='evt-1001'") == [(1,)]


def test_rejected_signature_does_not_poison_event(deliver, query, body, payment):
    """Bad signatures are audited separately before and after a valid delivery."""
    assert deliver(body, signature="bad").status_code == 401
    assert deliver(body).json() == {"status": "processed"}
    assert deliver(body, signature="").status_code == 401
    assert query("SELECT status FROM webhook_events WHERE event_id=%s", (payment["event_id"],)) == [
        ("processed",)
    ]
    assert query(
        """
        SELECT count(*) FROM webhook_events
        WHERE status='failed' AND payload->>'raw_body'=%s
    """,
        (body.decode(),),
    ) == [(2,)]


@pytest.mark.parametrize("case", ["invalid_amount", "unknown_resident", "malformed_json"])
def test_invalid_delivery_is_audited_without_payment(deliver, query, payment, case):
    """Signed bad data receives 422 and leaves no ledger entry."""
    if case == "invalid_amount":
        payment["data"]["amount_cents"] = -1
    elif case == "unknown_resident":
        payment["data"]["resident_id"] = "res-unknown"
    body = json.dumps(payment).encode()
    if case == "malformed_json":
        body = body[:-1]
    assert deliver(body).status_code == 422
    assert query(
        "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id=%s", (payment["event_id"],)
    ) == [(0,)]
    assert query(
        "SELECT status FROM webhook_events WHERE payload->>'raw_body'=%s", (body.decode(),)
    ) == [("failed",)]


def test_audit_write_failure_rolls_back_then_retry_succeeds(deliver, query, body, payment):
    """A real SQL constraint failure after INSERT must roll back the payment too."""
    # This constraint exists only in the disposable E2E database and targets one ID.
    query(
        "ALTER TABLE webhook_events ADD CONSTRAINT e2e_reject_audit CHECK (event_id <> %s)",
        (payment["event_id"],),
    )
    try:
        assert deliver(body).status_code == 503
        assert query(
            "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id=%s", (payment["event_id"],)
        ) == [(0,)]
        assert query(
            "SELECT status FROM webhook_events WHERE payload->>'raw_body'=%s", (body.decode(),)
        ) == [("failed",)]
    finally:
        query("ALTER TABLE webhook_events DROP CONSTRAINT e2e_reject_audit")
    response = deliver(body)
    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
