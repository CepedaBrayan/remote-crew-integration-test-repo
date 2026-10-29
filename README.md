## Solution

Implemented webhook processing, scheduled payment backfill, and reconciliation. The supplied schema, seed data, and mock BigPMS source remain unchanged.

### Architecture

| Module | Responsibility |
|---|---|
| `app/main.py` | HTTP routes, startup hooks, and scheduler wiring. |
| `app/auth.py` | Webhook signature verification. |
| `app/dto.py` | Payment and feed payload validation. |
| `app/webhooks.py` | Subscription registration and webhook processing. |
| `app/sync.py` | Feed pagination, retries, and sync orchestration. |
| `app/reconciliation.py` | Bulk partner totals and report assembly. |
| `app/orm.py` | All application SQL and persistence transaction boundaries. |
| `app/db.py` | Connection pool and cursor helpers. |
| `app/utils.py` | Best-effort auditing of rejected webhook deliveries. |

`AGENTS.md` and `SKILL.md` document repository conventions, transaction guarantees, and verification steps. Despite its name, `orm.py` uses psycopg2 directly; no ORM framework was introduced.

### 1. Webhook subscription and processing

Startup creates an idempotent unique index on `resident_ledger.bigpms_event_id`, then registers the callback with BigPMS. Registration uses a five-second timeout and three attempts, with one- and two-second waits. Exhausted failures abort startup.

Incoming requests are authenticated using a constant-time HMAC-SHA256 comparison over the exact request bytes. Validated payments resolve their property through `residents → units`.

The ledger insertion and webhook audit commit in one transaction. PostgreSQL uniqueness and `ON CONFLICT DO NOTHING` prevent double counting across concurrent requests, feed imports, and previously seeded payments.

| HTTP status | Meaning |
|---|---|
| `200` | Payment processed or duplicate acknowledged. |
| `401` | Missing or invalid signature. |
| `422` | Invalid payload or unknown resident. |
| `503` | Payment persistence failed. |

The supplied audit schema stores one row per valid event. Redelivery updates its status to `duplicate` while preserving its first stored payload. Payloads retain extra partner fields, although typed values may be normalized during serialization.

Rejected deliveries use independently generated receipt IDs so untrusted event IDs cannot overwrite legitimate audit records. Failed-delivery auditing is best effort when PostgreSQL is unavailable.

### 2. Scheduled feed sync

The scheduler runs every 30 seconds, starting from the cursor stored in `sync_state`. Each page commits its payments, next cursor, and run counters in a single transaction. A failed page rolls back completely; previously committed pages remain available to the next run.

- Webhook and feed ingestion share the same duplicate-safe ledger insertion.
- Feed events are validated before persistence. Invalid events, unknown residents, or inconsistent paging metadata stop the page without advancing its cursor.
- Each page allows four attempts for connection failures, timeouts, HTTP 429, and HTTP 5xx. Backoff is one, two, and four seconds; valid `Retry-After` values on 429 take precedence.
- HTTP requests and retry waits occur outside write transactions.
- A PostgreSQL session advisory lock prevents overlapping sync owners across processes. The next owner marks abandoned runs as failed and resumes from durable state.

`sync_runs` records the cursor range, outcome, error, and counters for committed pages. `events_fetched` includes duplicate rows, `events_applied` counts new insertions, and `events_skipped` counts duplicates. Retried requests and rolled-back pages do not inflate these counters.

`SYNC_INTERVAL_SECONDS` defaults to `30`. Optional `SYNC_PAGE_SIZE` defaults to `10` and accepts values from `1` to `50`.

### 3. Reconciliation report

`GET /api/v1/reconciliation` compares SQL payment totals with one bulk BigPMS summaries request per attempt. Every local resident is included, with zero used when no local payments exist.

`delta_cents = local_payments_cents - bigpms_payments_cents`

| Delta | Status |
|---|---|
| Zero | `in_sync` |
| Negative | `missing_local_payments` |
| Positive | `local_exceeds_bigpms` |

All local aggregation and issue detection run in one read-only, repeatable-read transaction:

- **Duplicate charges:** groups sharing resident, charge code, amount, and UTC calendar date.
- **Orphaned entries:** individual ledger rows whose resident does not exist.

