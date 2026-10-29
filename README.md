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
Requirement 2 is implemented in the follow-up section below. The reconciliation
report (requirement 3) is implemented in its solution section below.

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

GitHub Actions runs two independent jobs on pushes to `features`, `main`, `dev` and `feature/**`, and pull
requests targeting `main` or `dev`: unit tests plus app lint, and the same Docker E2E command
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

## Solution — Requirement 2: scheduled feed sync

The existing scheduler now calls the feed sync every 30 seconds. It starts from
`sync_state.cursor_seq` for `bigpms_ledger_events`, requests pages until `has_more`
is false, and backfills the missing seed payments `evt-1013`–`evt-1020`. It does
not delete local-only payments such as `evt-9999`. Requirement 3 reports these
differences; see its solution section below.

### Implementation and transaction boundaries

- `app/dto.py` validates the flat feed format separately from the webhook envelope,
  then normalizes it to the shared payment DTO. It rejects invalid money, timestamps,
  event types, sequences and paging metadata before committing any progress.
- `app/orm.py::insert_payment` contains the shared resident-to-property lookup and
  duplicate-safe INSERT. It accepts the caller's SQL cursor and never commits.
  Webhooks retain their existing atomic ledger/audit transaction. Feed ingestion
  does not invent webhook audit rows; its audit lives in `sync_runs`.
- `app/sync.py` handles HTTP, validation, retries and page orchestration.
- `app/sync_store.py` handles run ownership and SQL transactions. Each page commits
  its ledger insertions, `sync_state.cursor_seq`, and `sync_runs` counters together.
  A failure in any write rolls back that entire page; earlier committed pages remain.
- The unique ledger event index is shared by both channels. `ON CONFLICT DO NOTHING`
  handles repeated rows, seed payments, replay and concurrent webhook/feed delivery.

The returned `next_cursor` must equal the maximum sequence in the page (the mock's
contract), and every event must be newer than the requested cursor. An empty final
page keeps the same cursor; an empty `has_more` page is rejected. Sequence gaps are
allowed because the partner owns sequence allocation. Invalid events or unknown
residents fail the page rather than disappearing behind an advanced cursor. Such
an event blocks progress until corrected; a durable rejection/replay workflow is a
future improvement, not a hidden skip policy.

### Retries, overlap and crash recovery

Feed requests have a 3-second connect timeout and a 10-second read timeout. Each
page gets at most four attempts for network failures, HTTP 429 and HTTP 5xx.
Backoff is 1, 2 and 4 seconds. On 429, valid `Retry-After` delta-seconds or HTTP-date
values replace backoff; malformed/missing values use backoff. Permanent HTTP errors
and invalid successful responses fail immediately. A long valid Retry-After can
extend a run; waits are not shortened. The final 429 also waits before releasing
ownership, so the next scheduled run cannot bypass that cooldown. HTTP and sleeps
happen outside SQL transactions.

A PostgreSQL session advisory lock prevents simultaneous sync owners across app
processes, in addition to the scheduler's existing `max_instances=1`. The owner
keeps one pooled connection until completion but commits each page independently.
A competing attempt gets a failed `sync_runs` row explaining that it was skipped;
it does not change the active owner's state. The supplied schema has no skipped
run status, so this is recorded as `failed` with zero counters.

On ordinary failure, the run is marked failed without overwriting its committed
cursor/counters. On process termination, PostgreSQL rolls back the open transaction
and releases its session lock. The next owner marks any abandoned `running` rows
failed, then resumes from `sync_state`. If PostgreSQL itself is unavailable, the
failure can only be logged until it recovers. Acquisition failures before a run can
be persisted cannot create a database audit row.

`sync_runs` records starting and ending cursors, finish time, status and an error
when applicable. Counters describe **committed pages**: `events_fetched` includes
repeated rows in those pages, `events_applied` counts new inserts, and
`events_skipped` counts duplicate insert attempts. Consequently fetched equals
applied plus skipped, even after a partial failure. HTTP retries and rolled-back
pages do not inflate these durable counters. Empty successful runs record zeros.
Logs identify run IDs, cursors, retry attempts/waits, committed page counts and
failures, so stopping and restarting the partner is diagnosable.

### Configuration and verification

`SYNC_INTERVAL_SECONDS` retains its default of `30`. Optional `SYNC_PAGE_SIZE`
defaults to `10` and must be between `1` and the partner maximum of `50`; invalid
values fail the recorded run. Set it in the app container environment to override.
No additional production service, database table or dependency is required.

```bash
make install-dev
make check
make test-unit  # Both requirements; mocked external dependencies
make test-e2e   # Both requirements; Docker required
make test-all  # Unit suite, then E2E suite
```

The E2E suite still runs the real app, PostgreSQL and unchanged mock in an isolated
Compose project. It now also verifies scheduler-driven historical backfill. For
repeatable failure tests, the test container hosts a small scripted HTTP fixture
and launches actual sync worker processes against separately seeded, temporary
databases. This isolates fault injection from both development data and the app's
background scheduler; it does not change the mock or patch production functions.
The test image includes app code solely to run these real worker processes.

