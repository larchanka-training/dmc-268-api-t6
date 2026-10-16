# Todo: issue #116 / existing PR #124

- [x] Read AGENTS.md, issue #116, PR #124 and current overlapping PR ownership.
- [x] Record coordination before shared-file edits on PRs #133, #148, #155, #159.
- [x] Save the plan and task checklist in the isolated worktree.
- [x] Rebase PR head onto latest origin/main; resolve conflicts while preserving current main behavior.
- [x] Verify complete static error in both eval_live baseline rejection tests, with tmp_path roots and existing preservation assertions.
- [x] Demonstrate a RED regression if `; preserve the existing baseline` is removed; restore and run focused eval_live GREEN.
- [x] Align current v2 replay/doc examples and links; preserve explicitly historical v1 entries.
- [x] Document exact brace-limit WARNING, 256 work units and per-rule/per-fit over-budget trigger.
- [x] Keep WEBHOOK_WORKER change formatting-only relative to current main.
- [x] Checkpoint: focused tests pass; no accidental baseline/prompt/runtime changes or unrelated checkout edits.
- [x] Start dedicated disposable PostgreSQL and RabbitMQ with explicit test URLs and isolated vhost.
- [x] `uv run ruff check .` passes.
- [x] `uv run ruff format --check .` passes.
- [x] `uv run mypy .` passes.
- [x] `uv run pytest` passes with zero skips; record counts.
- [x] `uv run pytest -m integration -rs` passes with zero skips; record counts.
- [x] Stop/remove only disposable verification services after checks.
- [x] Independent Standards review: zero findings, or all findings fixed and reviewed again.
- [x] Independent Spec review: zero findings, or all findings fixed and reviewed again.
- [x] Commit reviewed changes with issue reference; publish to existing PR #124 branch using verified lease.
- [x] Update PR body and AC/DoD report with exact heads, commands, counts and remaining human gates.
- [x] Confirm PR remains open/unmerged and issue #116 remains open; leave final acceptance to tech lead.

## Review round 2 (preapproved)

The original completion and Spec 0 statements above are historical. Reviewer P2
identified the third original short line still present; AC1 formatting completion
must be re-established by the following tasks.

- [x] Read exact P2 thread and existing plan, todo, verification and review evidence.
- [x] Save this preapproved follow-up plan/checklist; acknowledge the first review missed the third original short line.
- [x] Refresh main, remote PR head and open-PR ownership; coordinate any overlapping owner before edits.
- [x] Reflow only current WEBHOOK_WORKER 670–678 through paragraph end; preserve all whitespace tokens and inline-code spans.
- [x] Leave forced short line before `installation_repositories.added` near 662 unchanged.
- [x] Check every reviewed paragraph line, full-file tokens versus fresh main, and inline-code spans versus pre-edit head.
- [x] Correct prior completion overclaim in verification/review evidence and prepare matching PR/issue report corrections.
- [x] Read P3-1 in the review body and inspect seed activation, manual gateway defaults and CI gate/service/skip-guard steps.
- [x] Coordinate README overlap with PR #133 before editing.
- [x] Correct README active-version explanation: database seeding activates newest known key version; manual gateway defaults use same v2 files; retain v1 history and wrap lines.
- [x] Confirm all Python/test/dependency/prompt/baseline content is identical to b42195d and original tested a7eedf7.
- [ ] After publication, wait for green CI on the exact new PR head; do not substitute old-head results.
- [ ] Record CI run URL/head and all four uv gate results, actual full-pytest count and integration zero-skip guard evidence from dedicated CI PG/Rabbit service containers.
- [ ] If CI fails, investigate/fix and obtain new green-head evidence before completion; no redundant local full/integration rerun for Markdown-only changes.
- [x] Fresh independent Standards review has zero unresolved findings, explicitly covering full reviewed paragraph, tokens and inline code.
- [x] Fresh independent Spec review has zero unresolved findings, explicitly covering the previously missed third short line.
- [x] Publisher commits/pushes to same PR #124 and updates body plus every AC/DoD evidence item.
- [x] Reply to reviewer thread with fixing SHA and acknowledgement; also report P3-1 addressed; leave thread resolution to reviewer.
- [x] Publish correction of prior completion overclaim; keep current-head approve and tech-lead acceptance pending.
- [x] Confirm PR unmerged and issue open; do not change acceptance/tracker/board state.

The CI items above are pending at the publication checkpoint. Their final exact-head
run URL, gate results and counts are recorded in the existing
[PR AC/DoD report](https://github.com/larchanka-training/dmc-268-api-t6/pull/124#issuecomment-6217853912)
and [issue report](https://github.com/larchanka-training/dmc-268-api-t6/issues/116#issuecomment-6217854511).
Those external records are the final CI checklist, avoiding another documentation-only
push that would start a new head and invalidate the just-recorded run.
