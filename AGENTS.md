# Repository instructions

- Read `docs/SPEC.md` and the relevant README solution section before changing behavior.
- Use `SKILL.md` for payment ingestion, cursor recovery, or integration-test work.
- Keep code, comments, docstrings, test names, commit messages and documentation in English.
- Preserve the supplied `db/init.sql`, `db/seed.sql` and `mock-bigpms/` sources. Add necessary indexes through application setup; do not alter or drop provided tables/columns.
- Keep validation and HTTP concerns out of persistence. Use the existing psycopg2 stack; avoid introducing an ORM, broker, DLQ, or framework for this assessment.
- Preserve one ledger payment per `bigpms_event_id` across webhook and feed paths. Never replace database uniqueness with a check-then-insert operation.
- Document every new function's contract, ownership of transactions, side effects and relevant failure behavior. Use short docstrings when a function is simple.
- Include unit tests for changed contracts and real PostgreSQL/HTTP tests for transaction, concurrency and recovery guarantees. Keep failure injection confined to disposable test databases and fixtures.
- Run `make check` and `make test-unit`; run `make test-e2e` for ingestion or database changes. Docker must be available for E2E. Report what was actually run and any limitations.
- Keep the README current with decisions, limitations, commands and honest AI usage. Do not invent candidate time spent.
- Target the branch requested by the user. Do not merge a PR or reset development data unless explicitly requested.