New checks cover HTTP 500/429 retries (including elapsed Retry-After), duplicate
pages, empty runs, failure after a committed page and resumption, rollback when
progress auditing fails, unknown residents, competing sync processes, killing a
worker mid-page, abandoned-run recovery, and concurrent real webhook/feed writes.
Temporary constraints, blockers and databases are removed by fixture cleanup;
Compose cleanup remains the final boundary. Worker logs join JUnit and container
logs in `artifacts/`, which GitHub Actions uploads even on failure.

CI runs both suites for PRs targeting `dev` or `main` and pushes to `dev`, `main`,
`features`, or `feature/**`. Existing Makefile commands cover the expanded suites.
`AGENTS.md` records repository rules; `SKILL.md` provides a focused ingestion and
recovery workflow for future coding agents.

### Follow-up and AI usage

With more time: add operational alerts for repeated sync failures or stale cursors,
a durable invalid-event remediation process, and a formal migration workflow. Avoid
adding these systems before the assessment requires them. Session locks require a
direct/session-preserving PostgreSQL connection; transaction-pooling proxies would
need a different ownership mechanism.

ChatGPT/Codex implemented this follow-up from the current `dev` branch, including
the shared insertion refactor, sync service, tests, agent instructions and documentation.
The candidate selected the per-page atomicity, duplicate protection, retry and lock
strategy in discussion and should review the implementation and record actual time
spent before submission. This section does not claim completion of requirement 3.


## Solution — Requirement 3: reconciliation report

`GET /api/v1/reconciliation` compares every local resident's payment total with
BigPMS, reports local data-quality issues, and never repairs or changes data.
All monetary values are integer cents. `delta_cents = local - BigPMS`: zero means
`in_sync`, negative means `missing_local_payments`, positive means
`local_exceeds_bigpms`. Residents without payments remain present with a zero total.

### Implementation and decisions

- `app/reconciliation.py` fetches the bulk `/api/v1/residents/summaries` endpoint
  once per successful attempt, validates its response and assembles the report.
  It never calls per-resident summary endpoints. Three attempts cover connection
  errors, timeouts, HTTP 429 and 5xx; waits are one and two seconds, overridden by
  valid Retry-After values on 429. Connect/read timeouts are 3/10 seconds.
- Missing, duplicate, unexpected resident identities or invalid/negative/non-integer
  totals fail the comparison with HTTP 503. The dataset assumes a shared resident
  roster, so omitted residents are not silently interpreted as zero. A zero total
  must be explicitly supplied. Partner failure and local database failure likewise
  return 503 with no partial or fabricated report; details are logged server-side.
- `app/reconciliation_store.py` performs three set-based SQL queries in one
  `REPEATABLE READ, READ ONLY` transaction. Payment totals use SUM/GROUP BY and
  include all residents with a LEFT JOIN/COALESCE. Python compares already aggregated
  results; it does not sum ledger rows. Network calls occur before this transaction.
- Duplicate charges are groups sharing resident, charge code, cents and UTC date,
  with COUNT(*) greater than one. UTC is explicit so server timezone settings do
  not change the grouping. Orphans are individual ledger rows with no resident,
  detected through NOT EXISTS. Neither charges nor orphaned payments are attributed
  to an existing resident's payment total.
- `issues_found` counts duplicate groups plus orphan rows. It does not count extra
  duplicate copies or residents with payment differences. `residents_out_of_sync`
  independently counts nonzero deltas. Residents and issue results have stable order.

After backfill at a quiescent moment, the seed produces seven `in_sync` residents,
res-107 `local_exceeds_bigpms` due to evt-9999, one duplicate-charge group for
res-105's June rent and one orphan for res-999: `issues_found = 2` and
`residents_out_of_sync = 1`. The report preserves all three planted anomalies.
Before backfill, all eight residents have missing local payments.

`generated_at` is the UTC report assembly time. REPEATABLE READ guarantees that
all local queries see one snapshot; no atomic snapshot spans PostgreSQL and the
remote partner. New partner payments can briefly produce missing-local deltas
while webhook/feed processing catches up. This is expected; compare at a quiet
moment. A partner snapshot timestamp or shared watermark would improve this with
more time. Repeated report requests are live reads, without a cache.

### Tests, commands and AI usage

Existing commands include all three requirements:

```bash
make install-dev
make check
make test-unit
make test-e2e
make test-all
```

Unit tests cover the single bulk request, retries, Retry-After, payload validation,
roster completeness, all delta statuses, separate counters, route behavior, and
read-only transaction/connection handling. New E2E tests run the actual API and
feed worker against a temporary PostgreSQL database and a stable HTTP partner
fixture. They check the exact before/after seed report, zero-payment residents,
UTC charge grouping, one bulk call per report, invalid/unavailable partner data,
and unchanged ledger data after reads. This stable fixture removes the timing
ambiguity of comparing two actively changing systems. Existing untouched-mock
webhook/backfill tests continue to run. All suites run in GitHub Actions and retain
diagnostics through the existing E2E runner; no additional command is required.

ChatGPT/Codex implemented the report, tests and documentation from the current dev
branch using the discussed design decisions. The candidate should review the SQL,
error policy and snapshot limitation and record actual assessment time. Future
work includes report freshness monitoring and remediation workflows, rather than
automatic financial corrections in a GET endpoint.
