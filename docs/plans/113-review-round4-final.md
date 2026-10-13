# Round-four final verification — PR #132 / #113

## Historical tested source and integration

Tested main: `620c81d927cebf3deb0721e5dacb7b70b721ad7e`.
Rebased parent: `e417c2ec5af2f7e5556614c38474743e31ebaa98` plus the
round-four test/documentation delta. #125 is included and changes configuration
and tests, so the full gates below ran again on this actual combined source.
#108/#134/#138 behavior and #127 AuthScope are retained. Both CICD bootstrap
paragraphs, including LLM/`PORTAL_URL`, and the first 0030 writer-stop contract
are preserved. #121 and #126 were OPEN at this historical checkpoint.

Every temporary mutation was restored before full gates. The tested snapshot
contains 390 tracked Python files; canonical sorted path-to-SHA-256 JSON hash:
`74bae0b9840619e6d7aadb0f39a67822152fc8c044339d8251f929fa13501d8b`. Publication source-integrity checks compare against this snapshot;
the final published SHA, hooks and CI results belong to the
[PR #132 timeline](https://github.com/larchanka-training/dmc-268-api-t6/pull/132).

## Completed repository gates

Commands ran with disposable PostgreSQL and RabbitMQ configured:

- `uv run ruff check .`: all checks passed.
- `uv run ruff format --check .`: 386 files already formatted.
- `uv run mypy .`: no issues found in 383 source files.
- `TEST_DATABASE_URL='<dev-db>' TEST_RABBITMQ_URL='<dev-broker>' uv run pytest -rs`:
  **2897 passed, 2 existing deprecation warnings in 363.02s (6m03s)**;
  zero failures and zero skips, process exit 0.
- `uv run pytest --collect-only -m integration -q`: **503 / 2897 tests collected,
  2394 deselected in 1.40s**, exit 0. All 503 integration cases ran within the
  full 2897-pass execution; no separate local integration execution is claimed.

Independent Standards and Spec review of the full base-to-working-tree delta
reported **0 actionable findings on each axis**. `git diff --check` passed.

## Four P3 corrections and follow-up

[Guard-order proof](113-review-round4-a.md): selected restored cases 5 passed
in 4.34s; the complete origin-guard file 79 passed in 47.37s; no skips.
Swapped disabled-before-origin guard mutation produced 4 failed / 1 passed
in 5.85s. Every inactive case failed the `update_check_run` invocation assertion
after its literal persisted cancellation outcome passed. The active paused control
passed. Original production source bytes were restored exactly.

[Documentation proof](113-review-round4-b.md): every historical 113 proof document
is portable while retaining actual SHAs, commands, counts and timings. Historical
round-three evidence and Accepted limits remain reader-facing. CICD documents
manual GitHub webhook redelivery within three days. Its recovery description is
corrected in round five: after 0030, an old image missing that revision fails
bootstrap and blocks API and both workers. Historical pre-#126 rollback could
replace containers and cause an outage; merged #126 rejects that image before
application-container changes. Recovery requires a fix-forward image knowing
0030. Independent old writers outside Compose can insert NULL-origin Runs; only
the new worker cancels eligible nonterminal work, preserving terminal history.

UI rerun 409 wording is tracked in
[UI #105](https://github.com/larchanka-training/dmc-268-ui-t6/issues/105), verified
on course board 12 with Todo status and linked in
[API #106](https://github.com/larchanka-training/dmc-268-api-t6/issues/106#issuecomment-6131037958).
No frontend implementation is included.

## Prerequisites pending at the round-four checkpoint

After #121/#126 merge, rebase #132 last on actual combined main. Keep both
`RunTriggerOutcome` and mandatory keyword-only `expected_installation_external_id`,
preserve #127 AuthScope and #125/#126 deployment contracts, verify the waiting
old-head CI receipt outcome and repeat full combined-head gates/CI. That future
integration is not claimed as tested. Obtain human approval on the final head;
reviewers resolve their own threads. Coordinate merge timing with axyi and stop
all old Run writers before merge, because main deployment is automatic.
Round-five integration and verification are recorded separately; the historical
2897-pass result above does not prove the later combined source.

## Historical publication

Published round-four head: `8a87fe9d10794ba92002f537b349f84ee7dbdac9`. Normal hooks
and source-fingerprint checks passed. UI #105 and the review response were published
on PR #132. Required [CI run 38298570137](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38298570137)
succeeded on that head: full suite 2897 passed in 509.89s; separate integration
execution 503 passed / 2394 deselected in 396.05s; both reported 2 existing warnings
and zero skips. Deploy staging was skipped. These are historical CI results.
