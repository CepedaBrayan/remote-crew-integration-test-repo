"""Real HTTP and PostgreSQL recovery tests, with deterministic failure injection.

The scheduled backfill test uses the untouched mock. Other scenarios launch the
production worker in a separate process against a scripted HTTP fixture and a
fresh database, so failures cannot race the main app's scheduler or pollute it.
"""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn
import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "bigpms_ledger_events"


def wait_for(check, timeout=15):
    """Poll a concrete condition with a bounded deadline and return its truthy value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("Condition did not become true before the deadline")


def test_scheduled_backfill_from_untouched_partner(query):
    """The actual app scheduler fills all eight missing seed payments automatically."""
    ids = [f"evt-{number}" for number in range(1013, 1021)]
    wait_for(
        lambda: (
            query("SELECT count(*) FROM resident_ledger WHERE bigpms_event_id = ANY(%s)", (ids,))[
                0
            ][0]
            == 8
        ),
        timeout=100,
    )
    wait_for(
        lambda: query("SELECT id FROM sync_runs WHERE status='success' AND cursor_end >= 20"),
        timeout=30,
    )
    assert query(
        "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id = ANY(%s)", (ids,)
    ) == [(8,)]
    assert query("SELECT count(*) FROM resident_ledger WHERE bigpms_event_id='evt-9999'") == [(1,)]
    assert query(
        "SELECT count(*) FROM sync_runs WHERE events_fetched <> events_applied + events_skipped"
    ) == [(0,)]


@pytest.fixture
def isolated_db():
    """Create a per-test seeded database and remove it after all child processes exit."""
    name = f"sync_e2e_{uuid4().hex}"
    admin = psycopg2.connect(os.environ["E2E_DATABASE_URL"])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_dsn(os.environ["E2E_DATABASE_URL"], dbname=name)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute((ROOT / "db/init.sql").read_text())
            cur.execute((ROOT / "db/seed.sql").read_text())
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from orm import ensure_payment_events_uniqueness; ensure_payment_events_uniqueness()",
            ],
            env=os.environ | {"DATABASE_URL": dsn, "PYTHONPATH": str(ROOT / "app")},
            check=True,
            timeout=10,
        )

        def query(statement, params=()):
            """Execute committed SQL against this test's isolated database only."""
            with conn.cursor() as cur:
                cur.execute(statement, params)
                return cur.fetchall() if cur.description else []

        yield SimpleNamespace(dsn=dsn, query=query)
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def partner():
    """Serve scripted feed responses over real HTTP without changing mock BigPMS."""
    state = SimpleNamespace(calls=[], handler=None)

    class Handler(BaseHTTPRequestHandler):
        """Implement only feed and registration endpoints used by these scenarios."""

        def do_GET(self):
            """Record cursor/time and return the test's chosen status, body and headers."""
            cursor = int(parse_qs(urlparse(self.path).query)["cursor"][0])
            state.calls.append((cursor, time.monotonic()))
            code, body, headers = state.handler(cursor)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(json.dumps(body).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass  # A crash test deliberately kills the requesting process.

        def do_POST(self):
            """Accept subscription registration for the isolated webhook app process."""
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(201)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            """Keep test output focused on assertions; worker logs are retained separately."""

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


@pytest.fixture
def workers(isolated_db, partner):
    """Launch actual sync workers, preserve diagnostics and reap every child process."""
    children = []
    env = os.environ | {"DATABASE_URL": isolated_db.dsn, "PYTHONPATH": str(ROOT / "app")}

    def start():
        """Start one production sync invocation with real network and database clients."""
        path = Path("/artifacts") / f"sync-worker-{uuid4().hex}.log"
        output = path.open("w")
        code = (
            "import logging,sys; logging.basicConfig(level=logging.INFO); "
            "from sync import run_feed_sync; run_feed_sync(sys.argv[1], 2)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code, partner.url],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        children.append((process, output))
        return process

    try:
        yield start
    finally:
        for process, output in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            output.close()


def feed_event(seq, event_id=None):
    """Build a unique flat feed payment compatible with the supplied seed residents."""
    return {
        "seq": seq,
        "event_id": event_id or f"e2e-{uuid4()}",
        "event_type": "payment.received",
        "resident_id": "res-101",
        "amount_cents": 1234,
        "occurred_at": "2026-07-07T14:30:00Z",
    }


def page(events, cursor, more=False):
    """Build a successful HTTP feed response, optionally repeating events."""
    return 200, {"events": events, "next_cursor": cursor, "has_more": more}, {}


def run_result(database):
    """Read the newest durable audit row including committed progress counters."""
    return database.query(
        "SELECT cursor_start, cursor_end, events_fetched, events_applied, events_skipped, status FROM sync_runs ORDER BY id DESC LIMIT 1"
    )[0]


def test_http_retries_duplicates_and_empty_run(isolated_db, partner, workers):
    """Real 500/429 responses retry the same cursor, then commit duplicates only once."""
    event = feed_event(1)

    def respond(cursor):
        """Fail the first two requests, then return a duplicate-containing page."""
        if len(partner.calls) == 1:
            return 500, {"error": "temporary"}, {}
        if len(partner.calls) == 2:
            return 429, {"error": "slow down"}, {"Retry-After": "1"}
        return page([event, event], 1) if cursor == 0 else page([], 1)

    partner.handler = respond
    assert workers().wait(timeout=30) == 0
    assert run_result(isolated_db) == (0, 1, 2, 1, 1, "success")
    assert [c[0] for c in partner.calls] == [0, 0, 0]
    assert partner.calls[2][1] - partner.calls[1][1] >= 0.95
    assert isolated_db.query("SELECT count(*) FROM webhook_events") == [(0,)]
    assert workers().wait(timeout=15) == 0
    assert run_result(isolated_db) == (1, 1, 0, 0, 0, "success")


def test_partial_failure_resumes_last_committed_page(isolated_db, partner, workers):
    """Exhausted retries after page one preserve its data and resume at its cursor."""
    first, second = feed_event(1), feed_event(2)
    partner.handler = lambda cursor: (
        page([first], 1, True) if cursor == 0 else (500, {"error": "offline"}, {})
    )
    assert workers().wait(timeout=30) == 0
    assert run_result(isolated_db) == (0, 1, 1, 1, 0, "failed")
    assert isolated_db.query("SELECT cursor_seq FROM sync_state") == [(1,)]
    partner.calls.clear()
    partner.handler = lambda cursor: page([second], 2)
    assert workers().wait(timeout=15) == 0
    assert partner.calls[0][0] == 1
    assert run_result(isolated_db) == (1, 2, 1, 1, 0, "success")
    assert isolated_db.query(
        "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id = ANY(%s)",
        ([first["event_id"], second["event_id"]],),
    ) == [(2,)]


def test_progress_write_failure_rolls_back_entire_page(isolated_db, partner, workers):
    """A SQL failure after payment inserts rolls back ledger, cursor and counters together."""
    events = [feed_event(1), feed_event(2)]
    partner.handler = lambda cursor: page(events, 2)
    isolated_db.query(
        "ALTER TABLE sync_runs ADD CONSTRAINT reject_progress CHECK (events_applied = 0)"
    )
    assert workers().wait(timeout=15) == 0
    assert run_result(isolated_db) == (0, 0, 0, 0, 0, "failed")
    assert isolated_db.query("SELECT cursor_seq FROM sync_state") == [(0,)]
    assert isolated_db.query(
        "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id = ANY(%s)",
        ([e["event_id"] for e in events],),
    ) == [(0,)]
    isolated_db.query("ALTER TABLE sync_runs DROP CONSTRAINT reject_progress")
    assert workers().wait(timeout=15) == 0
    assert run_result(isolated_db) == (0, 2, 2, 2, 0, "success")


def test_invalid_resident_blocks_page_without_losing_payments(isolated_db, partner, workers):
    """A valid row preceding an unknown resident is rolled back and can be replayed."""
    first, second = feed_event(1), feed_event(2)
    second["resident_id"] = "unknown"
    partner.handler = lambda cursor: page([first, second], 2)
    assert workers().wait(timeout=15) == 0
    assert run_result(isolated_db) == (0, 0, 0, 0, 0, "failed")
    assert isolated_db.query(
        "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id=%s", (first["event_id"],)
    ) == [(0,)]
    second["resident_id"] = "res-101"
    assert workers().wait(timeout=15) == 0
    assert run_result(isolated_db)[-1] == "success"


def test_worker_crash_rolls_back_and_recovers_abandoned_run(isolated_db, partner, workers):
    """Kill the process mid-page while its second INSERT is blocked by a row lock."""
    first, second = feed_event(1), feed_event(2)
    partner.handler = lambda cursor: page([first, second], 2)
    blocker = psycopg2.connect(isolated_db.dsn)
    process = None
    try:
        # An uncommitted competing insert blocks the worker's second event only.
        with blocker.cursor() as cur:
            cur.execute(
                "INSERT INTO resident_ledger (property_id,resident_id,entry_type,amount_cents,bigpms_event_id,occurred_at) VALUES ('prop-001','res-101','payment',1234,%s,now())",
                (second["event_id"],),
            )
        process = workers()
        wait_for(
            lambda: isolated_db.query(
                "SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%%INSERT INTO resident_ledger%%'"
            )
        )
        process.kill()
        process.wait(timeout=10)
    finally:
        blocker.rollback()
        blocker.close()
    wait_for(
        lambda: (
            not isolated_db.query(
                "SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND query LIKE '%%INSERT INTO resident_ledger%%' AND state='active' AND pid<>pg_backend_pid()"
            )
        )
    )
    assert isolated_db.query("SELECT cursor_seq FROM sync_state") == [(0,)]
    assert isolated_db.query(
        "SELECT count(*) FROM resident_ledger WHERE bigpms_event_id = ANY(%s)",
        ([first["event_id"], second["event_id"]],),
    ) == [(0,)]
    assert run_result(isolated_db)[-1] == "running"
    assert workers().wait(timeout=15) == 0
    assert run_result(isolated_db) == (0, 2, 2, 2, 0, "success")
    assert isolated_db.query("SELECT status,error FROM sync_runs ORDER BY id LIMIT 1")[0] == (
        "failed",
        "Interrupted before completion; recovered by next lock owner",
    )


def test_multiple_workers_share_one_feed_owner(isolated_db, partner, workers):
    """A second process records overlap without fetching or advancing the active run."""
    entered, release = threading.Event(), threading.Event()

    def respond(cursor):
        """Hold the first owner's HTTP request until the contender has finished."""
        entered.set()
        release.wait(timeout=10)
        return page([], cursor)

    partner.handler = respond
    first = workers()
    try:
        assert entered.wait(timeout=10)
        assert workers().wait(timeout=10) == 0
        assert len(partner.calls) == 1
        assert run_result(isolated_db)[-1] == "failed"
        assert isolated_db.query("SELECT last_status FROM sync_state") == [("running",)]
    finally:
        release.set()
    assert first.wait(timeout=15) == 0
    assert isolated_db.query("SELECT status FROM sync_runs ORDER BY id") == [
        ("success",),
        ("failed",),
    ]


@pytest.fixture
def webhook_app(isolated_db, partner):
    """Run the real webhook API on the same isolated database as the sync worker."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ | {
        "DATABASE_URL": isolated_db.dsn,
        "BIGPMS_API_URL": partner.url,
        "SYNC_INTERVAL_SECONDS": "3600",
        "PYTHONPATH": str(ROOT / "app"),
        "WEBHOOK_SECRET": "leaseflow-dev-secret",
    }
    with (Path("/artifacts") / f"webhook-race-{uuid4().hex}.log").open("w") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}"
        try:

            def ready():
                """Check API health without masking an early process exit."""
                assert process.poll() is None
                try:
                    return requests.get(url + "/api/health", timeout=1).status_code == 200
                except requests.ConnectionError:
                    return False

            wait_for(ready)
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_webhook_and_feed_concurrency(isolated_db, partner, workers, webhook_app, sign):
    """Concurrent real HTTP webhooks and a feed worker count one shared event once."""
    event = feed_event(1)
    partner.handler = lambda cursor: page([event, event], 1)
    body = json.dumps(
        {
            "event_id": event["event_id"],
            "event_type": event["event_type"],
            "occurred_at": event["occurred_at"],
            "data": {"resident_id": event["resident_id"], "amount_cents": event["amount_cents"]},
        }
    ).encode()
    # Block all contenders at the unique index, then let PostgreSQL choose a winner.
    blocker = psycopg2.connect(isolated_db.dsn)
    with blocker.cursor() as cur:
        cur.execute(
            "INSERT INTO resident_ledger (property_id,resident_id,entry_type,amount_cents,bigpms_event_id,occurred_at) VALUES ('prop-001','res-101','payment',1234,%s,now())",
            (event["event_id"],),
        )

    def deliver(_):
        """Send a signed request to the real API while feed ingestion is active."""
        return requests.post(
            webhook_app + "/webhooks/bigpms",
            data=body,
            headers={"X-BigPMS-Signature": sign(body)},
            timeout=20,
        )

    try:
        worker = workers()
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(deliver, i) for i in range(4)]
            try:
                wait_for(
                    lambda: (
                        isolated_db.query(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%%INSERT INTO resident_ledger%%'"
                        )[0][0]
                        >= 5
                    )
                )
            finally:
                blocker.rollback()
            responses = [f.result() for f in futures]
        assert all(r.status_code == 200 for r in responses)
        assert worker.wait(timeout=15) == 0
    finally:
        blocker.close()
    assert isolated_db.query(
        "SELECT count(*),sum(amount_cents) FROM resident_ledger WHERE bigpms_event_id=%s",
        (event["event_id"],),
    ) == [(1, 1234)]
    assert run_result(isolated_db)[-1] == "success"
    assert isolated_db.query(
        "SELECT count(*) FROM webhook_events WHERE event_id=%s", (event["event_id"],)
    ) == [(1,)]
