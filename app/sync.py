"""Fetch and validate BigPMS pages, retry transient failures and coordinate persistence."""

import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from dto import FeedPage
from sync_store import apply_feed_page, begin_sync_run, finish_sync_run, sync_session

log = logging.getLogger("leaseflow.sync")
MAX_ATTEMPTS = 4


def retry_delay(value: str | None, fallback: float) -> float:
    """Interpret Retry-After as seconds or an HTTP date, falling back if malformed.

    Past dates mean no additional delay. Never shorten a valid partner-requested
    wait; retry count is bounded, but a long Retry-After can extend a run.
    """
    if value is not None:
        if value.strip().isdigit():
            return float(value.strip())
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is not None:
                return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return fallback


def fetch_feed_page(api_url: str, cursor: int, limit: int) -> FeedPage:
    """Fetch one page with four attempts for network errors, 429 and server errors.

    Honor Retry-After before retrying 429; otherwise wait 1, 2 and 4 seconds.
    Permanent HTTP errors and invalid successful payloads fail immediately.
    Requests and waits run outside database transactions. Raise the last error
    when retries are exhausted; never invent a replacement page or cursor.
    """
    for attempt in range(MAX_ATTEMPTS):
        delay = float(2**attempt)
        rate_limited = False
        try:
            response = requests.get(
                f"{api_url.rstrip('/')}/api/v1/ledger-events",
                params={"cursor": cursor, "limit": limit},
                timeout=(3, 10),
            )
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if response.status_code == 429:
                    rate_limited = True
                    delay = retry_delay(response.headers.get("Retry-After"), delay)
                response.raise_for_status()
            else:
                response.raise_for_status()
                page = FeedPage.model_validate(response.json())
                page.check_progress(cursor)
                return page
        except requests.HTTPError as error:
            code = error.response.status_code
            if code != 429 and not 500 <= code < 600:
                raise
            failure = error
        except (requests.ConnectionError, requests.Timeout) as error:
            failure = error
        if attempt == MAX_ATTEMPTS - 1:
            if rate_limited:
                # Retain ownership through the cooldown so the next scheduled run
                # cannot bypass the final response's Retry-After.
                log.warning(
                    "Feed exhausted rate-limit retries cursor=%s cooldown=%ss", cursor, delay
                )
                time.sleep(delay)
            raise failure
        log.warning(
            "Feed retry cursor=%s attempt=%s/%s delay=%ss error=%s",
            cursor,
            attempt + 1,
            MAX_ATTEMPTS,
            delay,
            failure,
        )
        time.sleep(delay)
    raise RuntimeError("Unreachable feed retry state")


def run_feed_sync(api_url: str, page_size: int = 10) -> None:
    """Drain new feed pages under one database lock and retain durable progress.

    Record every acquired run, including empty runs. Audit overlap as a failed
    attempt without touching the active owner's state. A page failure leaves the
    previous committed cursor intact; failed-run auditing never overwrites it.
    Unexpected failures are logged at this scheduler boundary so the next interval
    can recover. If the database cannot be reached, SQL auditing is impossible.
    """
    run_id = None
    try:
        with sync_session() as conn:
            if conn is None:
                log.info("Feed sync skipped: another process owns the lock")
                return
            run_id, cursor = begin_sync_run(conn)
            try:
                if not 1 <= page_size <= 50:
                    raise ValueError("SYNC_PAGE_SIZE must be between 1 and 50")
                log.info("Feed sync started run=%s cursor=%s", run_id, cursor)
                while True:
                    page = fetch_feed_page(api_url, cursor, page_size)
                    applied, skipped = apply_feed_page(conn, run_id, cursor, page)
                    cursor = page.next_cursor
                    log.info(
                        "Feed page committed run=%s cursor=%s fetched=%s applied=%s skipped=%s",
                        run_id,
                        cursor,
                        len(page.events),
                        applied,
                        skipped,
                    )
                    if not page.has_more:
                        break
                finish_sync_run(run_id, "success")
                log.info("Feed sync succeeded run=%s cursor=%s", run_id, cursor)
            except Exception as error:
                log.exception("Feed sync failed run=%s; committed cursor is preserved", run_id)
                finish_sync_run(run_id, "failed", str(error)[:2000])
    except Exception:
        log.exception("Feed sync could not complete database bookkeeping run=%s", run_id)
