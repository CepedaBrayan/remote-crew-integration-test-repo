"""Unit contracts for authentication, registration, validation, persistence and routing."""

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import psycopg2
import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError


def test_valid_signature_has_no_audit_side_effect(modules, monkeypatch, body, sign):
    """A valid signature passes without writing a rejection receipt."""
    audit = Mock()
    monkeypatch.setattr(modules.auth, "audit_failure", audit)
    asyncio.run(modules.auth.auth(body, sign(body), "leaseflow-dev-secret"))
    audit.assert_not_called()


@pytest.mark.parametrize("signature", ["", "deadbeef", "é", "a" * 64])
def test_invalid_signature_is_audited(modules, monkeypatch, body, signature):
    """Missing, malformed and incorrect signatures are rejected with HTTP 401."""
    audit = Mock()
    monkeypatch.setattr(modules.auth, "audit_failure", audit)
    with pytest.raises(HTTPException) as error:
        asyncio.run(modules.auth.auth(body, signature, "leaseflow-dev-secret"))
    assert error.value.status_code == 401
    audit.assert_called_once_with(body, "Invalid signature")


def test_signature_uses_exact_body_bytes(modules, monkeypatch, body, sign):
    """Even harmless JSON whitespace invalidates a signature made for other bytes."""
    monkeypatch.setattr(modules.auth, "audit_failure", Mock())
    with pytest.raises(HTTPException) as error:
        asyncio.run(modules.auth.auth(body + b" ", sign(body), "leaseflow-dev-secret"))
    assert error.value.status_code == 401


@pytest.mark.parametrize("amount", [0, -1, True, "150000", 1.5, 2_147_483_648])
def test_payment_rejects_invalid_amount(modules, payment, amount):
    """Money must be positive integer cents fitting the supplied SQL INTEGER."""
    payment["data"]["amount_cents"] = amount
    with pytest.raises(ValidationError):
        modules.dto.PaymentEvent.model_validate(payment)


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", ""),
        ("event_type", "resident.updated"),
        ("occurred_at", "bad"),
        ("occurred_at", "2026-07-07T14:30:00"),
    ],
)
def test_payment_rejects_invalid_event_fields(modules, payment, field, value):
    """Event identity, type and timezone-aware timestamp are required."""
    payment[field] = value
    with pytest.raises(ValidationError):
        modules.dto.PaymentEvent.model_validate(payment)


def test_model_preserves_nested_partner_metadata(modules, payment):
    """The audit representation retains extra fields at both nesting levels."""
    event = modules.dto.PaymentEvent.model_validate(payment)
    assert event.model_dump(mode="json") == payment


def test_registration_request_contract(modules, monkeypatch):
    """Registration sends callback/secret, normalizes the URL and sets a timeout."""
    post = Mock(return_value=Mock())
    monkeypatch.setattr(modules.webhooks.requests, "post", post)
    modules.webhooks.register_subscription("http://partner/", "http://app/callback", "secret")
    post.assert_called_once_with(
        "http://partner/api/v1/webhook-subscriptions",
        json={"callback_url": "http://app/callback", "secret": "secret"},
        timeout=5,
    )
    post.return_value.raise_for_status.assert_called_once()


def test_registration_recovers_after_transient_failures(modules, monkeypatch):
    """Connection errors and timeouts are retried with bounded waits."""
    post = Mock(side_effect=[requests.ConnectionError(), requests.Timeout(), Mock()])
    sleep = Mock()
    monkeypatch.setattr(modules.webhooks.requests, "post", post)
    monkeypatch.setattr(modules.webhooks.time, "sleep", sleep)
    modules.webhooks.register_subscription("http://partner", "http://app", "secret")
    assert post.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]


def test_registration_propagates_final_http_failure(modules, monkeypatch):
    """Exhausting retries propagates the failure to abort application startup."""
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("unavailable")
    post = Mock(return_value=response)
    monkeypatch.setattr(modules.webhooks.requests, "post", post)
    monkeypatch.setattr(modules.webhooks.time, "sleep", Mock())
    with pytest.raises(requests.HTTPError):
        modules.webhooks.register_subscription("http://partner", "http://app", "secret")
    assert post.call_count == 3


@pytest.mark.parametrize("status", ["processed", "duplicate"])
def test_processing_passes_only_validated_model(modules, monkeypatch, body, status):
    """The persistence layer receives one validated DTO and its result is returned."""
    apply = Mock(return_value=status)
    monkeypatch.setattr(modules.webhooks, "apply_payment", apply)
    assert modules.webhooks.process_webhook(body) == {"status": status}
    assert isinstance(apply.call_args.args[0], modules.dto.PaymentEvent)
    assert len(apply.call_args.args) == 1


