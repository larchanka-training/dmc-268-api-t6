# Plan: worker documentation and historical environment traces (#208)

## Scope and decisions

Docs-only work initially based on main `22c621ab`, issue #208 and its 23 October
clarification; refreshed onto main `25c83fe0d024270e9a744850baa9721cb0ede617`
after PR #256 merged. Review round 1 rebases the existing PR #260 onto
`1ae74b1ae8fb48aed9dc4d9e913bf4c916d5a813`. Review round 2 rebases onto
`24910a0d281d9d71e8362bc89e1925c8c37398bf`, then `05a96bc422468a570bab3233b11b538eb4a87ff6` and main
`74b42a7134fb9f272d93c913344e5b826b03b523`, then current main
`4faa03dd7accfa161048f584b834b314fa1ab5b6` after source changes and new plans
landed. Upstream source remains unmodified. Correct two paragraphs in `docs/WEBHOOK_WORKER.md`; sanitize
machine-specific references throughout tracked `docs/plans/`, including JSON
metadata. Preserve historical counts, durations, warnings/skips, hashes,
revisions, task statuses and unrelated prose. No application, test, migration,
contract, dependency or deployment changes; no new tests for prose.

Follow the #109 cleanup precedent (`30260e1f`) and `109-plan.md:120`.
Use portable inputs such as `TEST_DATABASE_URL=<disposable-db-url>` and
`TEST_RABBITMQ_URL=<isolated-task-vhost-url>`. Remove obsolete log locations;
use `<private-output>` when a command needs an output argument. Describe
unavailable temporary helpers historically rather than claiming they are
checked-in scripts. Generalize sandbox workarounds consistently with #109,
retaining both failed and successful run results.

## Verified behavior

- `app/modules/reviews/application/check_runs.py` sends `report.run_url` as
  `details_url` for cancelled Runs; the summary is the reason alone. The gateway
  omits the key without a URL. Existing worker tests cover cancelled/superseded
  with and without a URL. PIPELINE_SPEC §7 and SYSTEM_DESIGN §8.3 agree already.
- `github_pull_request_projection.py` distinguishes another stored installation
  from a deleted receipt installation. Another stored installation takes
  precedence if both apply. Both refusals dispatch as
  `ignored_unknown_repository` with different details.

## Inventory and bounded cleanup batches

The initial audit covered 101 tracked plan/evidence files; the issue scan found
129 matching lines in 28 files, including valid matches. Review and the refreshed
main audit expanded coverage to 103 tracked files and cleanup to 32 existing
plan/evidence files (35 documents including worker and these plans) for initial
publication. Review round 1 audits 107 current-main plan/evidence documents and
109 branch documents including these plans. Cleanup now covers 35 historical
documents, worker documentation and these two plans: 38 changed documents.
Supplemental inspection covers plain-prose service ports, gate resource names,
relative and abbreviated checkout references, and newly merged plans. Paths below are
relative to `docs/plans/`; each batch touches at most five existing files.

