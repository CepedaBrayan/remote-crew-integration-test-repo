# Senior Integration Engineer Take-Home Project

## Payment Sync & Ledger Reconciliation

**Time Limit:** 2 hours (please stop at 2 hours even if unfinished, and log the time you actually spent in your README)
**Deliverable:** A working integration service, built on the provided starter repo, that boots with `docker-compose up` and actually works
**AI tools:** You are expected to use AI coding tools (Claude Code, Cursor, Copilot, etc.). You must be able to explain and defend every decision in the resulting code — that's the follow-up interview.

---

## Context

You're an integration engineer at **LeaseFlow**, a leasing platform for large multifamily operators. LeaseFlow needs resident payment data from **BigPMS** — the property management system of record — to power leasing and renewal decisions. You can't decide whether to renew a resident without knowing whether they actually pay rent.

LeaseFlow keeps a **local copy of the resident ledger** so its product features don't call BigPMS on every page load. Your job is to keep that local copy accurate.

BigPMS is a partner platform: you consume its APIs and webhooks, but you don't control it. Like every real partner system, it's imperfect — it rate-limits, it fails intermittently, and it sometimes delivers the same event twice. **Do not modify the mock BigPMS service.** Treat it like a partner you can't fix.

**The business stakes:** if LeaseFlow's ledger copy drifts from BigPMS's, operators make renewal decisions on wrong balances — chasing residents who already paid, or renewing residents who are months delinquent. Money data that's *almost* right is worse than no data.

---

## What We Provide

The starter repo boots with one command (`docker-compose up`) and includes:

- **Your service** (`app/`): FastAPI scaffold with a working health check, DB connection pool, and a background scheduler already wired to call your sync function every 30 seconds
- **Your database**: PostgreSQL, fully seeded — residents, leases, rent charges, and partial payment history. Every table you need exists. Don't alter or drop the provided tables or columns — but you **may** add your own indexes, constraints, or migrations (run from your app) if your design calls for them.
- **Mock BigPMS** (`mock-bigpms` service): the partner black box. Webhook subscription API, a paginated ledger-events feed, and per-resident payment totals.

See the README for endpoints, environment variables, and useful commands.

---

## Requirements

### 1. Webhook Subscription & Processing

BigPMS pushes `payment.received` events to registered subscribers. It won't send anything until you register.

**a) Register a subscription** on your app's startup (idempotent — safe to repeat):

```
POST {BIGPMS_API_URL}/api/v1/webhook-subscriptions
{"callback_url": "<CALLBACK_URL env var>", "secret": "<WEBHOOK_SECRET env var>"}
```

**b) Implement the callback endpoint** `POST /webhooks/bigpms`. For each incoming event:

- **Verify the signature.** The `X-BigPMS-Signature` header is a hex HMAC-SHA256 of the raw request body, keyed with the shared secret. Reject invalid signatures with a **401** — BigPMS deliberately sends some tampered payloads.
- **Apply the payment atomically.** Record the event in `webhook_events` AND insert the payment into `resident_ledger` in a **single transaction**. If either write fails, neither happens.
- **Handle redelivery.** BigPMS delivers ~25% of events twice. A duplicate must not double-post the payment — and must still get a **2xx** response (otherwise BigPMS keeps retrying forever). Record duplicates in `webhook_events` with status `duplicate`.

Webhook payload:

```json
{
  "event_id": "evt-2001",
  "event_type": "payment.received",
  "occurred_at": "2026-07-07T14:30:00Z",
  "data": {
    "resident_id": "res-101",
    "amount_cents": 150000,
    "method": "ach",
    "seq": 21
  }
}
```

> **Note — `property_id`:** payment events (both webhook and feed) do not carry a property, but `resident_ledger.property_id` is `NOT NULL`. Derive it. This dataset has a single property (`prop-001`); hardcoding it or resolving it via `residents → units → property_id` are both acceptable — document your choice. Multi-property is out of scope.

### 2. Scheduled Feed Sync

Webhooks miss things — deliveries fail, services restart, and LeaseFlow's local history predates the subscription. Implement `sync_bigpms_ledger_events()` (the scheduler already calls it every 30 seconds) to reconcile against BigPMS's feed:

```
GET {BIGPMS_API_URL}/api/v1/ledger-events?cursor=<seq>&limit=<n>
→ {"events": [...], "next_cursor": 20, "has_more": false}
```

Requirements:

- **Cursor-based paging.** Start from the cursor stored in `sync_state` (seeded at 0) and page through everything new. Apply events to `resident_ledger` idempotently.
- **Survive partner flakiness.** The feed randomly returns 500s and 429s (with a `Retry-After` header — honor it). Retry sensibly. A failed run must leave the database consistent and the next run must pick up where the last durable progress ended.
- **Advance the cursor transactionally.** The cursor must never move past events that weren't durably applied. A crash mid-batch must not lose or double-apply anything.
- **Record each run** in `sync_runs`: cursor range, events fetched/applied/skipped, status, error if any.

**Heads up:** events you already received via webhook *also appear in this feed* (and pages occasionally contain duplicates). Your ledger must count each payment exactly once, no matter how many times or through how many channels it arrives — and that has to hold under retries and concurrent delivery, not just on the happy path. How you guarantee it is a design decision.

When this works, the eight residents whose payment history was incomplete in the seed data (`evt-1013`–`evt-1020`) get backfilled automatically.

### 3. Reconciliation Report

Trust, but verify. Implement `GET /api/v1/reconciliation` returning a JSON report that proves (or disproves) that LeaseFlow's local ledger agrees with BigPMS:

- **Per resident:** local payment total (SQL over `resident_ledger`), BigPMS's authoritative total (one call to `GET /api/v1/residents/summaries` — don't call the per-resident endpoint N times), the delta, and a status:
  - `in_sync` — totals match
  - `missing_local_payments` — BigPMS has payments you don't
  - `local_exceeds_bigpms` — you have payments BigPMS doesn't know about
- **Data-quality issues**, detected in SQL against your local ledger:
  - `duplicate_charges`: same resident, same charge code, same amount, same date, more than once
  - `orphaned_ledger_entries`: ledger rows whose `resident_id` doesn't exist in `residents`