@pytest.mark.parametrize("body", [b"{", b"null", b"[]", b"{}", b"\xff"])
def test_invalid_body_never_reaches_persistence(modules, monkeypatch, body):
    """Malformed JSON and invalid models receive 422 without applying a payment."""
    apply, audit = Mock(), Mock()
    monkeypatch.setattr(modules.webhooks, "apply_payment", apply)
    monkeypatch.setattr(modules.webhooks, "audit_failure", audit)
    with pytest.raises(HTTPException) as error:
        modules.webhooks.process_webhook(body)
    assert error.value.status_code == 422
    apply.assert_not_called()
    audit.assert_called_once_with(body, "Invalid payment payload")


@pytest.mark.parametrize(
    "failure,code,reason",
    [
        (ValueError("Unknown resident"), 422, "Unknown resident"),
        (psycopg2.OperationalError("offline"), 503, "Payment transaction failed"),
    ],
)
def test_processing_maps_storage_errors(modules, monkeypatch, body, failure, code, reason):
    """Expected persistence errors are audited and mapped to the HTTP contract."""
    audit = Mock()
    monkeypatch.setattr(modules.webhooks, "apply_payment", Mock(side_effect=failure))
    monkeypatch.setattr(modules.webhooks, "audit_failure", audit)
    with pytest.raises(HTTPException) as error:
        modules.webhooks.process_webhook(body)
    assert error.value.status_code == code
    audit.assert_called_once_with(body, reason)


def test_audit_generates_independent_receipt_ids(modules, monkeypatch, body):
    """Each rejected delivery gets a separate UUID rather than its partner event ID."""
    record = Mock()
    monkeypatch.setattr(modules.utils, "record_failure", record)
    modules.utils.audit_failure(body, "bad")
    modules.utils.audit_failure(body, "bad")
    ids = [call.args[0] for call in record.call_args_list]
    assert all(isinstance(value, UUID) for value in ids)
    assert ids[0] != ids[1]


def test_audit_database_failure_is_logged_not_propagated(modules, monkeypatch, body, caplog):
    """An unavailable audit store must not replace the intended rejection response."""
    monkeypatch.setattr(
        modules.utils, "record_failure", Mock(side_effect=psycopg2.OperationalError())
    )
    modules.utils.audit_failure(body, "bad signature")
    assert "Could not record failed webhook" in caplog.text


def test_index_creation_returns_connection(modules, database):
    """Startup installs the cross-channel unique index and releases the connection."""
    modules.orm.ensure_payment_events_uniqueness()
    statement = database.cur.execute.call_args.args[0]
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in statement
    assert "ON resident_ledger (bigpms_event_id)" in statement
    database.release.assert_called_once_with(database.conn)


def test_failure_receipt_uses_generated_id(modules, database):
    """Failed audit rows include a generated ID and tolerate invalid UTF-8 bodies."""
    receipt = uuid4()
    modules.orm.record_failure(receipt, b"\xff", "bad")
    params = database.cur.execute.call_args.args[1]
    assert params[0] == f"rejected-{receipt}"
    assert params[1].adapted == {"raw_body": "\ufffd"}
    assert params[2] == "bad"
    database.release.assert_called_once_with(database.conn)


@pytest.mark.parametrize("inserted,status", [((123,), "processed"), (None, "duplicate")])
def test_payment_persistence_contract(modules, database, payment, inserted, status):
    """Property lookup and INSERT result determine a single atomic audit outcome."""
    database.cur.fetchone.side_effect = [("prop-from-resident",), inserted]
    event = modules.dto.PaymentEvent.model_validate(payment)
    assert modules.orm.apply_payment(event) == status
    writes = database.cur.execute.call_args_list
    assert writes[1].args[1][0] == "prop-from-resident"
    assert writes[1].args[1][3] == event.event_id
    assert "ON CONFLICT (bigpms_event_id) DO NOTHING" in writes[1].args[0]
    assert writes[2].args[1][2].adapted == payment
    assert writes[2].args[1][3] == status
    assert database.conn.__exit__.call_args.args == (None, None, None)
    database.release.assert_called_once_with(database.conn)


