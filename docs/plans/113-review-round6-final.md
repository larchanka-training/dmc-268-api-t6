# Final verification after GitLab integration — PR #132 / #113

## Integrated source

Verified main: `2251b5936822827fc75b38cfc6b352c6a5e89a6b`, including merged
#137 GitLab review publication and pipeline steps, #121 receipt retries and #126
rollback revision admission. Rebased checkpoint:
`d88725771ef89a94756ea3a06befd37b44b425d4`, plus the publication integration
corrections described in the [integration proof](113-review-round6-integration.md).

The shared provider route controls readiness and checkpoint check-run policy.
Inactive-origin terminalization disables check-run publication before provider
route/configuration selection. GitLab routing, stored overview, partial receipts,
shared rendering, pipeline events and commit-bound notifications coexist with
immutable GitHub origins, every request fence and successful-POST receipt retention.
Typed receipt retry intent and mandatory original-installation forwarding remain.
No dependency or migration is added.

## Focused evidence

The inherited selection first produced 15 failures and 148 passes in 34.68s,
zero skips. Fourteen failures exposed missing check-run policy during origin
cancellation; one exposed GitHub-only readiness intercepting the GitLab route.
The minimal correction produced 163 passes in 30.38s, zero skips. The expanded
22-file selection passed 537 tests in 179.65s, zero skips and 2 dependency warnings,
with PostgreSQL and RabbitMQ configured.

The [integration proof](113-review-round6-integration.md) details provider routing,
origin fences, receipt retry, partial-publication, stream/step, worker and persistence
coverage. Provider calls remain outside database transactions. Historical round-five
results verify `b5e0cec` on main `11ddfab` and do not verify this integrated source.

## Repository gates and independent reviews

- `uv run ruff check .`: All checks passed.
- `uv run ruff format --check .`: 397 files already formatted.
- `uv run mypy .`: Success: no issues found in 394 source files.
- `uv run pytest -rs` with PostgreSQL/RabbitMQ configured: 3190 passed,
  2 existing dependency warnings in 618.60s (0:10:18), zero failures/skips.

Integration collection selected 543 / 3190 tests, with 2647 deselected in 1.33s.
All 543 integration tests ran in the full suite above; there was no separate local
integration execution. These current combined-source results are distinct from
the historical round-five executions.

The tested snapshot contains 418 tracked Python/runtime/configuration files.
All SHA-256 fingerprints match the frozen source. Canonical sorted JSON SHA-256:
`52caf06dd6bcea53a1ad4416c3cd1c70947d79abea1f1a94bb93936fb4d2efb2`.

Independent Standards and Spec reviews of the actual main-to-working-tree delta
reported zero actionable findings on each axis. No historical gate or review
result substitutes for the current final verification.

## Publication and merge prerequisites

`git diff --check` and configured staged-file checks passed: trailing whitespace,
end of file, merge conflicts, Ruff check, Ruff format and mypy. YAML/TOML checks
had no applicable staged files. No hook changed a file; all 418 tested fingerprints
matched after the checks. Publication and review-response results are recorded in the
[PR #132 body](https://github.com/larchanka-training/dmc-268-api-t6/pull/132);
required CI is visible in its checks on the exact published head. These receipts
remain separate from the local evidence above.

Before merge, recheck actual main, obtain approval on the current head and confirm
reviewer-owned thread resolution. Stop all old Run writers before merge and agree
the merge time with the tech lead. A main push starts staging deployment, so writer
shutdown remains a prerequisite before that push.
