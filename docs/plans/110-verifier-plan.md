# Implementation plan: rollback verifier Compose key drift (#110, #106)

## Scope and decisions

Start from `origin/main` `7546e539e1c1f565f65b9a5ca4cdc5dd1fbfb3fa` in the
isolated `/private/tmp/dmc-268-rollback-verifier-110` worktree. The reported
compatible rollback succeeds, but the verifier omits Compose's worker `REDIS_URL`.
Keep the production Compose and rollback admission logic unchanged. Do not edit
files owned by PR #124 or PR #148.

Add a permanent regression in `tests/test_rollback_verifier.py` before changing
`scripts/verify_rollback.py`. Exercise the existing prepared-verifier boundary and
compare its independent expected role key sets against the real checked-in
Compose environment keys plus the generated role env-file keys and a controlled
base-image key set. Use existing repository parsing patterns; no new dependency
is needed. The test must fail on the omitted worker `REDIS_URL`, cover all three
roles, and fail for missing or unexpected keys. Do not derive the verifier's
production expectations from Compose: that would weaken the independent oracle.

Fix only the worker expected key set by adding `REDIS_URL`. Preserve exact sorted
key-list equality, role completeness, duplicate rejection and all secret/PEM
scanning. Retain existing hostile diagnostics tests; add focused assertions if a
new boundary needs them. Unit-test Docker fakes are acceptable; the six live
rollback cases must invoke the checked-in CLI directly without monkeypatch.

## Ordered work

1. Regression RED: add the cross-file contract test, run the focused test, and
   record failure showing only the missing worker key. Files: verifier tests.
2. Minimal GREEN: add the missing worker key, run verifier/revision/deployment
   focused tests, and record unchanged strict rejection and secrecy coverage.
   Files: verifier script and its tests. Depends on task 1.
3. Gates: start separate disposable PostgreSQL 17 and RabbitMQ 4 containers with
   unique project/names, credentials and loopback ports; configure
   `TEST_DATABASE_URL` and `TEST_RABBITMQ_URL`, ensure `jq` exists, run the four
   required `uv` gates and require zero skipped tests. No shared database/broker.
   Depends on task 2.
4. Live proof: build image A from this checkout when feasible, or identify the
   existing prepared historical A and explicitly record its source SHA limit.
   Invoke the unmodified fixed verifier with a fresh `rollback110-<16 hex>` project
   and new report path. Require all six cases and cleanup PASS. Capture A/B IDs,
   source/verifier/Compose hashes, migration M/N and secret-scan outcome. Depends
   on task 2; gate infrastructure may run independently of this verifier project.
5. Review and publish: add a separate follow-up evidence document/report instead
   of rewriting historical proof. Fresh standards/spec review must report zero
   findings. Clean all owned Docker resources and temporary image tags; verify
   absence by project labels. Rebase/check current main, commit with issue refs,
   push a separate PR with `What`, `Why`, `How to verify`, `Refs` and explicit
   `Refs #110` / `Refs #106`. Do not merge or close issues. Depends on tasks 3–4.

## Verification commands

From the isolated worktree, use `uv sync --locked` if required, then:

```bash
uv run pytest tests/test_rollback_verifier.py
uv run pytest tests/test_rollback_verifier.py tests/test_rollback_revision.py tests/test_deploy_staging_services.py
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
uv run python scripts/verify_rollback.py --project "$rollback_project" \
  --image-a "$rollback_image_a" --expect-image-a "$rollback_image_a_id" \
  --report "$rollback_report"
```

The test variables must target only owned PG/RabbitMQ resources. The verifier
creates its own internal project network, pinned stores, synthetic child image B,
credentials and offline network, and attempts cleanup in `finally`. Its Docker
wrapper is the shipped local-image pull seam; disclose that registry delivery is
not live-tested. It does not contact real GitHub/LLM providers. Require compatible
manual/auto exit 0, incompatible and unreachable manual/auto controlled exit 1,
unchanged refusal snapshots, complete exact role key evidence, secret-scan PASS
and cleanup PASS.

## Risks and limits

- Docker/build access may need approved execution; keep source edits isolated.
- Historical A validates current host scripts/Compose, not a current app build.
- Synthetic child B proves migration compatibility refusal without a downgrade;
  production registry delivery and external providers remain outside live proof.
- The full suite needs disposable PG/RabbitMQ and `jq`; fix prerequisites rather
  than accepting skips. Clean only resources created for this task.

## Checkpoints and open questions

The saved plan requires user approval before implementation under agent-loop.
No product/spec question is unresolved. Record actual runtime/image evidence
rather than predicting IDs or test counts.