`issues_found` counts duplicate-charge groups plus orphaned rows. `residents_out_of_sync` independently counts residents with nonzero payment deltas.

Missing, duplicate, unexpected, or invalid partner summaries return `503`; missing totals are not silently treated as zero. Transient partner failures receive up to three attempts, honoring `Retry-After` on 429.

After backfill at a quiescent moment, the expected seed result is:

- Seven residents `in_sync`.
- res-107 `local_exceeds_bigpms` because of `evt-9999`.
- Two data-quality issues: res-105's duplicate June rent charge and res-999's orphaned entry.

The endpoint reports discrepancies without modifying data. Its local snapshot is consistent, but cannot be atomic with BigPMS: newly emitted payments may briefly appear as missing locally while ingestion catches up.

### Verification

Local tooling requires Python 3.12 and Make. Docker is required for E2E tests.

```bash
python3.12 -m venv .venv
make install-dev
```

On Windows, create the environment with:

```powershell
py -3.12 -m venv .venv
make install-dev
```

The Makefile uses the virtual environment directly; activation is optional.

| Command | Purpose |
|---|---|
| `make format` | Fix supported lint issues and format `app/`. |
| `make check` | Check app lint and formatting. |
| `make test` / `make test-unit` | Run unit tests with external dependencies mocked. |
| `make test-e2e` | Run the isolated Docker E2E suite. |
| `make test-all` | Run unit tests followed by E2E tests. |
| `make help` | List available commands. |

Unit tests cover validation, authentication, retries, HTTP responses, persistence contracts, and transaction cleanup. An architecture check guards against application SQL execution outside `orm.py`.

E2E tests exercise real HTTP and PostgreSQL behavior, including concurrent deliveries, historical backfill, rollback, process termination, cursor recovery, and reconciliation. Deterministic failure scenarios use scripted HTTP fixtures and temporary databases; the original mock remains unchanged.

The E2E runner creates a separate Compose project, starts the three application services, and runs a temporary test container. It publishes no host ports, preserves development data, and cleans up its own resources on normal completion or failure. Reports and logs remain in `artifacts/`.

GitHub Actions runs lint, unit tests, and E2E tests for pull requests targeting `dev` or `main`, and pushes to `dev`, `main`, `features`, or `feature/**`.

### Time spent

Approximately **4 hours**, exceeding the requested two-hour cap. This includes implementation, refactoring, documentation, and automated verification.

### Decisions and tradeoffs

- Given the complexity and limited time, I chose **AI-assisted development supported by unit and E2E tests**. This accelerated implementation while providing executable checks for financial correctness and recovery behavior.
- I retained the existing FastAPI, psycopg2, and scheduler stack. Database uniqueness and explicit transaction boundaries provide duplicate protection and durable progress without introducing additional infrastructure.
- Invalid feed events block their page rather than being silently skipped. This protects against lost payments but requires correction before progress can resume.
- The existing audit schema records the latest outcome per valid webhook event rather than a complete delivery history. The unique index is installed at startup instead of introducing a migration framework.

### What I would do next

- **Add monitoring and alerts for ingestion freshness:** track stale cursors, repeated sync failures, and sustained reconciliation differences. Service health alone does not establish that payment data is current.
- **Add durable rejection handling and controlled replay:** retain invalid feed events for diagnosis and remediation, introducing a retry queue or DLQ if operational volume warrants it. This would address blocked pages while preserving duplicate safety.

### AI usage and my contribution

Approximately **80% of the code was generated by ChatGPT/Codex**, based on my estimate. AI produced the initial implementation, most subsequent feature code, tests, CI configuration, and documentation drafts.

My direct refactoring focused on the first PR: separating persistence into `orm.py`, signature verification into `auth.py`, and audit helpers into `utils.py`, while keeping validation outside persistence. I also guided the repository conventions in `AGENTS.md` and `SKILL.md`, establishing the structure for subsequent work.

I retained the AI-generated uniqueness, transactional sync, retry, and reconciliation implementations, supported by automated verification. Later features and SQL consolidation were implemented by Codex under my direction. During iteration, the unavailable pytest version was corrected to `8.4.2` to support local installation.

The automated tests provide evidence for the scenarios they cover; they do not replace understanding the implementation or its documented limitations.