| Batch | Files | Context |
| --- | --- | --- |
| A | `81-plan.md`, `81-verification.md`, `82-plan.md`, `82-todo.md` | Checkout, container/database/endpoint; replay/log paths and copied response directory. |
| B | `110-verifier-plan.md`, `110-verifier-verification.md`, `110-verifier-verification.json`, `111-plan.md`, `111-todo.md` | Checkout; separate gate resource names/cleanup references; sandbox-bind notes, mutation helpers/copies, logs/cache/backups. |
| C | `116-verification.md`, `129-todo.md`, `141-plan.md`, `141-todo.md` | Proof/log paths, DB/broker URLs, project, prose service ports and named database/vhost. |
| D | `142-plan.md`, `142-todo.md`, `143-plan.md`, `143-todo.md` | Named DB/vhost references in setup, connection commands and cleanup. |
| E | `145-plan.md`, `145-todo.md`, `151-plan.md`, `151-verification.md`, `162-todo.md` | Named database, host port inventory, historical containers and log path. |
| F | `178-plan.md`, `178-todo.md`, `206-plan.md`, `206-todo.md` | Evidence/cache/runtime paths, seed container/endpoints, checkout and old/final evidence locations. |
| G | `207-plan.md`, `207-todo.md` | Checkout, Docker socket and JUnit path, including bare temporary-directory prose. |
| H | `113-review-c.md`, `145-plan.md` (revisit), `168-withheld-files-plan.md`, `168-withheld-files-todo.md` | Review misses and refreshed-main additions: relative/abbreviated checkout references, named database/vhost, local URLs/temp directory, shell-wrapper location and concrete shared-clone exclusion. |
| I | `185-plan.md`, `178-todo.md` (revisit) | Follow-up review: concrete managed-worktree basename and private backup directory; retain the backup date and public provenance link. |
| J | `168-ast-parse-errors-plan.md`, `168-ast-parse-errors-todo.md`, `145-plan.md`, `129-todo.md`, `113-review-c.md` | PR #260 review: newly merged traces, remaining checkout/stash abbreviations, 113-family convention and fresh/existing UI checkout distinction. |
| K | `178-plan.md`, `178-todo.md`, `111-todo.md`, `162-todo.md`, `140-plan.md` | Restore actual uv mechanisms with portable values, reflow the stranded line, and generalize the optional sibling-checkout example. |

Preserve public portal/staging hosts and GitHub sandbox references in `111-*`,
`142-*`, `143-*`. Preserve `110-verification.md`'s complete image-build and
self-creating verifier procedure. Inspect rollback JSON in context: generated
live-project/image identifiers and fixture sentinels describe that procedure;
explicit separate gate infrastructure names need consistent placeholders.
Preserve stock reports byte-for-byte and keep checksum claims valid; retain
immutable hashes, numeric results, JSON shape/types and audit outcomes.
Generic URLs/isolation guidance, image versions and source line references are
valid. Leave `151-origin-locks-*` unchanged. Re-audit all other tracked plans and
newly merged plans during development; do not rely solely on the issue examples.

## Ownership and coordination

