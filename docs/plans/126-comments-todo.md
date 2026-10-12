# PR #126 review correction checklist

- [x] Existing PR #125 coordination comment covers shared docs/tests scope.
- [x] Task 1 RED: stderr noise/secrets, known refusal, invalid/ambiguous results.
- [x] Task 1 GREEN: separated streams, controlled reasons, private probe cleanup.
- [x] Task 2 RED: literal probe YAML, nested refused auto rollback, storage pulls,
  healthy up then failed diagnostics truthful manual/auto metadata.
- [x] Task 2 GREEN: pull missing, state before diagnostics, test-only ID annotation.
- [x] Checkpoint: probe config mutations fail; focused deployment tests green.
- [x] Task 3 RED/GREEN: actual single A head, generated fixture parent, safe N,
  actual report revisions; focused verifier tests green.
- [x] Task 4 docs: portable recipes, state/pull/proof limits, edge entry boundary,
  no personal path. Retain ancestry defense with reviewer rationale.
- [x] Modern Compose real isolated A/B verification passes and owned cleanup clean.
- [x] Separate PostgreSQL/RabbitMQ test services and dedicated vhost.
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [x] `uv run pytest` with real integration services and zero skips.
- [x] Refresh proof, head/base and counts.
- [x] Coordinator final review against every comment.
- [ ] Update PR #126 body and reply with commit/evidence; reviewer resolves threads.

Modern proof: Compose 5.6.0, six accepted/refused/offline cases and owned cleanup passed.
Both literal probe mutations: 4 failures each in separate temporary source copies.

Final gates: lint passed; format 358 files; mypy 355 sources; pytest 2608 passed, 0 skipped,
2 warnings in 336.75 seconds against owned integration services.

Final independent review after the filename collision correction: **Standards 0 findings;
Spec 0 findings**. The coordinator confirmed the exact gates and final development handoff.
Publication is prepared; external delivery is recorded after the push and reviewer reply.
