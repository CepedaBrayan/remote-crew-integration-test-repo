"""Feed contracts and failure paths independent of HTTP, PostgreSQL and wall time."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, MagicMock

import psycopg2
import pytest
import requests


@pytest.fixture
def feed_event(payment):
    """Represent the same payment in the flat partner feed format."""
    return {k: v for k, v in payment.items() if k != "data"} | payment["data"] | {"seq": 1}


@pytest.fixture
def page_data(feed_event):
    """Supply a valid one-event terminal page."""
    return {"events": [feed_event], "next_cursor": 1, "has_more": False}


def response(code=200, data=None, headers=None):
    """Build a Requests-compatible response with authentic HTTP error behavior."""
    result = requests.Response()
    result.status_code = code
    result.url = "http://partner/api/v1/ledger-events"
    result.headers.update(headers or {})
    result.json = Mock(return_value=data)
    return result


@pytest.mark.parametrize("value,expected", [(None, 2), ("bad", 2), ("-1", 2), (" 3 ", 3), ("0", 0)])
def test_retry_after_seconds_and_fallback(modules, value, expected):
    """Valid delta seconds override backoff; malformed headers keep the fallback."""
    assert modules.sync.retry_delay(value, 2) == expected


def test_retry_after_http_dates(modules):
    """HTTP dates wait until the requested time and past dates do not add delay."""
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20))
    assert 18 <= modules.sync.retry_delay(future, 2) <= 20
    assert modules.sync.retry_delay("Wed, 21 Oct 2015 07:28:00 GMT", 2) == 0


def test_fetch_retries_same_cursor_and_honors_rate_limit(modules, monkeypatch, page_data):
    """500 and 429 retries retain the requested cursor and honor Retry-After."""
    get = Mock(
        side_effect=[
            response(500),
            response(429, headers={"Retry-After": "7"}),
            response(data=page_data),
        ]
    )
    sleep = Mock()
    monkeypatch.setattr(modules.sync.requests, "get", get)
    monkeypatch.setattr(modules.sync.time, "sleep", sleep)
    assert modules.sync.fetch_feed_page("http://partner/", 0, 10).next_cursor == 1
    assert [call.args[0] for call in sleep.call_args_list] == [1, 7]
    for call in get.call_args_list:
        assert call.args == ("http://partner/api/v1/ledger-events",)
        assert call.kwargs == {"params": {"cursor": 0, "limit": 10}, "timeout": (3, 10)}


@pytest.mark.parametrize(
    "error", [requests.ConnectionError("offline"), requests.Timeout("timeout")]
)
def test_network_retry_exhaustion(modules, monkeypatch, error):
    """Four failed network attempts propagate the final error after three waits."""
    get, sleep = Mock(side_effect=error), Mock()
    monkeypatch.setattr(modules.sync.requests, "get", get)
    monkeypatch.setattr(modules.sync.time, "sleep", sleep)
    with pytest.raises(type(error)):
        modules.sync.fetch_feed_page("http://partner", 0, 10)
    assert get.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4]


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_permanent_http_errors_are_not_retried(modules, monkeypatch, code):
    """Client/configuration errors fail immediately instead of hiding behind retries."""
    get, sleep = Mock(return_value=response(code)), Mock()
    monkeypatch.setattr(modules.sync.requests, "get", get)
    monkeypatch.setattr(modules.sync.time, "sleep", sleep)
    with pytest.raises(requests.HTTPError):
        modules.sync.fetch_feed_page("http://partner", 0, 10)
    get.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "change",
    ["missing", "bad_amount", "bad_seq", "cursor_ahead", "cursor_behind", "empty_more", "old_seq"],
)
def test_bad_pages_cannot_advance_progress(modules, monkeypatch, page_data, change):
    """Reject malformed events and unsafe paging metadata before any persistence."""
    if change == "missing":
        del page_data["next_cursor"]
    elif change == "bad_amount":
        page_data["events"][0]["amount_cents"] = -1
    elif change == "bad_seq":
        page_data["events"][0]["seq"] = True
    elif change == "cursor_ahead":
        page_data["next_cursor"] = 2
    elif change == "cursor_behind":
        page_data["next_cursor"] = 0
    elif change == "empty_more":
        page_data.update(events=[], next_cursor=0, has_more=True)
    get = Mock(return_value=response(data=page_data))
    monkeypatch.setattr(modules.sync.requests, "get", get)
    with pytest.raises(ValueError):
        modules.sync.fetch_feed_page("http://partner", 1 if change == "old_seq" else 0, 10)
    get.assert_called_once()


def test_feed_normalization_and_duplicates(modules, page_data):
    """Flat feed payments normalize to the shared DTO; repeated rows remain countable."""
    page_data["events"] *= 2
    page = modules.dto.FeedPage.model_validate(page_data)
    page.check_progress(0)
    payment = page.events[0].as_payment()
    assert payment.data.resident_id == "res-101"
    assert payment.data.amount_cents == 150000
    assert len(page.events) == 2
    empty = modules.dto.FeedPage(events=[], next_cursor=10, has_more=False)
    empty.check_progress(10)


@pytest.fixture
def sync_dependencies(modules, monkeypatch):
    """Expose mocked storage boundaries while exercising actual sync orchestration."""
    conn = Mock()

    @contextmanager
    def session():
        """Yield ownership without opening PostgreSQL."""
        yield conn

    monkeypatch.setattr(modules.sync, "sync_session", session)
    mocks = {
        "begin_sync_run": Mock(return_value=(7, 0)),
        "apply_feed_page": Mock(return_value=(1, 0)),
        "finish_sync_run": Mock(),
        "fetch_feed_page": Mock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(modules.sync, name, mock)
    return conn, mocks


def test_sync_pages_from_durable_cursor(modules, sync_dependencies, page_data):
    """Use returned cursors and record success only after all pages commit."""
    conn, mocks = sync_dependencies
    first = modules.dto.FeedPage.model_validate(page_data | {"has_more": True})
    last = modules.dto.FeedPage(events=[], next_cursor=1, has_more=False)
    mocks["fetch_feed_page"].side_effect = [first, last]
    modules.sync.run_feed_sync("http://partner", 5)
    assert [c.args[1] for c in mocks["fetch_feed_page"].call_args_list] == [0, 1]
    assert [c.args[:3] for c in mocks["apply_feed_page"].call_args_list] == [
        (conn, 7, 0),
        (conn, 7, 1),
    ]
    mocks["finish_sync_run"].assert_called_once_with(7, "success")


def test_failed_page_does_not_report_success(modules, sync_dependencies, page_data):
    """A failed transaction stops paging and records failure without inventing progress."""
    _, mocks = sync_dependencies
    mocks["fetch_feed_page"].return_value = modules.dto.FeedPage.model_validate(page_data)
    mocks["apply_feed_page"].side_effect = psycopg2.IntegrityError("forced failure")
    modules.sync.run_feed_sync("http://partner")
    mocks["finish_sync_run"].assert_called_once_with(7, "failed", "forced failure")
    mocks["fetch_feed_page"].assert_called_once()


def test_bookkeeping_outage_is_logged(modules, sync_dependencies, caplog):
    """The scheduler boundary survives failure to persist a failed-run record."""
    _, mocks = sync_dependencies
    mocks["fetch_feed_page"].side_effect = requests.ConnectionError("offline")
    mocks["finish_sync_run"].side_effect = psycopg2.OperationalError("database offline")
    modules.sync.run_feed_sync("http://partner")
    assert "could not complete database bookkeeping" in caplog.text


def test_overlap_never_fetches(modules, sync_dependencies, monkeypatch):
    """A non-owner leaves both the partner and active run untouched."""
    _, mocks = sync_dependencies

    @contextmanager
    def occupied():
        """Represent a lock already owned by another worker."""
        yield None

    monkeypatch.setattr(modules.sync, "sync_session", occupied)
    modules.sync.run_feed_sync("http://partner")
    mocks["begin_sync_run"].assert_not_called()
    mocks["fetch_feed_page"].assert_not_called()


@pytest.mark.parametrize("size", [0, 51])
def test_invalid_page_size_is_audited(modules, sync_dependencies, size):
    """Out-of-contract limits fail the recorded run without sending a request."""
    _, mocks = sync_dependencies
    modules.sync.run_feed_sync("http://partner", size)
    mocks["fetch_feed_page"].assert_not_called()
    assert mocks["finish_sync_run"].call_args.args[1] == "failed"


def test_scheduler_passes_configuration(modules, monkeypatch):
    """The provided scheduled function invokes the implemented sync service."""
    run = Mock()
    monkeypatch.setattr(modules.main, "run_feed_sync", run)
    modules.main.sync_bigpms_ledger_events()
    run.assert_called_once_with(modules.main.BIGPMS_API_URL, modules.main.SYNC_PAGE_SIZE)


@pytest.fixture
def store_db(modules, monkeypatch):
    """Provide a transaction-aware mock for the dedicated sync persistence module."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    release = Mock()
    monkeypatch.setattr(modules.orm, "get_conn", Mock(return_value=conn))
    monkeypatch.setattr(modules.orm, "put_conn", release)
    return conn, cur, release


