# Tasks: worker documentation and historical environment traces (#208)

Plan/inventory: [208-plan.md](208-plan.md). Initial base: `22c621ab`;
current base: `1ae74b1ae8fb48aed9dc4d9e913bf4c916d5a813`.
Status: approved by the user with “Ок” on 2026-10-24; Task 1 review passed;
initial implementation published as PR #260. Review round 1 corrections are
implemented and current gates pass; Spec review, the final aggregate verdict and
publisher handoff remain pending.
The completed Tasks 1–3 below describe initial publication, not current-round gates.

## Approval and coordination

- [x] Read issue/latest clarification, repository rules and planning skills.
- [x] Inspect current source/specs and tracked plans; record inventory/exceptions.
- [x] Human approves saved plan and exact #220 coordination comment (“Ок”, 2026-10-24).
- [x] Refresh main/open PR ownership; post authorized #220 comment before editing
      its worker document; record
      [coordination link](https://github.com/larchanka-training/dmc-268-api-t6/pull/220#issuecomment-6382744717).
      Keep #227's `97-*` unchanged.
- [x] Coordinate new overlaps before editing; import no open-branch plans.
      No new ownership overlap was found within the approved inventory.

## Task 1: Correct worker explanations

Depends on approval/posted coordination; one worker document.

- [x] Preserve A's promise; describe conditional `details_url` to A's Run and no
      summary link; remove stranded word and unconditional no-link claim.
- [x] Distinguish both PR/label refusal reasons and their precedence; retain
      current deferral policy and adjacent waiting/rerun behavior.
- [x] Verify final paragraphs against current code/canonical specs; review diff.

Before-edit acceptance assertions failed for all three missing requirements:
conditional Run URL/summary distinction, both refusal reasons/precedence, and
removal of the stranded line/unconditional no-link claim. The same assertions
pass after editing. Source and canonical-spec review confirms cancelled summary
content, optional payload `details_url`, projection precedence, unchanged dispatch
deferral and PIPELINE_SPEC §6.1 rerun behavior. Exact replacement comparison
confirms that all other worker text is unchanged; `git diff --check` passes.
The required full gates pass under Task 3.
Task 1 independent review: **Standards 0 / Spec 0**; coordinator authorized Task 2.

## Task 2: Sanitize historical traces

Depends on approval/ownership. Batch file lists are in the plan; each touches at
most five existing files. Check full paragraphs/commands, unchanged numeric
proof/hashes/statuses and `git diff --check` after each batch.

- [x] A: #81 and #82 files.
- [x] B: rollback verifier and #111 files.
- [x] B JSON parses; numeric leaves/types/nesting and repeated references agree;
      stock reports/checksums and self-creating procedures preserved.
- [x] Checkpoint B: cumulative diff/exceptions reviewed.
- [x] C: #116, #129 and #141 files.
- [x] D: #142 and #143 files.
- [x] Checkpoint D: setup/connection/cleanup references portable and consistent.
- [x] E: #145, #151 and #162 files; neighboring origin-lock evidence untouched.
- [x] F: #178 and #206 files.
- [x] G: #207 files, including bare temporary-directory prose.
- [x] Checkpoint G: complete inventory handled; historical evidence intact.
- [x] H: correct both P3 checkout-reference misses; sanitize newly merged
      `168-withheld-files-*` traces after the conflict-free main rebase.
- [x] I: generalize managed-worktree basename and private backup directory;
      preserve branch/hash, backup date and public provenance.
- [x] Final inventory: 32 historical documents, worker and two new plans
      (35 documents); preserve existing worker/proof edits and exclusions.

Task 2 evidence: all 32 historical documents pass hash/result/duration/status
preservation and code-fence checks; each batch passes `git diff --check`.
The initial issue environment scan changed from 129 lines/28 files to
17 lines/8 files. Refreshed main adds two tracked plans: all 103 tracked documents
were re-audited, with 133 lines/30 files before and 17 lines/8 files after;
all remaining matches are allowed rollback procedure evidence or public hosts/
GitHub sandbox references. Supplemental directory/socket/port/resource checks
found no further machine-specific trace. JSON numeric/boolean leaves, types,
nesting and repeated resource references are preserved; stock reports/procedure
and excluded documents are byte-identical. See the plan's Task 2 checkpoints.
Production/test/migration/contract/dependency diffs are empty. Task 1 worker text
is unchanged since its successful review. Full gates pass under Task 3.
The first Task 2 review reported **Standards 0 / Spec 2 P3**. Both missed checkout
references are fixed; the additional merged plans retain all counts, statuses and
failure/staging guidance. The refreshed-main review reported **Standards 0 /
Spec 2 P3** for a worktree basename and private backup directory; both are fixed,
with backup date/public provenance preserved. Final independent review reports
**Standards 0 / Spec 0**.

## Task 3: Verify scope and hand off

Depends on Tasks 1–2; save portable evidence in these two plans only.

- [x] Compare issue Kickoff searches before/after; supplement directory/socket,
      prose-port and container/resource searches. Inspect all remaining hits
      and newly merged plans; no unapproved machine-specific trace remains.
- [x] Worker wording, numerical evidence and JSON verification pass.
- [x] `git diff --check` passes; production/test/migration diff empty; complete
      changed-file list contains only expected documents.
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [x] `uv run pytest`; record actual counts and integration availability/skips.
- [x] No implementation tests, dependencies or API/config changes added.
- [x] Independent Standards/Spec review reports zero findings; resolve findings
      within docs-only scope and repeat affected checks.
- [x] Publisher receives reviewed diff, actual gates, coordination link and
      `Refs #208`; publication/merge follows repository workflow.

Initial-publication review checkpoint on base
`25c83fe0d024270e9a744850baa9721cb0ede617`, published head `7335cf9`:
independent **Standards 0 / Spec 0**; publisher received the reviewed diff,
actual gates, coordination link and
`Refs #208`. That handoff is complete. Its initial publication default was draft;
the now-published PR #260 is non-draft and keeps that status during corrections.

Original-base gates completed before rebase: Ruff lint PASS; format PASS
(533 files); mypy PASS (530 source files); pytest **4324 passed, 961 skipped,
2 warnings in 309.64s**, exit 0, integration URLs absent. This is completed
baseline evidence only.

Initial-publication gates reported by the coordinator on
`25c83fe0d024270e9a744850baa9721cb0ede617`, using a separate runtime matching
`uv.lock`: Ruff lint PASS; format PASS (534 files); mypy PASS (531 source files);
pytest **4340 passed, 961 skipped, 2 warnings in 443.80s**, exit 0.
`TEST_DATABASE_URL`, `TEST_RABBITMQ_URL` and `TEST_REDIS_URL` were absent; skipped
integrations were not executed. Pytest XML/log were retained privately. No shared
environment change or duplicate full gate run was made for later prose fixes.
Initial-publication preservation and docs-only scope checks passed; independent
review and publisher handoff completed for that publication. The review-round
section below supersedes those historical completion statements.

## PR #260 review round 1

Continues the approved review/development loop from head `7335cf9`; no new
plan-approval stop. Current base is `1ae74b1ae8fb48aed9dc4d9e913bf4c916d5a813`.

- [x] Read the saved review/thread and establish before-edit acceptance failures.
- [x] P2: rebase the existing branch without conflict; sanitize newly merged
      `168-ast-parse-errors-*` traces without changing results or task statuses.
- [x] P3.1–3: fix the UI abbreviation and backup-stash label; align the
      `113-review-c.md` placeholder with its family convention.
- [x] P3.4: restore actual uv cache/environment mechanisms with portable values;
      restore the original-checkout assurance; invent no mechanism in `111-todo.md`.
- [x] P3.5–6: distinguish fresh/existing ui#106 checkouts and reflow the stranded
      integration-skip line.
- [x] P3.7: deliberately keep `<gate-project>` aliases; preserve prerequisite,
      live/offline and stock-report evidence. The review permits this choice.
- [x] Optional P3.8: generalize the sibling checkout in `140-plan.md`; withdraw
      the prior exception rationale.
- [x] Optional P3.9 disposition: no table change this round; the corrected
      “Superseded heads” paragraph satisfies AC2. The optional table expansion
      can be coordinated separately if authorized by the user.
- [x] Audit actual current main and branch: 107/109 plan documents;
      137 lines/32 files before and 17 permitted lines/8 files after.
- [x] Scope: 35 historical documents + worker + two new plans = 38 documents;
      `219-*`, excluded documents, reviewed worker and stock proofs unchanged.
- [x] Acceptance and preservation checks pass; `git diff --check` passes;
      application/test/migration/contract/dependency diff is empty.
- [x] Current-base `uv run ruff check .`: PASS.
- [x] Current-base `uv run ruff format --check .`: PASS, 544 files.
- [x] Current-base `uv run mypy .`: PASS, 539 source files.
- [x] Current-base `uv run pytest`: PASS, 4679 passed, 985 skipped, 2 warnings
      in 313.64s; exit 0. `TEST_DATABASE_URL` and `TEST_RABBITMQ_URL` absent;
      skipped integration cases were not executed. Log/XML retained privately.
- [x] Fresh independent Standards/Spec review reports zero findings.
- [x] Publisher receives the reviewed diff, actual gates, updated inventory,
      coordination link and review-point dispositions for the existing PR.
      Publication replies use the actual commit/rationales, leave thread
      resolution to the reviewer and re-request that reviewer.

The review-point dispositions and current-base evidence are in the plan's review
round section. All eight before-edit acceptance failures now pass. Historical
results, hashes, durations, statuses and fences remain unchanged; JSON and stock
proof bytes remain intact. No code/test/config/dependency change or duplicate
gate run was made. Optional P3.9 does not block the other authorized corrections.
All current-round gates pass. Fresh independent review reports Standards 0 /
Spec 0; the aggregate verdict has no actionable findings. Publisher handoff is
complete; publication progress and review responses are recorded on PR #260.
