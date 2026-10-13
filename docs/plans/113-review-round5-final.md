# Historical round-five final verification — PR #132 / #113

This proof records round-five checkpoint `b5e0cec92c24a8e702a90e75de5a10350f89cdaa`
on main `11ddfabf31a3345e6d21cdf57586281cb62cee7c`. Its results do not verify later
rebases or review corrections.

## Round-five combined source

Combined main: `11ddfabf31a3345e6d21cdf57586281cb62cee7c`, including merged
#121 and #126. Rebased parent: `b4fb4a24c4ff8cbcf6256c289565283c51b80851`,
plus the receipt-test adaptation and documentation corrections published at the
round-five checkpoint above.
The trigger retains `RunTriggerOutcome.detail`/`retry_required` and mandatory
keyword-only `expected_installation_external_id`. Upstream deployment and
AuthScope contracts are preserved.

The tested snapshot contains 412 tracked Python, runtime and configuration files.
Their SHA-256 fingerprints matched the round-five tested source after hooks and
commit. Canonical sorted JSON
SHA-256: `72d7f7838ba3161084d4917d323c379e25e4511c269ec879e32ce04b5896adb4`.
Later source changes require their own verification.

## Repository gates

- `uv run ruff check .`: All checks passed.
- `uv run ruff format --check .`: 391 files already formatted.
- `uv run mypy .`: Success: no issues found in 388 source files.
- `uv run pytest -rs` with PostgreSQL and RabbitMQ configured: 3132 passed,
  2 existing dependency warnings in 627.20s (0:10:27), zero failures/skips.

Integration collection selected 538 / 3132 tests, with 2594 deselected in 1.38s.
All 538 ran in the full suite above; there was no separate local integration run.
These results verify the round-five checkpoint only and are distinct from
round-four and later executions.

## Focused integration and review

The [PostgreSQL integration proof](113-review-round5-integration.md) records a
meaningful initial failure (`RunTriggerOutcome` treated as a string: 1 failed in
1.50s), its adaptation and two restored suites: 185 passed in 69.39s and 148 passed
in 84.23s, both with zero skips and 2 dependency deprecation warnings. The receipt
controls cover restart/redelivery, concurrent replay, superseded cancellation,
obsolete-head success and original-installation forwarding.

Independent Standards and Spec reviews of the combined-main-to-working-tree delta
reported zero actionable findings on each axis. The reviews include the integration
contract and both documentation corrections.

## Documentation corrections

The [documentation source proof](113-review-round5-docs.md) establishes the recovery
behavior by source inspection: an image missing 0030 fails bootstrap after that
migration and blocks API and both workers. Merged #126 rejects the incompatible
image before application-container or release-configuration changes; it does not
undo a failed deploy. Recovery requires a fix-forward image containing 0030.
NULL-origin insertion is limited to independent old writers outside Compose; only
the new worker cancels eligible nonterminal work. No old-image runtime reproduction
or production deployment is claimed.

Round-four publication steps are marked complete against historical head `8a87fe9`
and its successful CI. Historical counts, timings and SHAs remain distinct. The
corrected round-four documents were published at the round-five checkpoint.
UI #105 remains the separate frontend wording follow-up linked in API #106.

## Publication and remaining prerequisites

`git diff --check` and configured staged-file checks passed: trailing whitespace,
end of file, merge conflicts, Ruff check, Ruff format and mypy. YAML/TOML checks
had no applicable staged files. No hook changed a file; all 412 tested fingerprints
matched after the checks. Round-five publication completed at
`b5e0cec92c24a8e702a90e75de5a10350f89cdaa`; the
[review response](https://github.com/larchanka-training/dmc-268-api-t6/pull/132#issuecomment-6142077501)
was posted. All nine required jobs succeeded in
[CI run 38373267208](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/38373267208):
3132 full-suite passes in 599.18s and a separate 538 integration passes in 427.93s,
with zero failures/skips and 2 existing warnings in each execution. These receipts
verify that historical head and remain separate from the local results above.

At this checkpoint, current-head approval, reviewer-owned thread resolution and
writer shutdown at the agreed merge time remained external prerequisites. A main
push starts staging deployment, so old writers had to stop before merge. Later
heads require a fresh main check and their own verification.