@pytest.mark.parametrize("owned", [True, False])
def test_session_releases_or_records_overlap(modules, store_db, owned):
    """Session locks are released; rejected ownership attempts get a failed run row."""
    conn, cur, release = store_db
    cur.fetchone.return_value = (owned,)
    with modules.orm.sync_session() as result:
        assert result is (conn if owned else None)
    sql = " ".join(c.args[0] for c in cur.execute.call_args_list)
    assert ("pg_advisory_unlock" in sql) is owned
    assert ("INSERT INTO sync_runs" in sql) is not owned
    release.assert_called_once_with(conn)


def test_unlock_failure_discards_session(modules, store_db):
    """A failed unlock cannot return a session that still owns a lock to the pool."""
    conn, cur, release = store_db
    cur.fetchone.return_value = (True,)
    cur.execute.side_effect = [None, psycopg2.OperationalError("lost connection")]
    with modules.orm.sync_session():
        pass
    conn.close.assert_called_once()
    release.assert_called_once_with(conn)


def test_begin_recovers_abandoned_runs(modules, store_db):
    """A new lock owner starts from persisted state and marks old running rows failed."""
    conn, cur, _ = store_db
    cur.fetchone.side_effect = [(12,), (42,)]
    assert modules.orm.begin_sync_run(conn) == (42, 12)
    assert "Interrupted" in cur.execute.call_args_list[1].args[0]
    assert conn.__exit__.call_args.args == (None, None, None)


