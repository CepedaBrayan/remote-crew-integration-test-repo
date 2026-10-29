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
## Solution — Requirement 1: webhook subscription and processing

This first PR implements subscription registration and signed payment processing.
The scheduled feed sync and reconciliation report remain unimplemented; this PR
does not claim to complete requirements 2 or 3.

### Implementation and module responsibilities

| Module | Responsibility |
|---|---|
| `app/main.py` | Wire startup hooks and HTTP routes; authenticate before processing. |
| `app/auth.py` | Verify HMAC-SHA256 against the exact request bytes with a constant-time comparison. |
| `app/dto.py` | Validate payment identity, type, positive integer cents and timezone-aware timestamps. Preserve extra partner fields. |
| `app/webhooks.py` | Register the subscription, validate authenticated payloads and map processing failures to HTTP responses. |
| `app/orm.py` | Execute SQL through psycopg2: index setup, atomic payment/audit writes, failed receipts and read queries. Despite the filename, no ORM framework is used. |
| `app/utils.py` | Record failed deliveries with independent UUIDs, logging storage failures without replacing the intended HTTP error. |

Startup first creates a unique index on `resident_ledger.bigpms_event_id`, then
registers `CALLBACK_URL` and `WEBHOOK_SECRET` with BigPMS. Registration uses a
five-second Requests timeout and at most three attempts, with one- and two-second
waits. Exhausted failures abort startup. Successful registration means BigPMS
accepted the URL; it does not prove that future callback deliveries will succeed.

For a valid webhook, the app resolves the resident's property through its unit.
The payment and successful audit record commit in one transaction. A unique
index plus `ON CONFLICT DO NOTHING` prevents double counting, including concurrent
deliveries and events already imported through another channel. Future feed sync
must respect the same ledger uniqueness rule. SPEC permits added indexes run from
the app; the supplied `db/init.sql`, `db/seed.sql`, and mock source are unchanged.

The audit schema allows one row per valid event ID. A redelivery updates its
status to `duplicate` and preserves its first stored payload. That payload is the
validated model serialized to JSON: extra fields are retained, but typed values
such as dates may be normalized. It is not a byte-for-byte copy of the request.
Rejected deliveries receive generated receipt IDs so an untrusted partner ID
cannot overwrite a legitimate audit row. On transaction failure, the rollback
happens before the separate failed receipt is attempted. Auditing is best effort
when PostgreSQL is unavailable; the failure is logged instead.

| HTTP status | Outcome |
|---|---|
| `200` | Payment processed, or duplicate acknowledged without another ledger row. |
| `401` | Missing or invalid signature. |
| `422` | Invalid JSON/model or unknown resident. |
| `503` | Database operation failed; the payment transaction did not complete successfully. |

Functions document their contracts, side effects, failure behavior and transaction
boundaries. Validation stays in the webhook layer; persistence accepts a single
validated `PaymentEvent`. No broker, queue, new ORM or migration framework was added.

### Local tooling and unit tests

Use Python 3.12 and create the local environment once:

```bash
python -m venv .venv
make install-dev
make check
make test
```

On Windows, `py -3.12 -m venv .venv` can be used to select Python 3.12 explicitly.
The Makefile selects `.venv/Scripts/python.exe` on Windows and `.venv/bin/python`
on other systems. To use an already selected interpreter, pass `PYTHON=python`.
`requirements.txt` pins Ruff; `requirements-test.txt` adds the app and test dependencies.

`make test` and `make test-unit` run every test under `tests/unit` without Docker,
PostgreSQL, environment variables or network access. The eager connection-pool
constructor is mocked during unit imports. Tests cover authentication and exact
body bytes, DTO validation, subscription retries, error-to-HTTP mapping, audit
receipts, SQL parameters, connection release, startup ordering, route behavior,
and scheduler lifecycle. Mocked transaction tests check control flow; only E2E
tests establish actual PostgreSQL rollback and concurrency behavior.

`make format` fixes supported lint issues and formats only `app/`. `make check`
checks app lint and formatting without changing files. Plain `python -m pytest`
defaults to unit tests; E2E tests require the dedicated runner below.

### End-to-end tests locally and in GitHub Actions

With Docker running and Docker Compose v2 supporting `up --wait`:

```bash
make test-e2e
# Or run both suites in sequence:
make test-all
```

The runner (`scripts/run_e2e.py`) uses `compose.e2e.yml` and a unique project name
for each run. It builds the real app and untouched mock, starts PostgreSQL 16 with
the original schema/seed, and waits for health checks. A fourth, temporary test
container sends real HTTP requests and queries real PostgreSQL; it is test
infrastructure, not an additional application service. No host ports are published,
and no development data volumes or source bind mounts are reused.

E2E checks verify:

- Startup subscription and an actual automatically generated mock payment reaching the ledger.
- Valid payment/audit persistence and duplicate acknowledgements.
- Eight simultaneous HTTP deliveries resulting in exactly one payment.
- Deduplication against seeded payments with no previous webhook audit.
- Missing/invalid signatures and rejected receipts that do not poison valid event IDs.
- Invalid amounts, unknown residents and malformed JSON leaving no payment behind.
- A deliberately forced audit-write failure rolling back the preceding payment INSERT, followed by a successful retry.
- A separate app process pointing to an unreachable partner port exhausting registration retries and exiting with an error.

The failure injection adds a temporary constraint only to the disposable test
database and removes it in a `finally` block. The mock emits every second in this
test configuration; checks use unique event IDs and bounded polling instead of
assuming random duplicate/bad-signature deliveries will occur.

The E2E app runs without `--reload` so failed startup is visible as a process exit.
The development Dockerfile still uses reload; its supervisor may remain running
after the app worker fails. `Running` alone therefore does not prove API health.

The runner cleans up its own containers, network and database volumes on ordinary
success/failure. Reports and logs remain in ignored `artifacts/`:
`e2e.xml`, `compose.log`, and `startup-failure.log` when that check is reached.
Abruptly killing the runner can prevent cleanup; its generated project name is
shown in Docker output. Development containers are not removed.

GitHub Actions runs two independent jobs on pushes to `features`/`main` and pull
requests targeting `main`: unit tests plus app lint, and the same Docker E2E command
used locally. The E2E job uploads diagnostics even when tests fail. It has a bounded
job timeout so a broken startup cannot hang indefinitely.

### Decisions, limitations and AI usage

The unique index is a small, idempotent startup change rather than a full migration
framework. The supplied audit schema records the latest status per valid event,
not a complete history of every duplicate delivery. Registration retries are
bounded but currently also retry permanent configuration/HTTP errors. A successful
registration does not validate callback reachability. These are documented tradeoffs,
not guarantees of complete delivery; requirement 2 provides the missing backfill path.

ChatGPT/Codex generated the initial requirement-1 implementation. The candidate
subsequently separated authentication, DTOs, SQL operations and audit utilities,
kept validation outside persistence, and added local tooling. This follow-up adds
function documentation, unit/E2E coverage, Makefile commands and CI against that
refactored branch. The candidate should record their actual assessment time and
review the final behavior before submission; no time total is inferred here.
