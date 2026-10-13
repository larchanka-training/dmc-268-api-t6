# Round-eight policy write origin fence

The SQL policy write correction follows the combined main integration.
The source change is confined to
`app/modules/reviews/infrastructure/policy_snapshot_unit_of_work.py`.
The existing policy persistence fixture now puts its Run in `running` before
writing; every round-trip, raw-byte, prompt-binding and write-once assertion is
preserved. Additional coverage lives in
`tests/test_run_policy_snapshot_origin_postgres.py`.

## Atomicity and preservation

Both `stage_documents` and `create_snapshot` acquire the immutable origin
installation `FOR SHARE`, then Repository `FOR SHARE`, then Run `FOR UPDATE`.
The joined authority rows are refreshed with `populate_existing`. The same
transaction checks `origin_active` and `running` before performing any insert.
Missing Run, NULL origin, deleted installation, replaced installation and
unavailable repository raise `RunCancelled("installation_unavailable")`.
An active origin with a non-running Run raises `RunCancelled("run_inactive")`.

The store still only flushes; the caller owns commits. Diagnostic pins,
documents and snapshot reads remain available after retirement. Rejection
leaves partial staged documents, raw bytes, normalized text and both hashes
unchanged. A frozen snapshot, its snapshot id, creation time and policy digest
remain unchanged. Active-origin controls retain write-once behavior.
No application policy types, snapshot use case, schema, dependencies,
append-only action records or paid-usage writer were changed.

## Focused proof

- RED before implementation: the real PostgreSQL `documents-retired` case
  failed with `DID NOT RAISE RunCancelled`. The existing adapter committed the
  policy write after an independent retirement transaction.
- GREEN on final Python sources:
  `uv run pytest tests/test_run_policy_snapshot_origin_postgres.py tests/test_policy_snapshot_postgres.py -q`
  — **39 passed, zero skips**, two existing Starlette deprecation warnings.
  Final repeat on the frozen complete Python source: **39 passed, zero skips in 24.48s**.
- Final-document provider fake acquires installation, Repository and Run
  `FOR UPDATE NOWAIT` in an independent transaction, proving the read UoW
  ended before the provider boundary. Retirement before its final return
  produces literal document/snapshot counts `(0, 0)`; the active control
  produces `(2, 1)` and a retry performs no additional reads.
- Independent transactions revoke authority after documents commit and before
  snapshot insertion. Counts remain `(2, 0)`; existing frozen input remains
  `(2, 1)`. Rejected partial-document retry remains `(1, 0)`.
- Rebind contention is proved with `pg_blocking_pids`, without a timing-only
  assumption. A writer waits behind installation/Repository write locks;
  after replacement commits it raises `installation_unavailable` and inserts
  nothing at its pending boundary.
- While blocked on Run, both installation and Repository reject independent
  `FOR NO KEY UPDATE NOWAIT` with SQLSTATE `55P03`; `FOR KEY SHARE NOWAIT`
  still succeeds. Both policy insert methods are covered.
- Cached Run/Repository/installation identity-map entities are refreshed;
  subsequent independent deletion, rebind, unavailability and terminal Run
  transitions reject writes. Uncommitted writes roll back on UoW exit.
- Focused Ruff lint and format check passed for all three Python files.
- Focused mypy with and without `--follow-imports=silent` passed:
  **3 source files**. The ordinary run also passed on the frozen complete source.
  An initial cross-import run reported four missing checkpoint arguments in the
  policy/revision adapters and context stage; those errors were resolved before
  final verification. Full-tree gates are recorded separately in the [final proof](113-review-round8-final.md).

Commands used dedicated disposable PostgreSQL infrastructure. These focused
results are separate from the complete-source gates. Historical diagnostics,
source bytes, migration contracts and the four P3 regression corrections are preserved.
