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

## Review round 2: complete the revival paragraph reflow

The user approved this follow-up plan in advance. Reviewer P2
[`discussion_r4298538731`](https://github.com/larchanka-training/dmc-268-api-t6/pull/124#discussion_r4298538731)
found the third original short line, `after removing and re-adding`, still present
at line 670 of published head `b42195dae68bb89908d33c0ab820204b2dc73322`.
The first independent review missed this omission: its Spec 0 result and prior
completion reports overstated AC1's formatting completion. Token equality proved
content preservation, but did not prove that every requested short line was reflowed.

1. Refresh main, remote PR head and open-PR file ownership before editing; preserve
   any intervening changes and record coordination if an overlapping file has an
   owner. The coordinator performs this check and inspects the dedicated CI services.
2. Development reflows only current WEBHOOK_WORKER lines 670–678 through the
   paragraph end at approximately the existing 92-column width. Preserve every
   whitespace-delimited token and keep complete inline-code spans on one line,
   allowing an overlong atomic span when required. Leave the forced shorter line
   near 662 before `installation_repositories.added` unchanged. Check the whole
   reviewed paragraph line by line, all file tokens against fresh main, and
   inline-code spans against the pre-edit head. Update verification evidence and
   correct the prior review overclaim without presenting the old review as proof
   of this newly repaired omission.
3. Address review-body P3-1 in `review/README.md` lines 27–28 after coordination
   with PR #133. Explain that `app/bootstrap/seed_prompts.py` activates the newest
   known version for each key in `prompt_versions` by setting `is_active`. The
   manual gateway check in `app/bootstrap/llm_gateway.py`
   defaults to the same v2 files. Retain v1 as immutable history and wrap the
   explanation to paragraph width. Verify wording against those call sites;
   no runtime change or invented activation mechanism.
4. Run fresh independent Standards and Spec reviews. Specifically inspect every
   remaining line in the reviewed paragraph, token equality and intact inline code;
   do not infer layout completion from token equality alone. Fix any findings and
   refresh evidence as needed, then replace/supplement `116-review.md` with the
   follow-up result and clear acknowledgement of the previous miss.
5. Publisher commits and pushes to existing PR #124, updates its body and per-AC/DoD
   evidence, and replies to the P2 thread with the fixing SHA, also reporting P3-1.
   Correct prior PR/issue report overclaims with a follow-up acknowledgement; never
   resolve the reviewer's thread. Record pending current-head approve and reviewer
   resolution. No merge, issue closure or tech-lead acceptance/tracker/board change.
6. Follow the reviewer's explicit verification condition: for Markdown-only fixes,
   fresh green CI on the published head supplies the four gates. Prove Python/test
   identity versus `b42195d` (and original tested `a7eedf7`) and do not repeat the
   long local full/integration suites. Inspected `.github/workflows/ci-cd.yml`:
   `Python lint / type / test` runs `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy .`, full `uv run pytest` with
   coverage, and a separate integration JUnit zero-skip guard, using dedicated
   PostgreSQL 17/RabbitMQ 4 service containers. Wait for current-head CI to pass;
   record the exact SHA, run URL, actual pytest counts and zero-skip evidence in
   the verification/PR report before declaring completion. If CI fails, investigate,
   fix within scope and obtain green evidence again. Never infer a new head's
   successful gates solely from the previous head. No redundant local services
   are needed in this round.

Only `docs/WEBHOOK_WORKER.md`, `review/README.md` and the existing
planning/review/verification artifacts are edited in this round. No runtime, test,
prompt, baseline or dependency change. This CI-based verification supersedes the
local gate rerun specified in the first draft of this round-2 addendum.
