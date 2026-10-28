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

## Requirement 1: webhook implementation

The app creates a unique index on `resident_ledger.bigpms_event_id` before
registering its callback with BigPMS. Registration has a 5-second request timeout
and up to three attempts; exhausted retries fail startup instead of leaving an
apparently healthy service that receives no webhooks.

`app/webhooks.py` verifies HMAC-SHA256 over the original request bytes with a
constant-time comparison, then validates the fields used by the ledger. Amounts
must be positive integer cents and timestamps must include a timezone. The
property is resolved through the resident's unit, rather than hardcoded.

The payment insert and successful webhook audit share one transaction. A unique
index and `ON CONFLICT DO NOTHING` make duplicate deliveries safe under
concurrency, including events already imported through another channel. The
future feed sync should use the same conflict target. Adding this index is
permitted by SPEC's schema-extension rule; the supplied SQL files are unchanged.

The supplied audit table has one row per event ID. Redelivery updates that row to
`duplicate` while preserving its original payload. Rejected deliveries use a
separate generated receipt ID and store the raw body, so an invalid signature
cannot reserve or overwrite a legitimate event ID. Transaction failures roll back
before a separate failed receipt is attempted. If PostgreSQL itself is unavailable,
that receipt cannot be persisted and the failure is logged.

Responses: `200` for processed/duplicate payments, `401` for invalid or missing
signatures, `422` for invalid payloads or unknown residents, and `503` for storage
failures. Blocking database work runs in FastAPI's thread pool.

### Tests

With PostgreSQL running, install the test dependencies and run:

```bash
python -m pip install -r requirements-test.txt
# PowerShell: $env:TEST_DATABASE_URL = "postgres://leaseflow:leaseflow@localhost:5432/leaseflow"
export TEST_DATABASE_URL=postgres://leaseflow:leaseflow@localhost:5432/leaseflow
python -m pytest -q
```

Tests create and remove an isolated schema; existing application tables are not
modified. They cover signatures, payload validation, duplicates, eight concurrent
deliveries, rollback after an audit-write failure, database unavailability, and
subscription registration/retries. GitHub Actions runs them against PostgreSQL 16.

AI usage for this feature: ChatGPT/Codex generated the implementation, tests, and
these notes. Candidate review and explanation of the decisions are still required.
Requirements 2 and 3 remain the starter stubs.
