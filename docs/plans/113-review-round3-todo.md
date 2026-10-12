# PR #132 round-three checklist (#113)

Scope/criteria/ownership: [113-review-round3-plan.md](113-review-round3-plan.md).
Original approved plan and prior review plan remain applicable; all new source work is tests.

## Preparation

- [x] Read fresh 16:10 UTC review and three open P2 threads on 83a3629.
- [x] Verify clean authorized worktree and remote PR/main heads.
- [x] Audit current open-PR file owners; no foreign owner of planned tests.
- [x] Post narrow shared-doc coordination comments to #148/#137/#127/#124/#121/#126/#125.
- [x] Partition final and mutation ownership; give fresh developers brief paths.

## Developer A — reinstall proof

- [x] A1 reachable account-null OAuth → clean target → remove → add PG regression.
- [x] Existing target UUID/availability restored; archive/children/settings untouched.
- [x] Mutation removing existing-is-None fails; restore production bytes, focused green.
- [x] A2 overlapping opposite-order NULL-account callbacks exercise sorted row prelock.
- [x] Callback/sync control isolates sync sorted installation prelock.
- [x] Callback and sync prelock mutations separately fail; restore and rerun.
- [x] c-prime late deletion OAuth without listing; Workspace predicate mutation fails.
- [x] Save actual proof in 113-review-round3-a.md; no production changes remain.
  Six mutations red; restored focused suite: 31 passed, zero skips.

## Developer B — migration/lifecycle proof

- [x] B1 schema row108 paused+partial revocation+fresh grant → (108, False, True).
- [x] NOT EXISTS removal and freshness inversion each red; restored focused green.
- [x] B2 repo_removed prepare and reconcile/retry matrices, installation stays live.
- [x] Reconcile twice terminalizes queued/running/publishing, no emitted messages.
- [x] Owning-worker finish(failed) from running/publishing → cancelled/unavailable.
- [x] Availability-disjunct and finish-outcome mutations each red; source restored.
- [x] Save actual proof in 113-review-round3-b.md; preserve active/displaced controls.
  Five mutations red; restored focused suite: 82 passed, zero skips.

## Checkpoint 1

- [x] All blocking tests passed against unchanged production code.
- [x] Isolated mutations reproduced and reverted; no other tests overlapped mutations.

## Developer C — docs and metadata drafts

- [x] SD accepted incomplete-comment receipt race and external/local consequence.
- [x] WEBHOOK_WORKER clarifies PG-only deferral; tree/label network already possible.
- [x] CICD old Run writers stop BEFORE MERGE; automatic main deploy and timing stated.
- [x] Replace historical personal worktree/DSNs with portable placeholders.
- [x] Prepare accurate PR body with original/review/addendum plan and todo paths.
- [x] Verify rebase explanation covers #134/#108/#138; preserve foreign doc sections.
- [x] Save 113-review-round3-c.md with actual scope and validation.

## UI follow-up preparation

- [x] API #106 is the actual tracker; UI #106 absent.
- [x] UI issue list inspected; no narrow rerun 409 duplicate found.
- [x] UI create-issue preflight passes.
- [x] Save Russian spec and emitter-rendered full body; portable references/clear AC.
  Draft preparation complete; no UI issue or follow-up comment posted.
- [x] Present full preview after implementation/docs, while full gates ran; explicit Create required.
- [ ] Post follow-up/link only if user authorizes issue creation; otherwise leave draft.

## Checkpoint 2 — combined verification and independent review

- [x] All mutation windows ended; final production diff unchanged.
- [x] uv run ruff check . — All checks passed.
- [x] uv run ruff format --check . — 385 files already formatted.
- [x] uv run mypy . — Success: no issues found in 382 source files.
- [x] uv run pytest -rs: 2886 passed, 2 existing warnings in 534.49s, zero skips.
- [x] Fresh independent Standards and Spec reviews: zero actionable findings on both axes.
- [x] Record tested parent/base, source fingerprint and actual counts/timing in round-three final proof.

## Existing PR publication

- [x] Rebase onto newly merged #127 main 488475e; preserve SD/AuthScope and exact tested Python/runtime tree.
- [x] Rerun post-rebase Ruff/format/mypy (passed/385/382); pytest evidence remains the unchanged source snapshot.
- [x] Prepare intended files for authorized publication to existing #132; see PR timeline for outcome.
- [x] Prepare full body with rebase details, evidence and all plan/todo paths for publication.
- [x] Prepare three P2 replies with tests/mutation proof and P3 evidence; publish with exact SHA.
- [ ] Required CI green on newly published head; reviewer handles current-head approve.

Publication outcomes (head, body/reply links and re-request) are recorded in PR #132
timeline after this checklist snapshot. No external approval or CI result is predicted.

## External before-merge gates

- [x] #127 merged; #132 rebased onto its main 488475e with AuthScope text preserved.
- [ ] #125/#126/#121 landed; #132 rebased last with both #121 contracts preserved.
- [ ] Combined-head gates and waiting old-CI-receipt outcome verified.
- [ ] Reviewer resolves threads and approves exact current head.
- [ ] Stop every old Run writer BEFORE MERGE and agree merge timing with axyi.
- [ ] Tech lead handles merge/AC/issue closure; no Development-panel link.