PR #220 owns `docs/WEBHOOK_WORKER.md`. The user approved this plan and the exact
comment below with “Ок” on 2026-10-24. The authorized
[coordination comment](https://github.com/larchanka-training/dmc-268-api-t6/pull/220#issuecomment-6382744717)
was posted before editing:

> Coordinating docs-only work for #208: I plan to edit only the two paragraphs
> in `docs/WEBHOOK_WORKER.md` under “Superseded heads” that describe A's check-run
> link and PR/label deferral after rebind. The first will retain the existing
> supersession message and distinguish `details_url` to A's Run when `PORTAL_URL`
> is configured from the absent summary link. The second will distinguish
> `other_installation_repository` from `disabled_repository`, matching current
> projection code. Waiting/retry behavior and product code stay unchanged. I
> will recheck your latest diff and reconcile these paragraphs after rebase
> before review.

PR #227's `97-plan.md` and `97-todo.md` contain no applicable traces; no edits or
comment there proposed. The issue clarification withdrew #207/#142 merge
coordination. Refresh all open PR file ownership; any newly required overlap
needs its own coordination comment before editing. Do not import open-branch
plans for #168, #144, #219, #171, #170 or #169. Planning posted no comments.

## Ordered tasks and acceptance checks

### Task 1: Correct worker explanations (small: one file)

**Depends on:** plan approval and authorized, posted #220 comment.

- A's promise remains; configured `PORTAL_URL` supplies `details_url` to A's Run,
  while the summary has no link. Remove the stranded word and unconditional
  no-link claim; do not claim a URL exists without configuration.
- PR/label refusal distinguishes another stored installation
  (`other_installation_repository`) from deleted receipt installation
  (`disabled_repository`) with the code's precedence and existing deferral policy.
- Verify before/after paragraphs against check-run view/payload,
  projection/dispatcher and canonical specs. Preserve adjacent waiting, expiry,
  rerun and check-run text; check rerun wording against PIPELINE_SPEC §6.1.

### Task 2: Sanitize historical traces (small/medium batches above)

**Depends on:** plan approval and refreshed ownership. Process batches A–G
sequentially. For each, inspect complete changed paragraphs/commands/fences,
confirm every trace class handled, compare numeric proof/hashes/statuses against
base, and run `git diff --check`. Commands must retain portable inputs.
For B, parse edited JSON using `uv run python -m json.tool`, compare numeric
leaves/types/nesting and repeated resource references, and verify unchanged
stock-report bytes/checksums. Checkpoint after B, D and G: review cumulative
diff against inventory and preservation exceptions.

### Task 3: Verify scope and hand off (small: these two planning files)

**Depends on:** Tasks 1–2.

- Record issue Kickoff searches before editing, repeat afterward and inspect
  every remaining hit. Supplement with bare temporary-directory prose, Docker
  sockets, plain-prose service ports and container/resource names. Inspect all
  tracked plans, including newly merged ones; only justified exceptions remain.
- Require `git diff --check`, empty
  `git diff origin/main -- app/ tests/ alembic/`, and a complete changed-file
  list limited to expected documents. Explain any base-driven inventory revision.
- Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
  `uv run pytest`. Record actual results/skips; never claim skipped integration
  cases executed. Use owned disposable services if configuring integrations.
  Fresh independent Standards/Spec review must report zero findings before
  publisher handoff, with coordination link and `Refs #208`.

## Approval checkpoint

The completion statements in this and the following initial-publication
checkpoints concern reviewed head `7335cf9`; subsequent correction status and
verification are recorded in the review-round sections below.

The user confirmed this saved plan and #220 coordination comment with “Ок” on
2026-10-24; coordination is posted at the link above. Task 1 review reported
Standards 0 / Spec 0. Task 2's first review reported Standards 0 / Spec 2 P3
findings; the refreshed-main review reported two further P3 trace findings.
All four are corrected. Task 3 verification and gates pass; final independent
review reports Standards 0 / Spec 0, and publisher handoff is complete.
Concurrency and evidence loss are the principal
risks; ownership rechecks, bounded batches and numerical comparisons address them.
No unresolved product question.

## Task 2 checkpoints (2026-10-24)

All seven bounded batches are implemented: A/B/C/D/E/F/G changed
4/5/4/4/5/4/2 existing documents. Checkpoints B, D and G passed cumulative
diff/exceptions review and `git diff --check`. The initial 101-file audit at
`22c621ab` produced a 28-file cleanup; the first independent Task 2 review found
two missed historical checkout references in `113-review-c.md` and `145-plan.md`.
Both are corrected with portable placeholders. The latter file was already in
scope; the former adds one document to the inventory.

Main then advanced through PR #256. A scoped stash, rebase onto
`25c83fe0d024270e9a744850baa9721cb0ede617`, and restore completed without conflict
or loss of the existing documentation edits. The newly merged
`168-withheld-files-plan.md` and `168-withheld-files-todo.md` add two more documents.
Their checkout/database/vhost/loopback/temp traces are generalized. The concrete
shared-clone exclusion claim becomes conditional staging guidance, retaining
`.git/info/exclude`, `git add -f` and its original checkbox state. The local shell
wrapper location is generalized while its recorded failure cause/counts remain.
Batch H touches four files, including the revisit, and passes preservation/diff
checks. Cleanup now covers 31 historical documents; total scope is 34 documents.
No other source, test, migration, contract or dependency change.

The refreshed-main review reported Standards 0 / Spec 2 P3: a concrete managed
worktree basename in `185-plan.md` and a private backup directory in
`178-todo.md`. Batch I generalizes both, retaining the branch/hash, backup date
2026-10-15 and separate public provenance link. It adds one historical document
and revisits one already edited document. The final inventory is 32 historical
documents plus worker documentation and the two new plans: 35 documents.
The broader worktree/checkout/backup audit found no further actual local trace
at that checkpoint; the later PR review found two more abbreviations, now handled
in review round 1. Branch names remain unchanged.

The initial environment search found 129 matching lines in 28 files before
editing. The refreshed-main audit covers all 103 tracked plan/evidence documents:
133 matching lines in 30 files before editing, 17 lines in 8 files afterward.
Every remaining match was inspected:
7 belong to the allowed self-creating rollback image procedure/invocation and
its cleanup evidence; 10 are public portal/staging hosts or GitHub sandbox
references. Supplemental checks covered bare temporary-directory prose, sockets,
plain-prose ports, relative checkout/paperwork paths and separate service resources.
The previously retained sibling-checkout example in `140-plan.md` is generalized
for consistency in review round 1; it is no longer a cleanup exception.
The `97-*` and `151-origin-locks-*` exclusions are byte-identical to base.

All 32 edited historical documents retain content/provenance hashes, recorded
result fragments, durations, checkbox statuses and code-fence counts. Complete
changed paragraphs and commands were reviewed. Temporary mutation/audit helpers
are described historically; required reproduction commands retain portable inputs.
Failed, interrupted and successful historical runs remain distinct with their
original counts, warnings/skips and exit statuses.

The edited rollback metadata parses with `uv run python -m json.tool`. Exact
mapping comparison allows only the separate gate project's identifier and two
local Docker volume identifiers, consistently across references and audit keys.
Numeric/boolean leaves, types, nesting and content/provenance hashes are unchanged.
The stock `110-verifier-live.json` remains byte-identical with SHA-256
`d79ffbbb05dd822f231377c28b4673c8ef34839bd5eb10e4eaa6cb56295b28a3`;
`110-verification.md` and `110-verification.json` are also byte-identical,
preserving the complete self-creating procedure and checksum claims.

Task 1's worker diff is unchanged. All required gates pass under Task 3; final
independent review reports Standards 0 / Spec 0, and publisher handoff is complete.
Historical results in
the edited documents are preservation evidence; this change's gate results are
recorded separately below.

## Gate baseline before refreshed main

The coordinator completed all four gates on the original `22c621ab` base before
the rebase: Ruff lint passed; format passed (533 files); mypy passed (530 source
files); pytest passed with **4324 passed, 961 skipped, 2 warnings in 309.64s**,
exit 0. Integration service URLs were absent. This run completed; it was not
aborted. These are baseline results only, superseded by the refreshed-main gate
results below.

## Initial-publication verification on refreshed main

Base: `25c83fe0d024270e9a744850baa9721cb0ede617`. The coordinator used a separate
temporary runtime matching `uv.lock`; the original shared environment was not
modified. All four required gates completed successfully:

| Command | Result |
| --- | --- |
| `uv run ruff check .` | PASS |
| `uv run ruff format --check .` | PASS, 534 files |
| `uv run mypy .` | PASS, 531 source files |
| `uv run pytest` | PASS, 4340 passed, 961 skipped, 2 warnings in 443.80s; exit 0 |

`TEST_DATABASE_URL`, `TEST_RABBITMQ_URL` and `TEST_REDIS_URL` were absent.
The 961 skipped cases are not evidence that integration scenarios executed.
The coordinator retained the pytest log/XML privately. Subsequent fixes changed
only prose; no duplicate full test run was needed. Final preservation/scope
checks and `git diff --check` pass, including empty application/test/migration/
contract/dependency diffs. Final independent review reports Standards 0 / Spec 0
on this base, and the publisher received the reviewed diff, actual gate results,
coordination link and `Refs #208`. That handoff is complete; its initial default
was draft. The now-published PR #260 is non-draft.

## PR #260 review round 1 (completed; historical)

The outcomes below belong to base `1ae74b1` and published head `4f6d244`.
The latest reviewer approved these changes and resolved the blocking thread.
Current round-2 status follows this historical section.

This continues the user-approved review/development loop. The
[review](https://github.com/larchanka-training/dmc-268-api-t6/pull/260)
and [blocking thread](https://github.com/larchanka-training/dmc-268-api-t6/pull/260#discussion_r4367495716)
refer to published head `7335cf9adc54fd2965cebd25618cc751c1562f41`.
The existing branch rebased without conflict onto then-current main
`1ae74b1ae8fb48aed9dc4d9e913bf4c916d5a813`; the rebase did not affect the
original shared checkout or unrelated files. The existing PR remains non-draft and retains
its assigned reviewer when the publisher updates it.

| Review point | Disposition |
| --- | --- |
| P2 | Rebase complete; newly merged `168-ast-parse-errors-*` checkout, DB/vhost, URL and temporary-directory traces use the established portable inputs. Concrete local Git-exclusion guidance is conditional while retaining `git add -f`. Historical results/dates/hashes/statuses are preserved. |
| P3.1 | The remaining UI checkout abbreviation in `145-plan.md` uses `<isolated-ui-checkout>`. |
| P3.2 | `129-todo.md` describes a backup stash generically; the nearby full source/base hash and outcomes remain unchanged. |
| P3.3 | `113-review-c.md` uses the documented family convention `<worktree>`. |
| P3.4 | `178-plan.md`/`178-todo.md` retain `UV_CACHE_DIR=<task-private-cache>` and `UV_PROJECT_ENVIRONMENT=<existing-virtualenv>`; `111-todo.md` retains only the cache variable its original evidence used. The assurance about unchanged original checkout files is restored. |
| P3.5 | `145-plan.md` explicitly distinguishes a fresh checkout at the ui#106 head from the existing ui#106 checkout. |
| P3.6 | The stranded integration-skip line in `162-todo.md` is reflowed. |
| P3.7 | Deliberate no-change: keep the consistent `<gate-project>` aliases, which the reviewer permits. They describe the separate gate infrastructure. Prerequisite/live/offline evidence and stock reports remain untouched. |
| Optional P3.8 | Generalize `140-plan.md` to `<isolated-checkout>`; withdraw its prior exception rationale. |
| Optional P3.9 | No change this round: the corrected “Superseded heads” paragraph satisfies AC2. The optional projection-table expansion can be coordinated separately if the user authorizes it; the table remains unchanged. This optional suggestion does not block the authorized corrections. |

All eight before-edit acceptance checks failed for the reported omissions and
pass after correction. Source versions were inspected before restoring uv
mechanisms, so no usage was invented. Batches J/K touch five documents each.
The issue scan on actual current main finds 137 lines in 32 files; the corrected
branch finds 17 lines in 8 files. All 107 current-main plans and 109 branch plans
were audited, including `219-*`, which are unchanged. Remaining matches are the
same 7 permitted rollback-procedure entries and 10 public staging/sandbox entries.
Supplemental checkout/stash/backup/socket/port/resource checks find no further
applicable trace. The updated PR inventory is 38 documents: 35 historical
documents, worker documentation and these two new planning files.

Preservation checks pass for all changed historical documents: content/provenance
hashes, result fragments, durations, checkbox statuses and code fences. The
reviewed worker changes, JSON gate aliases, prerequisite/live/offline evidence,
stock-report bytes/checksums and excluded documents are unchanged during this
round. `git diff --check` passes; application/test/migration/contract/dependency
diffs against current main are empty.

The coordinator runs gates in its separate locked runtime on this current base:

| Command | Round-1 result |
| --- | --- |
| `uv run ruff check .` | PASS |
| `uv run ruff format --check .` | PASS, 544 files |
| `uv run mypy .` | PASS, 539 source files |
| `uv run pytest` | PASS, 4679 passed, 985 skipped, 2 warnings in 313.64s; exit 0 |

The earlier test counts belong to prior bases and are not current-round evidence.
`TEST_DATABASE_URL` and `TEST_RABBITMQ_URL` were absent; skipped integration
cases were not executed. The coordinator retained the current-round log/XML
privately. Fresh independent review reports Standards 0 / Spec 0; the aggregate
verdict has no actionable findings. The publisher received the reviewed diff,
actual gates, updated inventory, coordination link and review-point dispositions.
The publisher updates the existing PR, preserves its current non-draft status
and reviewer, replies to the blocking thread and numbered points with the actual
correction commit or rationale, leaves thread resolution to the reviewer, and
re-requests the assigned reviewer. No merge or issue closure is authorized here.

## PR #260 review round 2 (2026-10-25)

The latest review approves head `4f6d244f36e5279526f94456a7030c4489b5aff4`,
accepts the optional worker-table deferral and resolves the blocking thread.
Only the required rebase and PR-body missing space remain. Both documentation
commits first rebased onto `f445589a7bc5b339ee9e14c741149abd5ce14e08`, then
without conflict onto `24910a0d281d9d71e8362bc89e1925c8c37398bf` after main
advanced again. Source, tests, workflows and lockfile are unchanged between those
bases. `git range-diff` still reports `=` for both replayed patches; the latest
rebased head is `69246bddf594dc5f0c8d4cffa8ef7ad2ee0e0170` before bookkeeping.

At the preceding `24910a0d` base, the main-branch `docs/plans/` delta from
`1ae74b1` was empty. Its 107 main/109 branch plan inventory and scan evidence
were unchanged. The latest integration audit is recorded below.
No unaffected document was re-audited or edited. The initial private body draft
corrected only `to17` to `to 17` in the saved actual body. The final draft also
refreshes the latest base, gates and observed macOS/Linux verification outcomes.
Worker text/table, gate aliases, stock reports and accepted evidence are intact;
source/test/config/dependency diffs remain empty and `git diff --check` passes.

All four required gates passed on `24910a0d`, using separate locked runtimes and
Linux for pytest; the earlier macOS failure is retained below.
That checkpoint's completion is superseded by the latest integration evidence below.
Prior counts above remain historical. The publisher preserves the existing
non-draft PR and reviewer, pushes with the explicit expected prior head lease,
updates the body, replies to this review and re-requests its verified author,
since the rebase invalidates the old approval. No worker-table coordination,
extra approval request, merge or issue closure belongs to this round.

The macOS full pytest run on preceding base `f445589a` exited **1**: **2 failed, 4931 passed,
985 skipped, 2 warnings in 718.14s**. Both failures are
`test_validate_uses_the_profile_of_the_selected_corpus[main-legacy]` and
`[main-none]` in `tests/test_eval_live_workflow.py`, with
`/bin/bash: line 8: profile[@]: unbound variable`. This is a failed gate.

Clean `f445589a` Git-object snapshots of the test and workflow reproduce the
same two failures: **2 failed, 2 passed, 44 deselected in 1.06s**; the two
rules-profile controls pass. Both snapshots byte-match the PR worktree.
The local `/bin/bash` reports GNU Bash **3.2.57**; an independent empty-array
expansion under the same strict flags fails identically. The failure therefore
also exists on clean main in this environment and is not introduced by the docs.
Evidence and logs are retained privately. No workflow, test, strict flag or source
was changed to bypass it. Linux validation below runs the unchanged suite with a
modern `/bin/bash`; changing `PATH` alone cannot replace the hardcoded executable.

Earlier round-2 verification on `24910a0d`:

| Command | Result |
| --- | --- |
| `uv run ruff check .` | PASS |
| `uv run ruff format --check .` | PASS, 557 files |
| `uv run mypy .` | PASS, 547 source files |
| `uv run pytest -n 4 --junitxml=<private-output>/pytest.xml` | PASS, 4933 passed, 985 skipped, 8 warnings in 360.16s; exit 0 |

Pytest ran in a task-owned ephemeral Linux arm64 container with Python 3.13.11
and Bash 5.2.15. All 1120 tracked files matched the private source snapshot,
which retained independent Git metadata and excluded untracked environment files
and host secrets. `uv sync --frozen --all-groups` succeeded; the frozen sync check
reported no changes. All 5918 tests were collected with four workers, without
selection changes or strict-flag overrides. `TEST_DATABASE_URL`,
`TEST_RABBITMQ_URL` and `TEST_REDIS_URL` were verified absent; skipped integrations
were not executed. Full log/XML are retained privately. Publication is pending.

### Published integration checkpoint: `05a96bc4` (historical)

Main advanced with source changes and four new `171-*`/`209-*` plans. Both
documentation commits rebased without conflict onto the fixed point
`05a96bc422468a570bab3233b11b538eb4a87ff6`, producing head
`776a900f6e2ec93b478e45ece74928c09a91dab9` before bookkeeping.
Both replayed patches remain equivalent (`git range-diff`: `=`).
The four new plans contain no applicable concrete path, endpoint or resource
trace and remain byte-identical to main. The generic cache-access retry note
in `209-plan.md` is preserved, including its historical results and mechanism.

The actual current audit covers 111 main plans and 113 branch plans. The issue
scan remains 137 lines/32 files before and 17 permitted lines/8 files afterward;
supplementary scans inspected the new plans. Scope remains 38 documents, with
no new historical-plan edits. Application/test/workflow/config/dependency diffs
against the integration point are empty. Static gates pass: Ruff lint, format
(564 files), and mypy (554 source files).

The unchanged latest-source Linux run completed with
`uv run pytest -n 4 --junitxml=<private-output>/pytest.xml`: **4981 passed,
988 skipped, 8 warnings in 222.85s**, exit **0**; all 5969 tests were collected.
Its tracked-only snapshot matched 1131 files and retained independent Git
metadata, without untracked environment files or host secrets. The unchanged
lockfile permitted reuse of the verified environment; frozen sync reported no
changes. Runtime: Linux arm64, Python 3.13.11, Bash 5.2.15. Database, RabbitMQ
and Redis integration URLs were verified absent; skipped cases were not executed.
Full log/XML are retained privately. Earlier macOS and `24910a0d` Linux results
remain historical evidence, distinct from this source-changed integration run.

Fresh final review reports **Standards 0 / Spec 0** on this integration point.
The frozen files, actual gates and publication instructions have been delivered
for the existing non-draft PR; publisher handoff is complete. Remote publication
and review-response outcomes remain recorded on PR #260.

### Published integration checkpoint: `74b42a7` (historical)

Main advanced by three merged #168 parser CPU-limit commits. All three own
documentation commits rebased without conflict onto fetched current main
`74b42a7134fb9f272d93c913344e5b826b03b523`, producing head
`dab5081e455429314c9b5e8a5d54401bb361bc13` before this bookkeeping.
`git range-diff` reports all three replayed patches equivalent (`=`).

The two newly merged `168-parser-cpu-limit-*` plans contained a concrete local
DB/vhost name and two loopback service URLs. Only those three lines were
sanitized to isolated-resource wording and the established portable URL inputs.
Branch/revision references, CPU-limit design and numeric proofs, commands,
fences, dates and completed checkbox states remain intact. No other new traces
were found in the supplementary context scan. Scope now covers 40 documents:
37 historical documents, the worker document and the two #208 plans. The new
plans are merged-main content, with no open-PR ownership overlap introduced.

The current audit covers 113 main plans and 115 branch plans. The issue scan
finds 139 lines/33 files before and the same 17 permitted lines/8 files after.
The accepted worker paragraphs/table, stock proofs and all prior corrections
remain unchanged. Application/test/workflow/config/dependency diffs against
current main are empty. `git diff --check` passes. Current static gates pass:
Ruff lint, format (566 files), and mypy (556 source files).

The unchanged Linux full suite completed with
`uv run pytest -n 4 --junitxml=<private-output>/pytest.xml`: **4996 passed,
988 skipped, 8 warnings in 159.58s**, exit **0**; all 5984 tests were collected.
Its tracked-only
snapshot matched 1135 files and retains independent Git metadata. No untracked
environment files or host secrets were copied. The lockfile is unchanged;
frozen synchronization reports no environment changes. Runtime remains Linux
arm64, Python 3.13.11 and Bash 5.2.15. Database, RabbitMQ and Redis integration
URLs are absent; skipped integrations must not be reported as executed.
Full log/XML are retained privately. The preceding `05a96bc4` result is historical.
Fresh independent review reports **Standards 0 / Spec 0**. Frozen files, actual
gates and the current expected-head lease have been delivered to the publisher;
handoff is complete. Remote publication and review-response outcomes remain on PR #260.

### Merge preparation checkpoint: `4faa03d`

After explicit authorization to merge PR #260 and then move issue #208 to
AC Check, all four own documentation commits rebased without conflict onto
fetched current main `4faa03dd7accfa161048f584b834b314fa1ab5b6`, producing
head `90308a027158ab0036a49577aee0781c0e814db4` before bookkeeping.
All four replayed patches remain range-diff equivalent (`=`).

The four new `130-verifier-gateway-*`/`144-*` plans contained local checkout,
database/vhost, loopback service URL, temporary-disk and shell-wrapper traces.
Only those references were generalized; failed/successful results, dates,
revisions, hashes, task states, test selection and workaround mechanisms remain
intact. Fresh review identified the model-fixture loopback URL and one remaining shell-wrapper
path; both are now generalized, retaining exact model/error expectations and
the seven-failure result/workaround. Public dev sandbox repository/App provenance
remains under the existing public staging exception, adding one permitted line.

Current inventory: 117 main/119 branch plans; issue scan 152 lines/37 files before
and 18 permitted lines/9 files after. Scope is 44 documents: 41 historical
documents, the worker and two #208 plans. Existing accepted documents, worker
text/table and stock proofs are intact. Source/test/workflow/config/dependency
diffs against current main are empty; `git diff --check` passes. Static gates
pass: Ruff lint, format (590 files), and mypy (580 source files).

The first current Linux full run used
`uv run pytest -n 4 --junitxml=<private-output>/pytest.xml` and exited **1**:
**2 failed, 5369 passed, 1018 skipped, 8 warnings in 333.47s** (6389 tests).
Both failures were 8-second subprocess timeouts in
`tests/test_worker_shutdown_process.py`: the resistant-pipeline resources/lease
backstop case and watchdog-failed-pipeline join-before-teardown case. This is a
failed gate. Focused verification of those two unchanged cases passed:
**2 passed in 7.38s**, exit **0**. The container CPU quota was unrestricted.
The second full attempt retained every test with two workers:
`uv run pytest -n 2 --junitxml=<private-output>/pytest-n2.xml`; **5371 passed,
1018 skipped, 4 warnings in 260.67s**, exit **0**, all 6389 tests collected.
Both full-attempt logs/XML and focused evidence are retained privately.
No test timeout, source or strictness is changed. Its tracked-only
snapshot matched 1163 files with independent Git metadata; no untracked
environment files or host secrets were copied. The lockfile is unchanged and
the verified task runtime is reused with frozen synchronization. Runtime remains
Linux arm64, Python 3.13.11 and Bash 5.2.15. Database/RabbitMQ/Redis integration
URLs are absent; skips must not count as executed integrations. Earlier runs
are historical. Fresh independent review reports **Standards 0 / Spec 0**.
Frozen documentation, actual verification outcomes and the expected prior-head
lease have been delivered to the publisher; handoff is complete. The coordinator owns current-head reapproval, green CI and protected
merge, followed by the board move only after confirmed merge. No protection
bypass, issue closure or acceptance-checkbox edit is authorized here.
