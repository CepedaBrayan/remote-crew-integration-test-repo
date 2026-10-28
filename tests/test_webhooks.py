import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import psycopg2
import pytest
import requests

SECRET = "leaseflow-dev-secret"


def payment(event_id="evt-test-1"):
    return {
        "event_id": event_id,
        "event_type": "payment.received",
        "occurred_at": "2026-07-07T14:30:00Z",
        "data": {"resident_id": "res-101", "amount_cents": 150000,
                 "method": "ach", "seq": 21},
    }


def send(client, payload=None, *, body=None, signature=None):
    if body is None:
        body = json.dumps(payload if payload is not None else payment()).encode()
    if signature is None:
        signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/bigpms", content=body, headers={
        "Content-Type": "application/json", "X-BigPMS-Signature": signature,
    })


def row(database, query, params=()):
    with database, database.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()


def test_valid_payment_is_saved_with_its_audit(client, database):
    assert send(client).json() == {"status": "processed"}
    assert row(database, """
        SELECT property_id, resident_id, amount_cents, entry_type
        FROM resident_ledger WHERE bigpms_event_id = 'evt-test-1'
    """) == ("prop-001", "res-101", 150000, "payment")
    status, payload = row(database, "SELECT status, payload FROM webhook_events")
    assert status == "processed"
    assert payload == payment()  # Includes method and seq, although not used locally.


def test_redelivery_is_acknowledged_without_another_payment(client, database):
    assert send(client).status_code == 200
    response = send(client)
    assert response.status_code == 200
    assert response.json() == {"status": "duplicate"}
    assert row(database, "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id='evt-test-1'") == (1,)
    assert row(database, "SELECT status FROM webhook_events") == ("duplicate",)


def test_payment_already_imported_without_webhook_audit_is_duplicate(client, database):
    assert send(client, payment("evt-1001")).json() == {"status": "duplicate"}
    assert row(database, "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id='evt-1001'") == (1,)


def test_concurrent_deliveries_count_payment_once(client, database):
    barrier = Barrier(8)

    def deliver(_):
        barrier.wait()
        return send(client)

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(deliver, range(8)))
    assert all(r.status_code == 200 for r in responses)
    statuses = [r.json()["status"] for r in responses]
    assert statuses.count("processed") == 1
    assert statuses.count("duplicate") == 7
    assert row(database, "SELECT count(*), sum(amount_cents) FROM resident_ledger WHERE bigpms_event_id='evt-test-1'") == (1, 150000)


@pytest.mark.parametrize("signature", ["", "deadbeef", "a" * 64])
def test_invalid_signatures_are_rejected_and_audited(client, database, signature):
    assert send(client, signature=signature).status_code == 401
    assert row(database, "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id='evt-test-1'") == (0,)
    assert row(database, "SELECT status, error FROM webhook_events") == ("failed", "Invalid signature")


def test_missing_signature(client, database):
    assert client.post("/webhooks/bigpms", json=payment()).status_code == 401
    assert row(database, "SELECT status FROM webhook_events") == ("failed",)


def test_signature_covers_exact_bytes(client):
    body = json.dumps(payment(), indent=2).encode()
    assert send(client, body=body).status_code == 200
    original_signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert send(client, body=body + b" ", signature=original_signature).status_code == 401


def test_bad_signature_cannot_poison_valid_event_id(client, database):
    assert send(client, signature="bad").status_code == 401
    assert send(client).json() == {"status": "processed"}
    assert send(client, signature="bad").status_code == 401
    assert row(database, "SELECT status FROM webhook_events WHERE event_id='evt-test-1'") == ("processed",)
    assert row(database, "SELECT count(*) FROM webhook_events WHERE status='failed'") == (2,)


@pytest.mark.parametrize("body", [b"{", b"null", b"[]", b"{}", b"\xff"])
def test_malformed_payload_is_rejected_and_audited(client, database, body):
    assert send(client, body=body).status_code == 422
    assert row(database, "SELECT status FROM webhook_events") == ("failed",)


