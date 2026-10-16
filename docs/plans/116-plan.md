# Plan: finish issue #116 in PR #124

## Scope and decisions

Rebase the existing `chore/116-eval-docs-diagnostics` PR from known head
`fa7002e974b5b81077b3dc7bfa34e937eb4520fb` onto fresh `origin/main`
(currently `7546e539e1c1f565f65b9a5ca4cdc5dd1fbfb3fa`). Preserve changes
already merged into main, especially transport checkpoint forwarding, frozen
commit-context evaluation inputs, webhook lifecycle text and graph documentation.
Do not change review algorithms, published prompt versions, baseline captures,
dependencies, schema, or production behavior.

The six existing PR files are the scope: `review/scripts/eval_replay.py`,
`review/README.md`, `review/postprocess/lint-filter.md`, `docs/SYSTEM_DESIGN.md`,
`docs/WEBHOOK_WORKER.md`, and `tests/test_eval_live.py`. Planning and verification
artifacts accompany them. Work in the isolated managed worktree; leave unrelated
changes in the original checkout untouched.

## Ordered tasks

1. **Integrate current main** (medium; existing six-file PR).
   Fetch main and the PR head; confirm the remote PR head before rebase. Resolve
   conflicts by preserving current main contracts and reapplying only #116's
   documentation and assertion intent. Review the resulting main-to-branch diff.
   Coordination comments have already been posted before edits on open PRs #133,
   #148, #155 and #159; recheck ownership if scope changes. Verify no conflict
   markers and that `origin/main` is an ancestor.
2. **Pin complete diagnostics** (small; `tests/test_eval_live.py`).
   Keep exact string equality for both existing baseline rejection and a baseline
   created during capture: `responses directory is not empty; preserve the existing
   baseline`. Keep temporary roots provided by `tmp_path` and byte preservation,
   temporary directory cleanup and no-provider-call assertions. The error contains
   no dynamic path; do not invent a production path substitution. Preserve current
   `RunProviderCheckpoint` forwarding. Prove the regression test goes red when the
   trailing instruction is temporarily removed, restore production code, then run
   `uv run pytest tests/test_eval_live.py` green.
3. **Verify documentation against runtime** (medium; five documentation/example
   files). Use active `review.system.v2` and `review.conventions.v2`, with clearly
   labelled historical v1 files retained. Match replay manifest version examples
   and section references to the actual active files. Describe the 256-unit brace
   work cap and exact WARNING emitted once per ignored rule per budget fit only
   when prompt exceeds budget and priorities are computed. Reflow only the wake-up
   paragraph in WEBHOOK_WORKER; verify word content against current main is
   unchanged and preserve current lifecycle wording.
4. **Prove branch quality** (no implementation files).
   Provision dedicated disposable PostgreSQL and RabbitMQ, use an isolated RabbitMQ
   vhost and explicit `TEST_DATABASE_URL`/`TEST_RABBITMQ_URL`. Run all required gates:
   `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
   `uv run pytest`. Also run `uv run pytest -m integration -rs`; require zero skipped
   tests and record counts, service versions and commands. Never test against other
   local project services. Remove only the disposable services after verification.
5. **Independent review and same-PR publication**.
   Run separate Standards and Spec review through the code-review skill; resolve
   findings and rerun affected checks. Publisher commits with Conventional Commits
   and `Refs #116`, updates the same PR branch with an explicit lease if rebasing
   requires force, and updates PR #124 body (`What`, `Why`, `How to verify`, `Refs`).
   Report each AC and DoD separately, including exact tested and published heads,
   zero-skip evidence and pending human approval/acceptance. No new PR, merge,
   issue closure, Development-panel link or claim of tech-lead acceptance.

## Checkpoints and dependencies

- Task 1 precedes tasks 2–3; their shared files make edits sequential.
- After tasks 2–3: focused tests green, active references consistent, webhook diff
  formatting-only, original checkout unchanged.
- Task 4 precedes independent review and publication. New review fixes invalidate
  relevant evidence and require rerunning appropriate gates.
- Before publication: current main ancestry, remote lease, clean reviewed diff,
  ownership coordination and complete AC/DoD evidence.

## Risks and acceptance boundary

Conflicts must not restore stale main content; inspect each combined hunk rather
than accepting either whole side. Existing green gates with skips are insufficient;
missing services are setup failures, not acceptable evidence. WARNING text must
match logger behavior and its call site. Final approve on the current head,
reviewer-owned thread resolution and tech-lead acceptance/tracker/board updates
remain pending wherever they require those actors; leave issue #116 open.