def test_unknown_resident_stops_before_writes(modules, database, payment):
    """Missing residents raise a domain error without issuing payment/audit writes."""
    database.cur.fetchone.return_value = None
    with pytest.raises(ValueError, match="Unknown resident"):
        modules.orm.apply_payment(modules.dto.PaymentEvent.model_validate(payment))
    assert database.cur.execute.call_count == 1
    database.release.assert_called_once_with(database.conn)


def test_transaction_failure_reaches_context_manager(modules, database, payment):
    """An audit failure escapes the transaction and always releases its connection."""
    database.cur.fetchone.side_effect = [("prop-001",), (1,)]
    database.cur.execute.side_effect = [None, None, psycopg2.IntegrityError("audit failure")]
    with pytest.raises(psycopg2.IntegrityError):
        modules.orm.apply_payment(modules.dto.PaymentEvent.model_validate(payment))
    assert database.conn.__exit__.call_args.args[0] is psycopg2.IntegrityError
    database.release.assert_called_once_with(database.conn)


def test_health_and_resident_queries(modules, database):
    """Read helpers expose their results and return each borrowed connection."""
    assert modules.orm.get_health_check_status()["status"] == "ok"
    database.cur.fetchall.return_value = [{"id": "res-101", "unit_number": "101"}]
    assert modules.orm.get_residents() == {"residents": database.cur.fetchall.return_value}
    assert database.release.call_count == 2


@pytest.mark.parametrize(
    "name", ["ensure_payment_events_uniqueness", "get_health_check_status", "get_residents"]
)
def test_database_helpers_release_on_error(modules, database, name):
    """Read/setup errors propagate while the pool connection is returned."""
    database.cur.execute.side_effect = psycopg2.OperationalError("offline")
    with pytest.raises(psycopg2.OperationalError):
        getattr(modules.orm, name)()
    database.release.assert_called_once_with(database.conn)


def test_startup_orders_index_before_subscription(modules, monkeypatch):
    """The callback is registered only after duplicate protection succeeds."""
    calls = []
    monkeypatch.setattr(
        modules.main, "ensure_payment_events_uniqueness", lambda: calls.append("index")
    )
    monkeypatch.setattr(modules.main, "register_subscription", lambda *args: calls.append(args))
    modules.main.register_webhook_subscription()
    assert calls == [
        "index",
        (modules.main.BIGPMS_API_URL, modules.main.CALLBACK_URL, modules.main.WEBHOOK_SECRET),
    ]


def test_failed_index_prevents_registration(modules, monkeypatch):
    """Startup does not subscribe when duplicate protection cannot be installed."""
    register = Mock()
    monkeypatch.setattr(
        modules.main, "ensure_payment_events_uniqueness", Mock(side_effect=psycopg2.Error())
    )
    monkeypatch.setattr(modules.main, "register_subscription", register)
    with pytest.raises(psycopg2.Error):
        modules.main.register_webhook_subscription()
    register.assert_not_called()


def test_endpoint_authenticates_before_processing(modules, monkeypatch, body):
    """The HTTP boundary passes exact bytes to auth and processing in that order."""
    calls = []

    async def authenticate(*args):
        """Record authentication without accessing external dependencies."""
        calls.append(("auth", args))

    def process(raw):
        """Record processing after authentication has completed."""
        calls.append(("process", raw))
        return {"status": "processed"}

    monkeypatch.setattr(modules.main, "auth", authenticate)
    monkeypatch.setattr(modules.main, "process_webhook", process)
    response = TestClient(modules.main.app).post("/webhooks/bigpms", content=body)
    assert response.status_code == 200
    assert calls == [("auth", (body, "", modules.main.WEBHOOK_SECRET)), ("process", body)]


def test_endpoint_stops_on_authentication_error(modules, monkeypatch, body):
    """Invalid signatures cannot reach payment processing through the HTTP route."""
    process = Mock()
    monkeypatch.setattr(
        modules.main, "auth", AsyncMock(side_effect=HTTPException(401, "Invalid signature"))
    )
    monkeypatch.setattr(modules.main, "process_webhook", process)
    assert TestClient(modules.main.app).post("/webhooks/bigpms", content=body).status_code == 401
    process.assert_not_called()


def test_scheduler_lifecycle(modules, monkeypatch):
    """Lifecycle hooks start the scheduler and stop it without waiting."""
    scheduler = Mock()
    monkeypatch.setattr(modules.main, "scheduler", scheduler)
    modules.main.start_scheduler()
    modules.main.stop_scheduler()
    scheduler.start.assert_called_once_with()
    scheduler.shutdown.assert_called_once_with(wait=False)
