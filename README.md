# LeaseFlow ↔ BigPMS Integration — Starter Repo

This is the starter repo for the Senior Integration Engineer take-home.

## Start Here

1. **Read [docs/SPEC.md](docs/SPEC.md)** — the full project brief. Everything you're asked to build is defined there.
2. **Boot the repo** (Quick Start below) and verify everything runs.
3. **Build.** Your workspace is `app/main.py` — it has three stubbed TODOs matching the SPEC's requirements:

| # | Task | Where | SPEC section |
|---|------|-------|--------------|
| 1 | Webhook subscription + callback endpoint | `register_webhook_subscription()` / `POST /webhooks/bigpms` | §1 |
| 2 | Scheduled feed sync | `sync_bigpms_ledger_events()` (scheduler already calls it) | §2 |
| 3 | Reconciliation report | `GET /api/v1/reconciliation` | §3 |
| 4 | Write-up (time spent, decisions, AI usage) | your README | §4 |

**Ground rules:** 2-hour cap (stop even if unfinished — log your time). AI tools expected — you'll defend every decision in the review. Don't modify the DB schema or the mock BigPMS service.

## Quick Start

Be sure that you have Docker installed locally. 

```bash
docker-compose up --build
```

This starts three services:

| Service      | URL                   | Description                                      |
|--------------|-----------------------|--------------------------------------------------|
| **app**      | http://localhost:8000 | LeaseFlow integration service (your workspace)   |
| **db**       | localhost:5432        | PostgreSQL (user/pass/db: leaseflow)             |
| **mock-bigpms** | http://localhost:3001 | Mock BigPMS — the partner platform (do not modify)  |

## Verify It Works

1. **Your service:** http://localhost:8000/api/health → `{"status": "ok"}`
2. **Residents:** http://localhost:8000/api/v1/residents → 8 seeded residents
3. **Mock BigPMS:** http://localhost:3001/health → `{"status": "ok", "webhook_subscriptions": 0}`
4. **BigPMS's feed:** http://localhost:3001/api/v1/ledger-events?cursor=0&limit=5 (retry if you hit a simulated error — that's intentional)

## Mock BigPMS API (the partner surface you integrate with)

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/webhook-subscriptions` | Register `{"callback_url", "secret"}` to start receiving webhooks. Idempotent per callback_url. |
| `GET /api/v1/webhook-subscriptions` | List registered subscriptions |
| `GET /api/v1/ledger-events?cursor=&limit=` | Paginated payment events feed (cursor = last seen `seq`, limit ≤ 50) |
| `GET /api/v1/residents/summaries` | Bulk authoritative payment totals per resident |
| `GET /api/v1/residents/{id}/summary` | Single-resident totals |
| `GET /health` | Health + subscription count |

**Expected partner behavior (deliberate — do not "fix"):**

- The feed randomly returns **500** (~10%) and **429 with `Retry-After`** (~10%)
- Feed pages occasionally contain a **duplicate event**
- Webhooks are sometimes **delivered twice**
- Some webhooks arrive with an **invalid signature** (reject them)
- Events delivered by webhook **also appear in the feed**
- BigPMS emits a new payment event every ~15s **once you've registered a subscription**

Signature scheme: `X-BigPMS-Signature` header = hex `HMAC-SHA256(secret, raw_request_body)`.

## Environment Variables (pre-configured in docker-compose.yml)

| Variable | Value |
|----------|-------|
| `DATABASE_URL` | `postgres://leaseflow:leaseflow@db:5432/leaseflow` |
| `BIGPMS_API_URL` | `http://mock-bigpms:3001` |
| `CALLBACK_URL` | `http://app:8000/webhooks/bigpms` (what you register with BigPMS) |
| `WEBHOOK_SECRET` | `leaseflow-dev-secret` |
| `SYNC_INTERVAL_SECONDS` | `30` |

## What's Provided