@pytest.mark.parametrize("amount", [0, -1, True, "150000", 1.5, 2_147_483_648])
def test_invalid_amount_is_rejected(client, amount):
    payload = payment()
    payload["data"]["amount_cents"] = amount
    assert send(client, payload).status_code == 422


@pytest.mark.parametrize("field,value", [
    ("event_id", ""), ("event_type", "resident.updated"),
    ("occurred_at", "not-a-date"), ("occurred_at", "2026-07-07T14:30:00"),
])
def test_invalid_event_fields(client, field, value):
    payload = payment()
    payload[field] = value
    assert send(client, payload).status_code == 422


def test_unknown_resident_is_not_applied(client, database):
    payload = payment()
    payload["data"]["resident_id"] = "res-missing"
    assert send(client, payload).status_code == 422
    assert row(database, "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id='evt-test-1'") == (0,)
    assert row(database, "SELECT status, error FROM webhook_events") == ("failed", "Unknown resident")


def test_audit_write_failure_rolls_back_payment_then_retry_succeeds(client, database):
    # Force a real PostgreSQL failure after the ledger INSERT has succeeded.
    with database, database.cursor() as cur:
        cur.execute("""
            ALTER TABLE webhook_events ADD CONSTRAINT reject_test_payment
            CHECK (event_id <> 'evt-test-1')
        """)
    try:
        assert send(client).status_code == 503
        assert row(database, "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id='evt-test-1'") == (0,)
        assert row(database, "SELECT status, error FROM webhook_events") == ("failed", "Payment transaction failed")
    finally:
        with database, database.cursor() as cur:
            cur.execute("ALTER TABLE webhook_events DROP CONSTRAINT reject_test_payment")
    assert send(client).json() == {"status": "processed"}


def test_database_unavailability_returns_retryable_error(client, monkeypatch):
    import webhooks
    monkeypatch.setattr(webhooks, "get_conn", Mock(side_effect=psycopg2.OperationalError("offline")))
    assert send(client).status_code == 503
    assert send(client, signature="bad").status_code == 401


def test_startup_creates_index_before_registering(monkeypatch):
    import main
    calls = []
    monkeypatch.setattr(main, "ensure_payment_uniqueness", lambda: calls.append("index"))
    monkeypatch.setattr(main, "register_subscription", lambda *args: calls.append(args))
    main.register_webhook_subscription()
    assert calls == ["index", (main.BIGPMS_API_URL, main.CALLBACK_URL, main.WEBHOOK_SECRET)]


def test_registration_sends_callback_secret_and_timeout(monkeypatch):
    import webhooks
    post = Mock(return_value=Mock())
    monkeypatch.setattr(webhooks.requests, "post", post)
    webhooks.register_subscription("http://partner/", "http://app/webhook", "secret")
    post.assert_called_once_with("http://partner/api/v1/webhook-subscriptions",
                                 json={"callback_url": "http://app/webhook", "secret": "secret"},
                                 timeout=5)
    post.return_value.raise_for_status.assert_called_once()


def test_registration_retries_then_recovers(monkeypatch):
    import webhooks
    post = Mock(side_effect=[requests.Timeout(), Mock()])
    sleep = Mock()
    monkeypatch.setattr(webhooks.requests, "post", post)
    monkeypatch.setattr(webhooks.time, "sleep", sleep)
    webhooks.register_subscription("http://partner", "http://app/webhook", "secret")
    assert post.call_count == 2
    sleep.assert_called_once_with(1)


def test_registration_failure_stops_startup(monkeypatch):
    import webhooks
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("unavailable")
    post = Mock(return_value=response)
    monkeypatch.setattr(webhooks.requests, "post", post)
    monkeypatch.setattr(webhooks.time, "sleep", Mock())
    with pytest.raises(requests.HTTPError):
        webhooks.register_subscription("http://partner", "http://app/webhook", "secret")
    assert post.call_count == 3
