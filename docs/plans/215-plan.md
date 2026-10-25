# #215 rollback switch boundary and runbook tails

Issue: [#215](https://github.com/larchanka-training/dmc-268-api-t6/issues/215).
Delivery: [PR #297](https://github.com/larchanka-training/dmc-268-api-t6/pull/297).
Checklist and dated validation: [215-todo.md](215-todo.md).

## Outcome and scope

Protect the late edge of the release switch with one behavioural regression and align
the rollback runbook with the existing scripts. `deploy.sh` already writes `.deploy-switch`
before `.env` and the new-image pull; the regression must detect moving that marker too late.

The six approved paths are `tests/test_deploy_staging_services.py`, `docs/CICD.md`,
`docs/SECRETS.md`, `.github/workflows/rollback.yml` (image-input description only), and these
two plan documents. Preserve all pre-existing #179 tests and runtime scripts. No dependency,
migration, monitoring change or shared-staging exercise is required.

## Implementation and acceptance

1. **Late-switch regression.** Deploy A then B with distinct run IDs. Attempt C with run
   `103-1` and a failed C image pull; call the real CI rollback helper with that same run ID.
   Before rollback, `.env` is C, state is B and no C compose-up occurred. After rollback,
   `.env` and state are B, `rolled_back=true`, output contains `rolled back to …B` and no
   `release unchanged`, and calls prove B pull → compatible revision probe → B up using
   B's immutable identity. Previous remains B. Keep all existing tests unchanged.
2. **Mutation proof.** Temporarily move the complete marker-write block after
   `docker pull "${IMAGE}"`. The new test must fail through rollback behaviour: CI reports
   `release unchanged` and leaves C in `.env`. Restore the script byte-for-byte and prove
   GREEN. Never commit the mutation.
3. **Rollback documentation.** Make the diagram/job row conditional on crossing the release
   switch and include first-deploy bootstrap. Document empty-image reapplication after an
   application auto rollback or deploy registry-login refusal, the promotion consequence,
   and explicit SHA/digest for going farther back. Describe the no-previous bootstrap
   exception. Distinguish automatic Compose delivery from manual edge/ports refresh; manual
   workflow uploads no secrets bundle. TLS refusal follows host preparation but precedes
   application changes.
4. **Environment guidance.** Add sorted webhook-worker `REDIS_URL`; qualify key samples by
   image, role and configured optional values. Name the verifier's omitted optional keys:
   `GITHUB_CLIENT_ID`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_FALLBACK_MODEL`, `PORTAL_URL`.
   Include `release unchanged` among paths without env diagnostics and link the failure
   table. Distinguish CI's successful early-skip step from a manual missing-password refusal.

The behavioural regression and restoration checkpoint precede documentation completion.
Compare prose with actual scripts/workflows; do not add tests that merely mirror prose.

## Verification and delivery

Run the exact rollback checkpoint:

```sh
uv run pytest tests/test_deploy_staging_services.py tests/test_rollback_revision.py tests/test_rollback_verifier.py -q
```

Run the required gates with uv: `ruff check .`, `ruff format --check .`, `mypy .`, `pytest`.
The full suite requires disposable PostgreSQL and an isolated RabbitMQ test vhost following
`docs/TEST_PLAN.md`; zero skips are required. Shell workflow tests require genuine Linux
Bash parity. Report each run's scope honestly: a focused pass does not certify the full suite.
Independent Standards and Spec reviews precede publishing; rebase on main before review and
merge. PR body uses What / Why / How to verify / Refs with `Refs #215`.

## Review follow-up

The 2026-10-25 review requested unconditional empty-image/promotion wording, a bootstrap
exception, corrected early-refusal guidance, explicit auto-rollback Compose provenance, a
separate unchanged terminal node, and concise durable planning evidence. These corrections
remain within the approved six-path scope and preserve the original regression and runtime.
The review-follow-up static gates and the exact genuine-Linux rollback checkpoint passed;
independent Standards and Spec reviews found zero actionable items. See the checklist for
dated validation and the PR current-head CI check; earlier full-suite results are historical.

## Coordination and acceptance

Before shared-document edits, recheck open PR ownership and comment on each owning PR.
Initial coordination covered #290, #279, #254, #227 and #220; review follow-up rechecked
current owners #280, #279, #227 and #220. Their migration, monitoring and GitLab hunks remain
outside this change.

Current-head approval and resolved review threads are required before merge. The tech lead
accepts AC, closes the issue and updates board 12. Publishing does not authorize those actions.