- **`db/init.sql`** — full schema: residents, leases, `resident_ledger`, `webhook_events`, `sync_state`, `sync_runs`. **Do not modify.**
- **`db/seed.sql`** — 8 residents, three months of rent charges, payment history through BigPMS event `evt-1012`. Contains planted data-quality issues (see SPEC).
- **`app/main.py`** — FastAPI scaffold: health check works, scheduler is wired to call `sync_bigpms_ledger_events()` every 30s, webhook/reconciliation endpoints are stubbed with TODOs.
- **`app/db.py`** — connection pool, ready to use.
- **`mock-bigpms/`** — the mock BigPMS service (runs as `mock-bigpms`). Black box.

## Useful Commands

```bash
# Watch the partner's behavior (webhook sends, simulated failures)
docker-compose logs -f mock-bigpms

# Watch your own service
docker-compose logs -f app

# Connect to your database
docker-compose exec db psql -U leaseflow -d leaseflow

# Tear down and reset all data
docker-compose down -v && docker-compose up --build
```

## Tips

- The app volume-mounts `./app`, and uvicorn runs with `--reload` — edits apply without rebuilding.
- `docker-compose down -v` resets you to the seeded state. Useful for testing your sync from scratch.
- Watch `mock-bigpms` logs while you work — it announces duplicates, bad signatures, and simulated failures as it produces them.
- If something is ambiguous, make a decision and document it in your README.

Good luck.

---

## Solution

Implemented webhook ingestion, scheduled feed synchronization, and the reconciliation report. The original schema files, seed data, and mock BigPMS service remain unchanged.

### Implementation

**1. Webhook ingestion**

- Registers the callback with BigPMS at startup, with bounded retries. Registration failure prevents application startup.
- Verifies the HMAC signature against the raw request body before processing the payment.
- Validates the payload and resolves the resident's property from local records.
- Writes the payment and its audit result in one transaction.
- Uses a unique index on `bigpms_event_id` to prevent duplicate payments across webhooks and feed imports, including concurrent deliveries.
- Returns `401` for invalid signatures, `422` for invalid payloads or unknown residents, and `503` for database failures. Rejected deliveries are audited when the database is available.

**2. Scheduled feed synchronization**

- Reads the persisted cursor from `sync_state` and follows the paginated feed.
- Commits each page's payments, cursor, and progress counters in one transaction. A failed page rolls back; previously committed pages remain available for the next run.
- Retries transient network errors, HTTP `500` responses, and rate limits with bounded backoff, honoring `Retry-After`.
- Uses a PostgreSQL advisory lock to prevent overlapping sync runs across application processes.
- Records each run in `sync_runs`, including its cursor range, counters, status, and error. Progress counters reflect committed pages.
- Reuses the payment insertion logic and database uniqueness guarantee used by webhooks.

**3. Reconciliation report**

- Aggregates local payment totals in SQL and retrieves authoritative totals through the bulk BigPMS summaries endpoint.
- Calculates `delta_cents = local_payments_cents - bigpms_payments_cents`:
  - Zero: `in_sync`.
  - Negative: `missing_local_payments`.
  - Positive: `local_exceeds_bigpms`.
- Detects duplicate charges and orphaned ledger entries through set-based SQL queries.
- Reads local totals and data-quality issues from a consistent, read-only database snapshot.
- Returns `503` when authoritative data cannot be retrieved or validated, rather than returning a misleading report.

`issues_found` counts duplicate-charge groups and orphaned entries only. Payment discrepancies are counted separately in `residents_out_of_sync`.

After the historical backfill, at a moment without pending deliveries, the seeded data should produce seven residents in sync, `res-107` with excess local payments, and two data-quality issues. Brief discrepancies are expected while new BigPMS payments are still being delivered.

### Code Organization

| Module | Responsibility |
|--------|----------------|
| `app/main.py` | HTTP routes, configuration, and application lifecycle |
| `app/auth.py` | Webhook signature verification |
| `app/dto.py` | Payload validation models |
| `app/webhooks.py` | Subscription registration and webhook processing |
| `app/sync.py` | Feed requests, retries, and synchronization orchestration |
| `app/reconciliation.py` | Authoritative totals retrieval and report assembly |
| `app/orm.py` | All application SQL and database transactions |
| `app/db.py` | Connection pool management |
| `app/utils.py` | Failure-auditing utilities |

Despite its name, `orm.py` uses explicit SQL through psycopg2; no ORM dependency was added. Validation stays outside the persistence layer.

