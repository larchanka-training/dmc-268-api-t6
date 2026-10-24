# Plan: worker documentation and historical environment traces (#208)

## Scope and decisions

Docs-only work initially based on main `22c621ab`, issue #208 and its 23 October
clarification; refreshed onto main `25c83fe0d024270e9a744850baa9721cb0ede617`
after PR #256 merged. Correct two paragraphs in `docs/WEBHOOK_WORKER.md`; sanitize
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
plan/evidence files, worker documentation and these two plans (35 documents).
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
The broader worktree/checkout/backup audit found no further actual local trace;
portable sibling isolation guidance and branch names remain unchanged.

The initial environment search found 129 matching lines in 28 files before
editing. The refreshed-main audit covers all 103 tracked plan/evidence documents:
133 matching lines in 30 files before editing, 17 lines in 8 files afterward.
Every remaining match was inspected:
7 belong to the allowed self-creating rollback image procedure/invocation and
its cleanup evidence; 10 are public portal/staging hosts or GitHub sandbox
references. Supplemental checks covered bare temporary-directory prose, sockets,
plain-prose ports, relative checkout/paperwork paths and separate service resources.
Portable sibling-checkout isolation guidance in `140-plan.md` remains valid.
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

## Final verification on refreshed main

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
coordination link and `Refs #208`. Handoff is complete; publication defaults to draft.
