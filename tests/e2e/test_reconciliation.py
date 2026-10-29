"""Exercise the real report API against PostgreSQL and a stable HTTP partner."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from test_feed_sync import isolated_db, webhook_app, wait_for, workers


@pytest.fixture
def partner(isolated_db):
    """Serve a quiescent 20-payment history and bulk totals with injectable failures."""
    initial = isolated_db.query(
        "SELECT bigpms_event_id,resident_id,amount_cents,occurred_at FROM resident_ledger WHERE entry_type='payment' AND bigpms_event_id <> 'evt-9999' ORDER BY bigpms_event_id"
    )
    events = [
        {
            "seq": int(event_id.split("-")[1]) - 1000,
            "event_id": event_id,
            "resident_id": resident,
            "amount_cents": amount,
            "occurred_at": occurred.isoformat(),
            "event_type": "payment.received",
        }
        for event_id, resident, amount, occurred in initial
    ]
    for seq, resident, amount in [
        (13, "res-105", 150000),
        (14, "res-106", 155000),
        (15, "res-107", 140000),
        (16, "res-108", 165000),
        (17, "res-101", 150000),
        (18, "res-102", 145000),
        (19, "res-103", 160000),
        (20, "res-104", 172500),
    ]:
        events.append(
            {
                "seq": seq,
                "event_id": f"evt-{1000 + seq}",
                "resident_id": resident,
                "amount_cents": amount,
                "occurred_at": "2026-06-05T11:30:00Z",
                "event_type": "payment.received",
            }
        )
    totals = {}
    for event in events:
        totals[event["resident_id"]] = totals.get(event["resident_id"], 0) + event["amount_cents"]
    state = SimpleNamespace(
        calls=[],
        code=200,
        rows=[
            {"resident_id": key, "total_payments_cents": value}
            for key, value in sorted(totals.items())
        ],
    )

    class Handler(BaseHTTPRequestHandler):
        """Expose bulk summaries, feed pagination and startup registration over HTTP."""

        def do_GET(self):
            """Return stable summaries or feed rows, recording every requested endpoint."""
            parsed = urlparse(self.path)
            state.calls.append(parsed.path)
            code = 200
            if parsed.path == "/api/v1/residents/summaries":
                code, body = state.code, {"residents": state.rows}
            elif parsed.path == "/api/v1/ledger-events":
                params = parse_qs(parsed.query)
                cursor, limit = int(params["cursor"][0]), int(params["limit"][0])
                remaining = [e for e in events if e["seq"] > cursor]
                page = remaining[:limit]
                body = {
                    "events": page,
                    "next_cursor": max((e["seq"] for e in page), default=cursor),
                    "has_more": len(remaining) > limit,
                }
            else:
                code, body = 404, {"error": "Unexpected per-resident or unknown request"}
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            """Accept the actual app's startup subscription without generating payments."""
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(201)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            """Suppress HTTP fixture noise; application logs are retained by the app fixture."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_seed_report_converges_after_feed_sync(isolated_db, partner, webhook_app, workers):
    """The API proves eight missing histories become seven matches and one local excess."""
    before = isolated_db.query("SELECT count(*),sum(amount_cents) FROM resident_ledger")
    result = requests.get(webhook_app + "/api/v1/reconciliation", timeout=10)
    assert result.status_code == 200, result.text
    report = result.json()
    assert len(report["residents"]) == 8
    assert all(r["status"] == "missing_local_payments" for r in report["residents"])
    assert report["summary"] == {"residents_out_of_sync": 8, "issues_found": 2}
    assert isolated_db.query("SELECT count(*),sum(amount_cents) FROM resident_ledger") == before
    assert partner.calls == ["/api/v1/residents/summaries"]
    assert workers().wait(timeout=20) == 0
    after_sync = isolated_db.query("SELECT count(*),sum(amount_cents) FROM resident_ledger")
    partner.calls.clear()
    result = requests.get(webhook_app + "/api/v1/reconciliation", timeout=10)
    assert result.status_code == 200, result.text
    report = result.json()
    assert sum(r["status"] == "in_sync" for r in report["residents"]) == 7
    extra = next(r for r in report["residents"] if r["resident_id"] == "res-107")
    assert extra["status"] == "local_exceeds_bigpms"
    assert extra["delta_cents"] > 0
    assert extra["delta_cents"] == extra["local_payments_cents"] - extra["bigpms_payments_cents"]
    assert report["summary"] == {"residents_out_of_sync": 1, "issues_found": 2}
    assert report["issues"]["duplicate_charges"] == [
        {
            "resident_id": "res-105",
            "charge_code": "rent",
            "amount_cents": 150000,
            "occurred_on": "2026-06-01",
            "count": 2,
        }
    ]
    assert report["issues"]["orphaned_ledger_entries"][0]["resident_id"] == "res-999"
    assert partner.calls == ["/api/v1/residents/summaries"]
    assert isolated_db.query("SELECT count(*),sum(amount_cents) FROM resident_ledger") == after_sync


def test_zero_payments_and_utc_charge_grouping(isolated_db, partner, webhook_app):
    """Zero-payment residents remain visible; charge duplication uses UTC calendar dates."""
    isolated_db.query(
        "DELETE FROM resident_ledger WHERE resident_id='res-108' AND entry_type='payment'"
    )
    for row in partner.rows:
        if row["resident_id"] == "res-108":
            row["total_payments_cents"] = 0
    isolated_db.query(
        "INSERT INTO resident_ledger(property_id,resident_id,entry_type,charge_code,amount_cents,occurred_at) VALUES ('prop-001','res-101','charge','test_utc',9,'2026-08-01T00:30:00+00:00'),('prop-001','res-101','charge','test_utc',9,'2026-07-31T20:30:00-04:00'),('prop-001','res-101','charge','test_utc',9,'2026-08-02T00:30:00+00:00')"
    )
    report = requests.get(webhook_app + "/api/v1/reconciliation", timeout=10).json()
    zero = next(r for r in report["residents"] if r["resident_id"] == "res-108")
    assert zero["local_payments_cents"] == zero["bigpms_payments_cents"] == 0
    assert zero["status"] == "in_sync"
    group = next(g for g in report["issues"]["duplicate_charges"] if g["charge_code"] == "test_utc")
    assert group["occurred_on"] == "2026-08-01" and group["count"] == 2
    assert report["summary"]["issues_found"] == 3


@pytest.mark.parametrize("failure", ["outage", "missing", "duplicate", "negative"])
def test_partner_failure_returns_503_without_writes(isolated_db, partner, webhook_app, failure):
    """Unavailable or invalid bulk summaries cannot masquerade as a successful report."""
    if failure == "outage":
        partner.code = 500
    elif failure == "missing":
        partner.rows.pop()
    elif failure == "duplicate":
        partner.rows.append(partner.rows[0])
    else:
        partner.rows[0]["total_payments_cents"] = -1
    before = isolated_db.query("SELECT count(*),sum(amount_cents) FROM resident_ledger")
    result = requests.get(webhook_app + "/api/v1/reconciliation", timeout=20)
    assert result.status_code == 503
    assert result.json() == {"detail": "Reconciliation unavailable"}
    assert isolated_db.query("SELECT count(*),sum(amount_cents) FROM resident_ledger") == before
    assert len(partner.calls) == (3 if failure == "outage" else 1)
    assert set(partner.calls) == {"/api/v1/residents/summaries"}
