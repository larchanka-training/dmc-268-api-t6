# PR #126 review corrections

Correct issue #110 review on the isolated checkout rebased onto main `46a90772`.
The user authorized these narrow review fixes; no additional approval stop.

## Scope decisions

- P2-1: checker emits its controlled verdict to stdout. Suppress Compose stderr and admit
  only the exact compatible stdout line with zero exit status. Preserve trailing newlines
  with a sentinel; nonzero known refusal lines retain safe reasons, all other results fail closed.
- P3-1: annotate raw image ID branch as verifier-only; document local pull
  substitution and its registry-delivery proof limitation. No registry redesign.
- P3-2: `up --pull missing` permits absent pinned store images while app stays on
  inspected immutable local ID.
- P3-3: record current/previous state immediately after healthy `up --wait`, before
  diagnostics. Diagnostics failure still fails job, but next deploy sees true state.
- P3-4: literal captured probe YAML assertion plus nested failed deploy → refused
  auto rollback regression. Assert invariants from rollback entry, after deploy's
  previous changes, not from before the failed forward deploy.
- P3-5: keep repeatable opt-in verifier; derive M from image A's single-head graph,
  render B fixture parent from M and prevent revision collision. Portable report
  recipes use `"$(mktemp -d)/report.json"`; retain conditional private plugin exposure.
- P3-6: remove personal checkout path from original plan.
- P3-7: clarify admission boundary is entry to `rollback.sh`; edge upload/provision
  and proxy preparation already may change host. Docs only, no workflow redesign.
- P3-8: retain ancestry defense; acknowledge normal valid single-head reachability.

## Ordered tasks

1. **Probe boundary (small, 2 files):** tests/test_deploy_staging_services.py and
   deploy/scripts/rollback.sh. RED cases: compatible stdout with noisy/secret stderr,
   known refusal with progress, nonzero/ambiguous/unexpected results, no leaks or
   mutation. GREEN: separate capture, controlled reason and private-file cleanup.
2. **Invocation/state contracts (small, same 2 files, after 1):** RED literal YAML
   (external network and postgres DSN), nested refused auto rollback, immutable app
   with storage pull capability, healthy-up diagnostic failure manual/auto state.
   GREEN `--pull missing`, state before diagnostics, test-only branch comment.
   Failed up or admission must never record success. Focused tests checkpoint.
3. **Dynamic verifier (medium, after 1–2):** scripts/verify_rollback.py,
   tests/test_rollback_verifier.py, tests/fixtures/rollback/new_revision.py.
   RED changed A head, invalid/multiple heads, rendered parent and collision-safe N;
   GREEN dynamic graph/report values, preserving ownership/cleanup/secrecy checks.
4. **Docs/evidence (medium, after live proof):** docs/CICD.md,
   docs/plans/110-plan.md, 110-todo.md, 110-verification.md and .json. Update portable
   recipes, actual state/pull/boundary semantics, personal-path removal and honest
   current evidence. Preserve context for historical proof where necessary.

## Verification and coordination

- Test first for behavioral changes; focused deploy/rollback/verifier tests.
  Checkpoint: mutations external=false and postgres→db must now fail regressions.
- Real disposable A/B verifier with modern Compose 5.6.0 available via
  `DOCKER_CONFIG=/private/tmp/pr126-compose56`; cover compatible/incompatible
  manual/auto, unavailable DB, secret checks and owned cleanup. Docker escalation
  is required. Never use shared staging or another chat's DB/broker resources.
- Separate owned PostgreSQL/RabbitMQ and test vhost; set TEST_DATABASE_URL and
  TEST_RABBITMQ_URL. Run four exact gates: `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`; zero skips.
- Prepared image A can validate deployment-script changes; report its older app
  base honestly. Prefer fresh A if locally buildable. No new dependency planned.
- PR #125 overlap: docs/CICD.md, docs/SECRETS.md, tests/test_deploy_staging_services.py.
  Avoid SECRETS edits. Existing comment 6096022216 explicitly covers this continued
  rollback preflight/diagnostics/verifier scope and preservation of #111 behavior.
- Final review against issue #110 and each disposition; refresh PR body, actual
  base/head, gate counts and evidence. Reply with commit/reason in reviewer threads;
  reviewer resolves them. No merge or issue closure.
