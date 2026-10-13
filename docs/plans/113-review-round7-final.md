# Historical unpublished verification — PR #132 round seven / #113

This checkpoint was not published. Its 3196-pass result verifies only main
`2251b59` plus the then-unpublished P3 delta on `075004e`; it does not verify
the later #136 integration. Current combined-source evidence is recorded in
the [round-eight final proof](113-review-round8-final.md).

## Historical verified source

Base main: `2251b5936822827fc75b38cfc6b352c6a5e89a6b`.
Starting head: `075004e996db907b19787c91322ff8b62e43825e`, plus the six
PostgreSQL regression cases and documentation corrections in this round.
Production source, migrations, deployment configuration, workflows and dependency
files match the starting head exactly. Temporary mutations were restored before
final verification. No dependency, migration or runtime behavior was added.

The historical tested snapshot contained 418 tracked Python/runtime/configuration files.
SHA-256 fingerprints matched the frozen checkpoint; the two test changes are included
in that snapshot. Canonical sorted JSON SHA-256:
`e7508a394b95995c719aa8ae85c8bf21852df922348718324f19b5654269c12b`.

## Focused regression and mutation evidence

[GitLab origin proof](113-review-round7-a.md): real SQL publication context, claim
guard and publisher cover active and unknown GitLab origins. Literal state/error,
project 101 gateway calls and zero GitHub/check-run calls are asserted. The restored
module passed 40 tests in 27.56s with zero failures/skips. GitLab always-active
and always-inactive mutations each produced 1 failure and 1 pass at the context
origin assertion (2.13s and 1.99s respectively); exact production bytes were restored.

[Waiting receipt proof](113-review-round7-b.md): retry/redelivery controls and
installation 17 retirement/rebind to 29 cover persisted receipt identities,
retry/attempt/claim/lease fields, old Run origin and literal message/provider-call
observations. Rebound cases cannot create a new-head Run or use installation 29.
The combined-fence mutation produced 2 failures and 2 passes in 9.87s: both rebound
failures observed a forbidden extra queued Run. The restored full module passed
42 tests in 99.73s, zero failures/skips. Exact production bytes were restored.

[Documentation proof](113-review-round7-c.md): SYSTEM_DESIGN requires future
GitLab writers to set immutable Run origin and clarify non-GitHub error wording
when ingestion is implemented. Historical round-five evidence identifies its
actual checkpoint; completed round-four integration checks are recorded with
round-five/six evidence. The concise PR body keeps current-head verification and
links every repository document at the published head.

## Repository gates and review

- `uv run ruff check .`: All checks passed.
- `uv run ruff format --check .`: 397 files already formatted.
- `uv run mypy .`: Success: no issues found in 394 source files.
- `uv run pytest -rs` with PostgreSQL/RabbitMQ configured: 3196 passed,
  2 existing dependency warnings in 697.43s (0:11:37), zero failures/skips.

Integration collection selected 549 / 3196 tests, with 2647 deselected in 1.44s.
All 549 integration cases ran in the full suite above; there was no separate local
integration execution.

Standards and Spec reviews at this checkpoint reported zero actionable findings
on each axis. These results do not verify any later integrated source.

## Publication and merge prerequisites

`git diff --check` and configured staged-file checks passed: trailing whitespace,
end of file, merge conflicts, Ruff check, Ruff format and mypy. YAML/TOML checks
had no applicable staged files. No hook changed a file; all 418 tested fingerprints
matched after the checks.

This unpublished checkpoint has no commit, review-response or required-CI receipt.
Later publication evidence belongs to the exact subsequent published head.

Squash only: intermediate commits have failing checks. Before merge, recheck main,
obtain current-head approval at the agreed time with @axyi and confirm reviewer-owned
thread resolution. Stop every old Run writer before merge/main auto-deploy. If main
advances, rebase and rerun gates on the new head before merge.