`AGENTS.md` and `SKILL.md` document repository conventions, including SQL placement, function documentation, and testing expectations.

### Development and Tests

Local tooling uses Python 3.12 and a virtual environment. Docker Compose is required for the application and E2E tests; Make is required for the commands below.

Create the virtual environment from the repository root:

```bash
# macOS / Linux
python3.12 -m venv .venv
```

```powershell
# Windows
py -3.12 -m venv .venv
```

Then install development and test dependencies:

```bash
make install-dev
```

The Makefile uses the virtual environment's Python executable directly, so activation is optional.

| Command | Purpose |
|---------|---------|
| `make up` | Build and start the development services |
| `make logs-app` | Follow application logs |
| `make format` | Apply automatic lint fixes and format `app/` |
| `make check` | Check lint and formatting without changing files |
| `make test-unit` | Run all unit tests |
| `make test-e2e` | Run the isolated E2E suite |
| `make test-all` | Run unit and E2E tests |
| `make help` | List available commands |

**Unit tests** cover validation, authentication, registration retries, payment processing, sync behavior, reconciliation, and error handling. External services and database interactions are mocked. An architecture check also enforces that application SQL remains in `app/orm.py`.

**E2E tests** exercise HTTP endpoints and PostgreSQL transactions, including duplicate delivery, webhook/feed concurrency, cursor recovery, partner failures, and reconciliation of the planted issues. Controlled partner fixtures make failure scenarios reproducible.

`make test-e2e` runs `scripts/run_e2e.py`, which:

1. Builds an isolated Compose environment and waits for PostgreSQL, BigPMS, and the application to become healthy.
2. Starts a temporary test container to execute the E2E suite.
3. Verifies separately that an unreachable partner causes application startup to fail.
4. Collects reports and logs under `artifacts/` and removes the test environment.

The E2E environment does not share the development database volume.

**GitHub Actions** runs lint, formatting, unit tests, and E2E tests for pull requests targeting `main` or `dev`, and pushes to the configured development branches. Test reports and diagnostic logs are uploaded as workflow artifacts.

### Time Spent

Approximately **4 hours**, including implementation, refactoring, tests, and documentation. This exceeded the requested two-hour cap.

### Decisions and Tradeoffs

- **AI-assisted implementation with unit and E2E tests:** Given the scope and limited time, I used AI to accelerate implementation and test coverage. Tests provided checks for retries, concurrency, and transaction boundaries, while architectural decisions still required review.
- **Keep the existing stack:** Explicit SQL and psycopg2 were sufficient for the required queries and transaction control. Adding an ORM or another persistence framework would have increased setup and review overhead.
- **Enforce idempotency in PostgreSQL:** A unique event index protects against concurrent webhook and feed writes. An application-level existence check alone would leave a race condition.
- **Commit sync progress per page:** This keeps transactions bounded and preserves completed work after a failure. Invalid events block their page rather than allowing the cursor to silently skip unapplied payments.
- **Work within the supplied audit schema:** Webhook audit rows represent the latest result per event, not a complete delivery history. Failure auditing is best-effort when the database itself is unavailable.

### What I Would Do Next

- **Add operational monitoring and alerts:** Track sync lag, repeated failures, rejected webhooks, and sustained reconciliation discrepancies so integration problems are detected before they affect residents.
- **Add durable failure capture and controlled replay:** Preserve rejected events for investigation and replay after correction. Introduce a retry queue or dead-letter queue if delivery volume and operational requirements justify it.

### AI Usage

Approximately **80% of the code was AI-generated**, including initial feature implementations, tests, CI configuration, and documentation. This is an estimate, not a measured line count.

My main manual refactoring focused on the first PR: separating SQL into `orm.py`, authentication into `auth.py`, and supporting utilities into `utils.py`, while keeping validation outside persistence. I also guided the repository instructions in `AGENTS.md` and `SKILL.md` to establish a consistent structure for subsequent changes.

Much of the subsequent implementation was retained as generated, including database-backed idempotency, transactional cursor advancement, and SQL reconciliation, with targeted corrections during review and testing. One concrete correction was replacing an unavailable pytest version pin with `8.4.2` after dependency installation failed.

The unit and E2E suites support these decisions, but do not replace understanding or reviewing the implementation.