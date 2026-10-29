"""Unit contracts for bulk summaries, report classification and read-only storage."""

from unittest.mock import MagicMock, Mock

import psycopg2
import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient


def response(code=200, rows=None):
    """Create a response with real Requests HTTP error behavior."""
    result = requests.Response()
    result.status_code = code
    result.url = "http://partner/api/v1/residents/summaries"
    result.json = Mock(return_value={"residents": rows if rows is not None else []})
    return result


def test_bulk_request_contract(modules, monkeypatch):
    """All resident totals come from one bulk HTTP call with explicit timeouts."""
    get = Mock(return_value=response(rows=[{"resident_id": "r1", "total_payments_cents": 0}]))
    monkeypatch.setattr(modules.reconciliation.requests, "get", get)
    assert modules.reconciliation.fetch_payment_totals("http://partner/") == {"r1": 0}
    get.assert_called_once_with("http://partner/api/v1/residents/summaries", timeout=(3, 10))


def test_summary_retry_and_rate_limit(modules, monkeypatch):
    """Transient errors retry the bulk endpoint and respect a partner cooldown."""
    limited = response(429)
    limited.headers["Retry-After"] = "4"
    get = Mock(side_effect=[response(500), limited, response()])
    sleep = Mock()
    monkeypatch.setattr(modules.reconciliation.requests, "get", get)
    monkeypatch.setattr(modules.reconciliation.time, "sleep", sleep)
    assert modules.reconciliation.fetch_payment_totals("http://partner") == {}
    assert [c.args[0] for c in sleep.call_args_list] == [1, 4]


@pytest.mark.parametrize("error", [requests.Timeout(), requests.ConnectionError()])
def test_summary_network_exhaustion(modules, monkeypatch, error):
    """Network failures stop after three attempts."""
    get = Mock(side_effect=error)
    monkeypatch.setattr(modules.reconciliation.requests, "get", get)
    monkeypatch.setattr(modules.reconciliation.time, "sleep", Mock())
    with pytest.raises(type(error)):
        modules.reconciliation.fetch_payment_totals("http://partner")
    assert get.call_count == 3


@pytest.mark.parametrize(
    "rows",
    [
        [{"resident_id": "r1", "total_payments_cents": -1}],
        [{"resident_id": "r1", "total_payments_cents": True}],
        [{"resident_id": "r1", "total_payments_cents": "10"}],
        [{"resident_id": "r1", "total_payments_cents": 10}] * 2,
    ],
)
def test_invalid_or_duplicate_totals_fail(modules, monkeypatch, rows):
    """Malformed amounts and duplicate identities never produce a plausible report."""
    get = Mock(return_value=response(rows=rows))
    monkeypatch.setattr(modules.reconciliation.requests, "get", get)
    with pytest.raises(ValueError):
        modules.reconciliation.fetch_payment_totals("http://partner")
    get.assert_called_once()


def test_permanent_error_is_not_retried(modules, monkeypatch):
    """An invalid endpoint fails immediately."""
    get = Mock(return_value=response(404))
    monkeypatch.setattr(modules.reconciliation.requests, "get", get)
    with pytest.raises(requests.HTTPError):
        modules.reconciliation.fetch_payment_totals("http://partner")
    get.assert_called_once()


def test_classification_and_independent_issue_counts(modules, monkeypatch):
    """Report all three delta directions, zero payments and distinct issue counters."""
    rows = [
        {"resident_id": key, "name": key, "local_payments_cents": value}
        for key, value in [("equal", 100), ("missing", 50), ("extra", 150), ("zero", 0)]
    ]
    issues = {"duplicate_charges": [{"count": 3}], "orphaned_ledger_entries": [{"ledger_id": 1}]}
    monkeypatch.setattr(
        modules.reconciliation,
        "fetch_payment_totals",
        Mock(return_value={"equal": 100, "missing": 100, "extra": 100, "zero": 0}),
    )
    monkeypatch.setattr(
        modules.reconciliation, "read_reconciliation_snapshot", Mock(return_value=(rows, issues))
    )
    report = modules.reconciliation.build_reconciliation_report("http://partner")
    assert [r["delta_cents"] for r in report["residents"]] == [0, -50, 50, 0]
    assert [r["status"] for r in report["residents"]] == [
        "in_sync",
        "missing_local_payments",
        "local_exceeds_bigpms",
        "in_sync",
    ]
    assert report["summary"] == {"residents_out_of_sync": 2, "issues_found": 2}
    assert report["generated_at"].endswith("+00:00")


@pytest.mark.parametrize("totals", [{}, {"r1": 0, "unknown": 0}])
def test_incomplete_roster_returns_unavailable(modules, monkeypatch, totals):
    """Missing or unexpected partner residents are explicit comparison failures."""
    monkeypatch.setattr(modules.reconciliation, "fetch_payment_totals", Mock(return_value=totals))
    monkeypatch.setattr(
        modules.reconciliation,
        "read_reconciliation_snapshot",
        Mock(return_value=([{"resident_id": "r1", "local_payments_cents": 0}], {})),
    )
    with pytest.raises(HTTPException) as error:
        modules.reconciliation.build_reconciliation_report("http://partner")
    assert error.value.status_code == 503


@pytest.mark.parametrize(
    "failure", [requests.ConnectionError(), ValueError("bad JSON"), psycopg2.OperationalError()]
)
def test_report_failures_return_503(modules, monkeypatch, failure):
    """Unavailable or invalid source data never returns a misleading success."""
    monkeypatch.setattr(modules.reconciliation, "fetch_payment_totals", Mock(return_value={}))
    monkeypatch.setattr(
        modules.reconciliation, "read_reconciliation_snapshot", Mock(side_effect=failure)
    )
    with pytest.raises(HTTPException) as error:
        modules.reconciliation.build_reconciliation_report("http://partner")
    assert error.value.status_code == 503


def test_route_calls_report_service(modules, monkeypatch):
    """The existing route exposes the implemented report."""
    build = Mock(return_value={"summary": {"issues_found": 2}})
    monkeypatch.setattr(modules.main, "build_reconciliation_report", build)
    result = TestClient(modules.main.app).get("/api/v1/reconciliation")
    assert result.status_code == 200
    assert result.json() == build.return_value
    build.assert_called_once_with(modules.main.BIGPMS_API_URL)


@pytest.mark.parametrize("fail", [False, True])
def test_snapshot_readonly_and_connection_release(modules, monkeypatch, fail):
    """One read-only repeatable-read transaction returns all queries or rolls back."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [[{"resident_id": "r1"}], [{"count": 2}], [{"ledger_id": 1}]]
    if fail:
        cur.execute.side_effect = psycopg2.OperationalError("offline")
    release = Mock()
    monkeypatch.setattr(modules.orm, "get_conn", Mock(return_value=conn))
    monkeypatch.setattr(modules.orm, "put_conn", release)
    monkeypatch.setattr(modules.orm, "dict_cursor", lambda c: c.cursor())
    if fail:
        with pytest.raises(psycopg2.OperationalError):
            modules.orm.read_reconciliation_snapshot()
        assert conn.__exit__.call_args.args[0] is psycopg2.OperationalError
    else:
        residents, issues = modules.orm.read_reconciliation_snapshot()
        assert residents == [{"resident_id": "r1"}]
        assert issues["duplicate_charges"] == [{"count": 2}]
        assert "REPEATABLE READ, READ ONLY" in cur.execute.call_args_list[0].args[0]
        assert cur.execute.call_count == 4
    release.assert_called_once_with(conn)