Suggested shape (adapt as long as it's clear and complete):

```json
{
  "generated_at": "2026-07-07T15:00:00Z",
  "residents": [
    {
      "resident_id": "res-101",
      "name": "Jane Doe",
      "local_payments_cents": 450000,
      "bigpms_payments_cents": 450000,
      "delta_cents": 0,
      "status": "in_sync"
    }
  ],
  "issues": {
    "duplicate_charges": [
      {"resident_id": "res-105", "charge_code": "rent", "amount_cents": 150000, "occurred_on": "2026-06-01", "count": 2}
    ],
    "orphaned_ledger_entries": [
      {"ledger_id": 42, "resident_id": "res-999", "entry_type": "charge", "amount_cents": 7500}
    ]
  },
  "summary": {"residents_out_of_sync": 1, "issues_found": 2}
}
```

> `issues_found` counts **data-quality issues only** — `len(duplicate_charges) + len(orphaned_ledger_entries)`. It is separate from `residents_out_of_sync`, which is a reconciliation status, not a data-quality issue.

The seed data contains **three planted issues**:

1. A **duplicate charge** (res-105 double-billed June rent) → `duplicate_charges`.
2. An **orphaned ledger entry** (res-999 doesn't exist in `residents`) → `orphaned_ledger_entries`.
3. A **local-only payment** (res-107, `evt-9999`) that BigPMS has no record of → surfaces as res-107 `local_exceeds_bigpms` in the `residents` array.

So once your sync works, a correct report shows **7 residents `in_sync`, res-107 `local_exceeds_bigpms`**, and `issues_found: 2` (the duplicate charge and the orphan; both are charges, so they don't move payment totals). Before your sync works, all eight residents show `missing_local_payments` — watching them converge is how you'll know your sync is correct.

> **Reconciliation is point-in-time.** BigPMS emits a new payment every ~15s once you're subscribed, and it lands in BigPMS's totals before your webhook/feed has applied it locally. A resident can briefly show `missing_local_payments` in the seconds between a new event and your service catching up — that transient delta is expected, not a bug. Judge correctness at a quiescent moment.

Do the aggregation in SQL. Set-based queries, not Python loops over rows.

### 4. README Write-Up

Include in your README (brief — bullet points are fine):

- **Time spent** — honestly. Stopping at 2 hours with unfinished work beats a suspiciously polished 6-hour submission.
- **Decisions and tradeoffs** — what you chose, what you considered, why. The *why* matters more than the *what*.
- **What you'd do next** — with more time. (This is where retry queues, DLQ, monitoring, and alerting belong: described, not built.)
- **AI usage** — which parts were AI-generated, what you kept as-is, what you corrected and why. You'll walk through this code with our engineers; "the AI wrote it" is not an explanation we accept, from candidates or from ourselves.

---

## Bonus (only if you have time left within the cap)

- **Diagnosable logging.** We will stop the mock BigPMS mid-run, wait a minute, restart it, and read your logs. Can we tell what happened — retries, failures, recovery — without guessing? Log like the person on call is you.
- **Sync status endpoint.** `GET /api/v1/sync/status`: current cursor, last run outcome, webhook counts by status. The thing you'd check first when someone says "the numbers look wrong."

---

## What NOT To Do

- **Don't modify** the mock BigPMS service, or alter/drop the provided tables and columns. (Adding your own indexes, constraints, or migrations is fair game — see "What You're Given.")
- **Don't build a frontend.** There isn't one.
- **Don't implement** authentication, a dead-letter queue, a message broker, or multi-property support. Describe them in the README if relevant; don't build them.
- **Don't make the mock BigPMS less flaky.** Handling its behavior *is* the test.
- **Don't leave dead code** or half-finished experiments.

---

## Evaluation

What we weigh (full rubric shared during the review):

- **Transaction semantics & idempotency (30%)** — money is counted exactly once, whatever the partner throws at you. Atomic writes, cross-channel dedupe, cursor correctness.
- **Webhook subscription & handling (20%)** — registration works, signatures verified, tampered payloads rejected, duplicates acknowledged but not double-applied.
- **Feed sync resilience (20%)** — paging, retries, `Retry-After` honored, durable progress, `sync_runs` audit trail.
- **Reconciliation & SQL (20%)** — set-based SQL, all planted issues found, correct deltas with direction.
- **Communication (10%)** — README decisions, honest time log, AI usage notes.
- **Bonus (up to +10%)** — diagnosable logging, sync status endpoint.

---

## Recommended Order

1. Read the README, skim the schema and seed data, poke the mock BigPMS endpoints (~15 min)
2. Webhook subscription + callback endpoint (~30 min)
3. Scheduled feed sync (~40 min)
4. Reconciliation report (~20 min)
5. README write-up (~15 min)
6. Bonus (remaining time, if any)

---

## Submission

Submit a **Git repository** (public or private, shared with us). It must boot with `docker-compose up` and work: we should be able to watch webhooks land, see the backfill converge, and pull a clean reconciliation report within a few minutes of starting it.

If something is ambiguous, **make a decision and document it in your README**. That's part of the test — this role is full of ambiguity, and we want to see how you handle it.

Good luck.

---

## Solution

Implemented webhook processing, scheduled payment backfill, and reconciliation. The supplied schema, seed data, and mock BigPMS source remain unchanged.

### Architecture

| Module | Responsibility |
|---|---|
| `app/main.py` | HTTP routes, startup hooks, and scheduler wiring. |
| `app/auth.py` | Webhook signature verification. |
| `app/dto.py` | Payment and feed validation. |
| `app/webhooks.py` | Subscription registration and webhook processing. |
| `app/sync.py` | Feed pagination, retries, and orchestration. |
| `app/reconciliation.py` | Bulk partner totals and report assembly. |
| `app/orm.py` | All application SQL and persistence transaction boundaries. |
| `app/db.py` | Connection pool and cursor helpers. |
| `app/utils.py` | Best-effort auditing of rejected webhook deliveries. |

`AGENTS.md` and `SKILL.md` define repository conventions and verification steps. Despite its name, `orm.py` uses psycopg2 directly.

### Webhook processing

- Startup creates an idempotent unique index on `bigpms_event_id`, then registers the callback. Registration has a five-second timeout and three attempts; exhausted failures abort startup.
- Signatures are verified against the exact request bytes using a constant-time HMAC comparison.
- Property ownership is resolved through `residents → units`.
- Payment and webhook audit writes commit together. Database uniqueness prevents duplicate payments across concurrent deliveries and feed imports.
- Valid redeliveries return `200` and update the audit status to `duplicate`. Rejected requests use independent receipt IDs to protect existing audit records.

| HTTP status | Outcome |
|---|---|
| `200` | Processed or duplicate. |
| `401` | Missing or invalid signature. |
| `422` | Invalid payload or unknown resident. |
| `503` | Payment persistence failed. |

### Scheduled feed sync

Each page commits its payments, cursor, and run counters in one transaction. Failures roll back the current page; the next run resumes from the last committed cursor.

- Four attempts handle network errors, HTTP 429, and HTTP 5xx, with exponential backoff and support for `Retry-After`.
- HTTP requests and waits occur outside write transactions.
- A PostgreSQL advisory lock prevents overlapping sync owners across processes.
- The next owner marks abandoned runs as failed and resumes from durable state.
- Invalid events or unknown residents stop their page without advancing the cursor.
- `sync_runs` counters describe committed pages: fetched includes repeated rows, applied counts new payments, and skipped counts duplicates.

The interval defaults to 30 seconds. Optional `SYNC_PAGE_SIZE` defaults to `10` and accepts values from `1` to `50`.

### Reconciliation

The report compares SQL payment aggregates with one bulk BigPMS summaries request per attempt.

`delta_cents = local_payments_cents - bigpms_payments_cents`

Zero means `in_sync`, negative means `missing_local_payments`, and positive means `local_exceeds_bigpms`.

All local queries share one read-only, repeatable-read snapshot. Duplicate charges are grouped by resident, charge code, amount, and UTC date. Orphans are individual ledger rows without a matching resident.

`issues_found` counts duplicate groups plus orphan rows, separately from `residents_out_of_sync`. Invalid or incomplete partner data returns `503` rather than a partial comparison.

The endpoint never modifies data. Its local snapshot cannot be atomic with BigPMS, so newly emitted payments can briefly appear as missing while ingestion catches up.

### Running checks

Install Python 3.12 and Make for local tooling. E2E tests also require Docker.

```bash
python3.12 -m venv .venv
make install-dev
```

On Windows:

```powershell
py -3.12 -m venv .venv
make install-dev
```

The Makefile uses the virtual environment directly; activation is optional.

| Command | Purpose |
|---|---|
| `make format` | Fix supported lint issues and format `app/`. |
| `make check` | Check app lint and formatting. |
| `make test` / `make test-unit` | Run unit tests. |
| `make test-e2e` | Run isolated Docker E2E tests. |
| `make test-all` | Run both suites. |
| `make help` | List available commands. |

Unit tests cover validation, authentication, retries, error handling, and persistence contracts. An architecture check guards against application SQL execution outside `orm.py`.

E2E tests verify real HTTP and PostgreSQL behavior, including concurrent deliveries, rollback, backfill, process termination, cursor recovery, and reconciliation. Controlled failures use scripted HTTP fixtures and temporary databases without modifying the supplied mock.

The E2E runner creates an isolated Compose project with a temporary test container, preserves development data, and cleans up its resources on normal completion or failure. Reports and logs remain in `artifacts/`.

GitHub Actions runs lint, unit tests, and E2E tests for PRs targeting `dev` or `main`, and pushes to `dev`, `main`, `features`, or `feature/**`.

### Time spent

Approximately **4 hours**, exceeding the requested two-hour cap. This includes implementation, refactoring, documentation, and automated verification.

### Decisions and tradeoffs

- Given the complexity and limited time, I chose **AI-assisted development supported by unit and E2E tests** to accelerate implementation and verify duplicate safety, transaction behavior, and recovery.
- I retained the existing FastAPI, psycopg2, and scheduler stack. PostgreSQL uniqueness and explicit transactions provide correctness without introducing an ORM or queue infrastructure.
- Invalid feed events block their page instead of being skipped. This avoids losing payments but requires correction before progress can resume.
- The supplied webhook audit schema stores the latest outcome per valid event rather than a complete delivery history. Failed-delivery auditing is best effort during database outages.

### What I would do next

- **Add monitoring and alerts** for stale cursors, repeated sync failures, and sustained reconciliation differences. A healthy service does not necessarily have current payment data.
- **Add durable rejection handling and controlled replay** for invalid feed events. A retry queue or DLQ could support remediation if operational volume warrants it, while retaining the existing duplicate protection.

### AI usage and my contribution

Approximately **80% of the code was generated by ChatGPT/Codex**, based on my estimate. AI produced the initial implementation, most subsequent feature code, tests, CI configuration, and documentation drafts.

My direct refactoring focused on the first PR: separating persistence into `orm.py`, signature verification into `auth.py`, and audit helpers into `utils.py`, while keeping validation outside persistence. I also guided the conventions in `AGENTS.md` and `SKILL.md`, establishing the structure for subsequent work.

I retained the AI-generated uniqueness, transactional sync, retry, and reconciliation implementations, supported by automated verification. Later features and SQL consolidation were implemented by Codex under my direction. During iteration, the unavailable pytest version was corrected to `8.4.2` to support local installation.

The tests provide evidence for covered scenarios; they do not replace understanding the implementation and its limitations.