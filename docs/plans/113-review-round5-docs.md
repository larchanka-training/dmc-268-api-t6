# Round-five documentation verification — PR #132 / #113

Combined main: `11ddfabf31a3345e6d21cdf57586281cb62cee7c`, including #121 and
#126. Rebased parent: `b4fb4a24c4ff8cbcf6256c289565283c51b80851`. Scope: the two
documentation findings from the 2026-10-12 21:51 UTC review.

## Source-verified recovery behavior

- `deploy/compose/staging.yml` runs `alembic upgrade head && python -m
  app.bootstrap.seed_prompts` in bootstrap. `api`, `worker` and `webhook-worker`
  each require `bootstrap: service_completed_successfully`. An unknown revision
  therefore blocks all three services; a previous image without 0030 cannot
  restore the stack after that migration.
- `alembic.ini` uses `script_location = alembic`. The actual graph is in
  `alembic/versions`; `20261010_0030` follows `20261010_0029`. Migration 0030 adds
  a nullable Run origin and backfills existing rows without adding a default.
- Merged #126 `deploy/scripts/rollback.sh` pulls the target, resolves immutable
  identity and runs `check_target_revision` before writing Compose env/config,
  ensuring role env files or running Compose up. The checker loads the target
  image graph and reads the database revision in a short read-only connection.
  A revision missing from the target graph returns exit 1 and the controlled
  verdict `rollback revision check: refused: database revision is unknown`.
- `deploy/scripts/deploy.sh` removes stale bootstrap then runs Compose up; a
  failure calls rollback. The pre-change admission guard protects against
  further rollback changes; it does not undo changes from the failed deploy.
  Historical pre-#126 rollback could replace containers with an incompatible
  image and leave staging unavailable. Recovery after 0030 requires a forward
  release containing that revision.
- Independent old writers outside this Compose stack can insert NULL origin.
  `run_origin.py` rejects missing origin; new-worker lifecycle and reconciliation
  cancel eligible nonterminal work as `installation_unavailable`. Terminal
  history remains unchanged. An old worker cannot provide this cancellation.
- 0030 downgrade drops immutable origin; 0029 downgrade conservatively folds
  availability into enabled, then drops numeric account identity. Re-upgrade
  cannot reconstruct lost lineage. The guard must not be bypassed by downgrade.

These conclusions come from source inspection, not an old-image runtime
reproduction or a production deployment. Both CICD deployment passages, #125
bootstrap/LLM/`PORTAL_URL`, writer shutdown before merge and manual GitHub webhook
redelivery remain documented.

## Historical evidence and checklist correction

Round-four evidence remains tied to main `620c81d`, rebased parent `e417c2ec` and
published head `8a87fe9d10794ba92002f537b349f84ee7dbdac9`. The UI link, normal-hook
commit/push, review response and successful required
[CI run 38298570137](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38298570137)
are now marked complete. Historical counts and durations are unchanged.
[UI #105](https://github.com/larchanka-training/dmc-268-ui-t6/issues/105) remains
on course board 12 with Todo status and linked in
[API #106](https://github.com/larchanka-training/dmc-268-api-t6/issues/106#issuecomment-6131037958).

## Documentation checks

`git diff --check` passed. A sweep of `docs/plans/113*.md` and the prepared PR body
found no private filesystem paths, local service addresses or credential-bearing
DSNs. Only documentation was changed for these findings. The [final proof](113-review-round5-final.md)
records all four completed combined-head gates and independent reviews.
Publication hooks and required CI will be reported from their own executions. The [integration proof](113-review-round5-integration.md)
records the completed PostgreSQL receipt checks (333 focused passes, zero skips).
