"""LeaseFlow integration API and scheduler.

Requirements 1 and 2 are implemented: authenticated webhooks and durable feed
sync. Reconciliation (requirement 3) remains a TODO; see docs/SPEC.md and README.
"""

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from auth import auth
from fastapi import FastAPI, Request, Response
from orm import ensure_payment_events_uniqueness, get_health_check_status, get_residents
from starlette.concurrency import run_in_threadpool
from sync import run_feed_sync
from webhooks import process_webhook, register_subscription

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


log = logging.getLogger("leaseflow")

BIGPMS_API_URL = os.environ.get("BIGPMS_API_URL", "http://localhost:3001")
CALLBACK_URL = os.environ.get("CALLBACK_URL", "http://localhost:8000/webhooks/bigpms")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "leaseflow-dev-secret")
SYNC_INTERVAL_SECONDS = int(os.environ.get("SYNC_INTERVAL_SECONDS", "30"))
SYNC_PAGE_SIZE = int(os.environ.get("SYNC_PAGE_SIZE", "10"))

app = FastAPI(title="LeaseFlow Integration Service")


# ---------------------------------------------------------------------------
# Provided endpoints (working — no changes needed)
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health():
    """Expose application health only after PostgreSQL responds successfully."""
    return get_health_check_status()


@app.get("/api/v1/residents")
def list_residents():
    """Return seeded residents and unit numbers from the local database."""
    return get_residents()


# ---------------------------------------------------------------------------
# Requirement 1 — Webhook subscription + callback endpoint (see SPEC section 1)
#
# Register with BigPMS so it starts sending events:
#   POST {BIGPMS_API_URL}/api/v1/webhook-subscriptions
#   body: {"callback_url": CALLBACK_URL, "secret": WEBHOOK_SECRET}
# Registration is idempotent — safe to do on every startup.
# ---------------------------------------------------------------------------


@app.on_event("startup")
def register_webhook_subscription():
    """Install duplicate protection before registering the callback at startup.

    Any unhandled database or registration error aborts application startup.
    """
    ensure_payment_events_uniqueness()
    register_subscription(BIGPMS_API_URL, CALLBACK_URL, WEBHOOK_SECRET)


@app.post("/webhooks/bigpms")
async def bigpms_webhook(request: Request):
    """Authenticate the raw body before validating or applying any payment.

    Persistence runs in the thread pool because psycopg2 is synchronous.
    A successful response acknowledges both new payments and safe redeliveries.
    """
    raw_body = await request.body()
    await auth(raw_body, request.headers.get("X-BigPMS-Signature", ""), WEBHOOK_SECRET)
    return await run_in_threadpool(process_webhook, raw_body)


# ---------------------------------------------------------------------------
# Requirement 2 — Scheduled feed sync (see SPEC section 2)
# ---------------------------------------------------------------------------


def sync_bigpms_ledger_events():
    """Backfill partner payments from the last durable cursor on each interval.

    The sync service records failures and preserves committed pages for the next
    scheduled attempt. A database advisory lock prevents overlapping workers.
    """
    run_feed_sync(BIGPMS_API_URL, SYNC_PAGE_SIZE)


scheduler = BackgroundScheduler()
scheduler.add_job(
    sync_bigpms_ledger_events,
    "interval",
    seconds=SYNC_INTERVAL_SECONDS,
    id="bigpms_ledger_events_sync",
    max_instances=1,
)


@app.on_event("startup")
def start_scheduler():
    """Start the provided interval scheduler after subscription setup succeeds."""
    scheduler.start()
    log.info("Scheduler started (sync every %ss)", SYNC_INTERVAL_SECONDS)


@app.on_event("shutdown")
def stop_scheduler():
    """Stop scheduling new runs without waiting for an already running job."""
    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# TODO 3 — Reconciliation report (see SPEC section 3)
# ---------------------------------------------------------------------------


@app.get("/api/v1/reconciliation")
def reconciliation_report():
    """Return HTTP 501 until requirement 3 is implemented."""
    # TODO: implement — see docs/SPEC.md
    return Response(status_code=501, content="Not implemented")
