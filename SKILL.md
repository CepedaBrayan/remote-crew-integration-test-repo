---
name: maintain-leaseflow-ingestion
description: Implement and review LeaseFlow payment ingestion and recovery. Use for BigPMS webhooks, scheduled feed sync, cursor transactions, cross-channel deduplication, reconciliation reports, and their unit or Docker E2E tests in this repository.
---

# Maintain LeaseFlow ingestion

1. Read `AGENTS.md`, `docs/SPEC.md`, `db/init.sql`, and the relevant README solution section. Inspect the partner's existing contract without editing its implementation.
2. Identify the entry point and transaction owner:
   - Reconciliation: `reconciliation.py` validates bulk partner totals and assembles the report; `orm.py` runs set-based queries in one read-only repeatable-read snapshot.
   - Webhook: `main.py` authenticates; `webhooks.py` validates; `orm.apply_payment` commits ledger and webhook audit together.
   - Feed: `sync.py` fetches and validates; `orm.py` owns the advisory lock and commits each page with its cursor and run counters.
   - Shared insertion: `orm.insert_payment` receives a database cursor and a validated payment; it never commits or creates webhook audit records.
3. Keep all application SQL in `app/orm.py`, including reads, writes, index creation, advisory locks and transaction settings. Keep `db.py` for pool/cursor infrastructure. Have HTTP/orchestration modules call documented persistence functions; do not introduce separate SQL/store modules. Keep test-only fixture/assertion SQL in tests and preserve supplied initialization scripts.
4. Keep these invariants explicit in the change:
   - Enforce event uniqueness in PostgreSQL for both channels.
   - Advance only to the partner's validated `next_cursor`, in the payment transaction.
   - Preserve prior committed pages after failure; never reset the cursor to recover.
   - Keep network requests and retry waits outside write transactions.
   - Honor Retry-After and bound retry attempts; reject invalid pages instead of silently skipping money.
   - Hold the session advisory lock on a dedicated borrowed connection; unlock before returning it to the pool, or discard the connection.
5. Test observable outcomes rather than only SQL strings. Use unit tests for protocol validation/retry rules. Use `tests/e2e/test_feed_sync.py` for real rollback, process termination, lock contention and cross-channel races. Keep scripted failures in test fixtures; retain the scheduled test against untouched mock BigPMS.
6. Run `make check`, `make test-unit`, and `make test-e2e`. Read the JUnit report and logs in `artifacts/` when E2E fails. Confirm provided schema, seed and mock files have no diff.
7. Update README tradeoffs and AI attribution. Describe the PR's concrete behavior, transaction guarantee, verification and remaining limitations. Keep reconciliation read-only, define delta as local minus partner cents, and count data-quality issues independently from residents out of sync.