def test_missing_sync_state_fails(modules, store_db):
    """Missing seed state is an error, never an implicit cursor reset to zero."""
    conn, cur, _ = store_db
    cur.fetchone.return_value = None
    with pytest.raises(RuntimeError, match="Missing seeded"):
        modules.orm.begin_sync_run(conn)


def test_page_writes_share_transaction(modules, store_db, page_data, monkeypatch):
    """Cursor and counters are written alongside all payment insertions."""
    conn, cur, _ = store_db
    cur.fetchone.return_value = (0,)
    page_data["events"] *= 2
    insert = Mock(side_effect=["processed", "duplicate"])
    monkeypatch.setattr(modules.orm, "insert_payment", insert)
    page = modules.dto.FeedPage.model_validate(page_data)
    assert modules.orm.apply_feed_page(conn, 7, 0, page) == (1, 1)
    assert all(call.args[0] is cur for call in insert.call_args_list)
    assert cur.execute.call_args.args[1] == (1, 2, 1, 1, 7)
    assert conn.__exit__.call_args.args == (None, None, None)


def test_failed_insert_stops_cursor_update(modules, store_db, page_data, monkeypatch):
    """A payment error reaches transaction rollback before progress writes happen."""
    conn, cur, _ = store_db
    cur.fetchone.return_value = (0,)
    monkeypatch.setattr(
        modules.orm, "insert_payment", Mock(side_effect=ValueError("Unknown resident"))
    )
    with pytest.raises(ValueError):
        modules.orm.apply_feed_page(
            conn, 7, 0, modules.dto.FeedPage.model_validate(page_data)
        )
    assert cur.execute.call_count == 1
    assert conn.__exit__.call_args.args[0] is ValueError


def test_finish_preserves_durable_counters(modules, store_db):
    """Finishing changes outcome only; it cannot overwrite the committed range or counts."""
    conn, cur, release = store_db
    modules.orm.finish_sync_run(7, "failed", "offline")
    assert cur.execute.call_args_list[0].args[1] == ("failed", "offline", 7)
    assert "cursor_end" not in cur.execute.call_args_list[0].args[0]
    release.assert_called_once_with(conn)


def test_old_owner_cannot_overwrite_recovered_status(modules, store_db):
    """Once a newer owner recovered the old run, late finalization leaves state alone."""
    _, cur, _ = store_db
    cur.rowcount = 0
    modules.orm.finish_sync_run(7, "failed", "connection lost")
    assert cur.execute.call_count == 1


def test_final_rate_limit_retains_cooldown(modules, monkeypatch):
    """Exhaustion must not let the next scheduled run bypass the final Retry-After."""
    get = Mock(return_value=response(429, headers={"Retry-After": "3"}))
    sleep = Mock()
    monkeypatch.setattr(modules.sync.requests, "get", get)
    monkeypatch.setattr(modules.sync.time, "sleep", sleep)
    with pytest.raises(requests.HTTPError):
        modules.sync.fetch_feed_page("http://partner", 0, 10)
    assert get.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [3, 3, 3, 3]
