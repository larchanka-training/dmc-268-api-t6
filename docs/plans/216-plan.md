# Reflection regressions and documentation (#216): review round 2

## Current round 2 plan

Address the [new P2 thread](https://github.com/larchanka-training/dmc-268-api-t6/pull/303#discussion_r4375309930)
and all four P3 items in the 26 October review of published head `7c1455bf`.
The reviewer accepted and closed the four round 1 threads; their fixes and the
existing mutation-killing regression tests remain. The round 1 record below is
historical. The scope decision in this section records removal of the replay tool.

### Decisions and dependencies

- Continue the existing ready PR #303 without requesting another reviewer.
  The user authorized addressing all comments; no new feature approval is needed.
- Remove the three replay-tool files by the tech lead's explicit scope decision.
  Keep numerical mutation results and disposition tables, labeled **local run;
  driver not in repository**. Do not add a replacement tool or follow-up issue.
- Preserve existing behavioral tests and all accepted round 1 behavior. No new
  behavior is requested, so do not invent RED tests for file deletion or prose.
- Correct published provenance and stale documentation references using current
  main symbols where possible. Preserve the two schema/payload hashes and the
  explicitly UNKNOWN live-provider acceptance result. Do not make paid calls.
- Refresh ownership before any later shared-file edit. Fully paginated file lists
  for the six other currently open PRs (#327, #326, #304, #283, #279 and #220)
  show no owner of the targeted #130/#144/#216 documents or three replay files.
  #324 and #130 F1 have merged; their behavior must survive the rebase.
- Safely rebase on current main before final verification/publication. The main
  snapshot checked during planning is `1417faa716294865c196decdf5f19867237d234b`;
  a later advance requires another integration check. The old 6,660-test result
  remains historical and cannot prove the new tree.

### R2.1: Apply the complete review correction

**Dependencies:** Saved plan and refreshed ownership. **Estimated scope:** One
bounded deletion/documentation slice: three tool deletions, five shared plan
documents, and the four #216 evidence/plan metadata documents. These edits all
implement the same review correction; no application/runtime change is planned.

**Acceptance criteria:**

- [x] Delete `review/scripts/replay_reflection_mutations.py`,
  `review/scripts/reflection_mutations.json` and
  `tests/test_reflection_mutation_replay.py`. Keep existing regression tests.
  Remove executable-driver reproduction instructions and links from #216 docs;
  retain measured result tables with the local-run limitation.
- [x] Address P3-1–3 in `216-plan.md`, `216-verification.md`,
  `216-coordination.md`, `130-verifier-gateway-plan.md`,
  `130-verifier-gateway-todo.md`, `130-run-pins-plan.md`, `144-plan.md` and
  `144-todo.md`: use published `4fa702e5` / `f00d1b92` or unlabeled local-before-rebase
  prose, replace stale stacking claims with main after #324, replace shifted
  `policy_snapshot.py` line references with actual symbols, and include
  `eval_live.py` among checksum consumers. Preserve schema/payload digests.
- [x] Address P3-4 with one concise comment on #217 directing the F2 owner to the
  strict-schema acceptance gate before v4 activation; record its receipt. Safely
  rebase and preserve merged F1/#324 and accepted #216 behavior, with no unrelated
  code changes.

**Verification:** Diff/path/dead-link/stale-reference audit; exact deleted paths;
no regression-test deletion or immutable prompt/schema change; inspect current
symbols and checksum consumers. Review the rebase range-diff against the
published `7c1455bf` range. Fresh independent Standards and Spec reviews must each
report zero findings; return any finding to development before final gates.

### Checkpoint: Complete correction reviewed

- [x] One P2 and all four P3 items have concrete dispositions.
- [x] Independent Standards = 0; Spec = 0; source writers stop for final gates.

**R2.1 complete:** Removed exactly three replay-tool files; retained behavioral
regressions and historical local mutation tables. P3-1–3 provenance/symbol/consumer
corrections are complete. The one-line [#217 gate comment](https://github.com/larchanka-training/dmc-268-api-t6/issues/217#issuecomment-6413779899)
was posted and read back; real provider acceptance stays UNKNOWN. The main F1
rebase required six factory-import consumer updates (18 consumers total), the
missing `RunFailure` import and two existing test-contract adaptations. Current
worker-wiring prose reflects merged F1. Fresh independent implementation reviews
reported Standards = 0 and Spec = 0; all source writers then stopped.

### R2.2: Verify the final rebased tree

**Dependencies:** R2.1 reviewed clean. **Files:** #216 evidence metadata only.
**Estimated scope:** S.

**Acceptance criteria:**

- [x] Run `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .` and `uv run pytest` with isolated PostgreSQL/RabbitMQ configured.
  Record actual current counts, base/head/input boundary and zero unexplained
  failures/skips. Do not rerun the removed mutation driver.
- [x] Record current proof separately from historical 113-probe/6,660-test
  measurements, and check that evidence-only edits preserve measured code.
- [x] Fresh final Standards/Spec checks cover the complete review delta and
  resulting evidence; no private machine paths or service details enter Git.

**Verification:** Gate logs/results, before/after input identity and final diff.

**R2.2 current measurement:** Base `1417faa7`, rebased ancestor `f145b70c`
plus reviewed R2 dirty changes. The 1,223-file projection excluding exactly the
four #216 metadata documents is
`36f1a8eee8ceb851aff6d41a8daae435b530c0cbf220cfbe0566852ac73386d3`.
The first full run was RED: 6,983 passed, one eight-second shutdown subprocess
timeout, zero skipped, two existing warnings, 2,161.64 s. It is not waived or
reported as a successful gate. Unchanged shutdown tests gave 4 failed/9 passed
on bind-mounted source, then 39/39 across three repeats on a byte-identical native
Linux-volume copy. The precise stage of the original timeout remains unproven.
No source, test, assertion or timeout changed. A fresh complete canonical run on
the native copy has passed Ruff, format (621 files) and mypy (611 source files);
its full pytest completed **6,984 passed, zero failures/errors/skips, two existing
warnings, 1,156.88 s** (JUnit duration 1,156.689 s). All 1,223 native-copy hashes
match before/after the complete run and the live worktree has the same identity.
These source-preserving evidence edits change only the four excluded documents.
Fresh independent final evidence reviews reported Standards = 0 and Spec = 0.
Publication and exact-head CI remain pending.

### R2.3: Publish the existing PR update

**Dependencies:** R2.2 complete and reviewed. **Files:** #216 publication metadata
and existing PR fields/comments. **Estimated scope:** S.

**Acceptance criteria:**

- [ ] Publisher explicitly stages reviewed paths, makes Conventional Commits
  referencing #216, refreshes main/remote head, and safely updates the existing
  ready PR with a verified force-with-lease if rebase requires it.
- [ ] Rewrite the four-section PR body around the resulting change and current
  verification; reply to the new P2 thread with the new SHA and summarize four
  P3 dispositions. Preserve the reviewer-owned resolution boundary and prior
  closed threads; request no new reviewer.
- [ ] Verify current-head CI and Board 12 **On Review**, then remove only owned
  temporary resources. Keep #216 open; no merge or self-approval.

**Verification:** GitHub readback, exact-head CI and ownership-checked cleanup.

### Risks and remaining unknowns

Main and overlapping PRs can advance; recheck ownership and preserve upstream F1
semantics during integration. Provider strict-schema acceptance remains UNKNOWN
until the #217/F2 gate is executed. The historical replay numbers remain useful
evidence, but the driver is deliberately absent and no longer a supported
repository command. There are no unresolved product questions for this pass.

## Historical round 1 plan

## Scope and completed baseline

Address every item in [review round 1 of PR #303](https://github.com/larchanka-training/dmc-268-api-t6/pull/303#pullrequestreview-5643731668): four P2 threads and eleven P3 items. This feedback pass continues the approved agent-loop implementation without a new feature or prompt rollout. Public behavioral test seams remain `ReflectionPass.verify`, `ExecuteReviewRun`, public builders/guards, parsers, schemas and publication outputs.

The original implementation is preserved in commits `0a19ff12` and `bf9d4c6d`. It delivered regression coverage M01–M15, public rendering and ID validation, typed policy budgets, test-only factories, missing-Reflection server-error classification, legacy attribution fail-closed behavior, documentation corrections and two verified historical PR #133 body corrections. Original tasks 0–16 completed their independent Standards/Spec reviews before publication. Historical detail remains in Git history; [verification](216-verification.md) keeps measured results, current limits and portable reproduction.

Historical measurements must remain distinct:

| Baseline | Measured result | Meaning |
| --- | --- | --- |
| Pristine `e6d3077` | 5,684 passed, 2 failed, 0 skipped | Original full run was red; unchanged focused reruns passed, without proving a cause. |
| Final original implementation `4faa03d` | 6,521 passed, 0 skipped | Service-backed full Linux run; later adoption of documentation-only `f8b13103` preserved executable bytes. |
| Published `bf9d4c6d` | CI 6,521 passed, 0 skipped; 103 isolated mutation replays killed/restored | Historical result, superseded as current evidence once this round rebases. Review discovered an additional live survivor outside that recorded replay set. |
| Review-round measured tree | Base `9243fd67`, HEAD `f00d1b92`, with reviewed uncommitted R1 fixes | 6,660 passed, zero failed/errors/skips; 113 live mutation probes killed/restored GREEN. Full input identity and metadata-only continuation proof are in verification. Publication/current-head CI remain pending. |

Original mutation dispositions remain: the removed exact/lowercase slug map and secondary positional `any` expression are superseded by #230, not equivalent. Four domain-equivalent probes are the redundant downstream duplicate guard, isolated empty-check term in the checksum inequality, and removal of verifier arrays' `minItems: 0` for evidence/requirement refs. A mandatory-envelope equality exploration changes diagnostics before the next zero-budget guard; it is not strict equivalence and is outside the original final-send guard acceptance. No live non-equivalent survivor may remain unexplained.

## Decisions and boundaries

- Continue on `chore/216-reflection-tests-docs` and update existing non-draft [PR #303](https://github.com/larchanka-training/dmc-268-api-t6/pull/303), with no reviewer requested. Do not create another PR, merge, close #216 or resolve reviewer-owned threads.
- All implementation is delegated. Each task below touches at most five implementation/test/documentation files, excluding the plan/todo/evidence metadata. Fresh independent Standards and Spec reviews follow each task; findings return to development before advancement.
- Existing-correct behavior uses isolated mutant RED followed by byte-restored GREEN. Changed behavior uses failing public tests followed by the minimum fix. No tests for trivial import/wording changes that merely mirror implementation.
- Keep `RunFailure("internal_error")` for a snapshot composed without Reflection. Explicitly document its transition from terminal policy failure to retryable server failure (three attempts, then failed/DLQ under current worker policy). Adjacent unreadable verifier resources and duplicate defaults IDs retain their existing integrity classification in this bounded change, with an explicit disposition.
- The schema's absolute end lookahead is a provider-compatibility risk, not evidence that a live provider accepts it. Investigate supported schema/regex subsets from authoritative sources and the actual transport. Record an explicit pre-v4 rollout gate in remaining #130 PR F rollout slice (announced as F2), linked to #217, including exact schema SHA, strict request payload, primary/fallback routes and observed provider outcome. No paid call, dependency, silent weakening of newline validation or immutable prompt edit is authorized by this task.
- Retain the original implementation as one logical commit to preserve review provenance (published as `4fa702e5` after rebase). Correct the malformed issue reference in the small publication-record commit while rebasing; new review fixes use focused Conventional Commits with `(#216)` or `Refs #216`. A history split of the original commit is optional feedback; this plan consciously declines it because it would obscure anchored review evidence without changing behavior.

## Review inventory and acceptance map

| Review item | Disposition and task |
| --- | --- |
| P2 factory/rebase, thread `4371232016` | R1.1: semantic rebase, thirteenth test factory consumer, #130 plan reference; refresh total consumers and full-run provenance. |
| P2 PIPELINE/retry, thread `4371232021` | R1.5: correct snapshot error and inline `None` descriptions; retain `internal_error`, name terminal-to-retry change and bounded neighboring-error disposition. |
| P2 portable plans, thread `4371232026` | R1.6b: remove machine paths, service addresses and session transcripts from all four #216 documents; retain numerical/SHA evidence and canonical gate commands; local run, driver not in repository. |
| P2 step-3 refs/allowlist, thread `4371232031` | R1.2: attributed `insufficient_context` with empty requirement refs is rejected per candidate, not a failed pass; assert exact publication allowlists for present rules/AGENTS. |
| P3-1 provider lookahead | R1.7: documented official compatibility investigation and explicit pre-rollout validation gate; no unsupported claim of provider acceptance. |
| P3-2 warning severity | R1.2: supported `low` survives `warning`, `info` drops; exact final summary/counts and critical-for-all mutant. |
| P3-3 semantic source-list assert | R1.3: independently assert the complete requirement-ref source list in both packaged versions, not a repeated mention. |
| P3-4 wrong repository class | R1.5: actual `SqlAlchemyReviewOutputRepository` in README and both #120 historical documents, without rewriting adjacent scope. |
| P3-5 both-passes docstrings | R1.3: both affected verifier-only docstrings accurately state their coverage. |
| P3-6 adjacent assembly errors | R1.5: explicit consciously retained resource/duplicate-ID integrity classifications, without silently changing retry behavior. |
| P3-7 missing document duplication | R1.4: use public `policy_documents.missing_document` from eval and the test helper; preserve dataclass values/output bytes. |
| P3-8 commits | R1.1/R1.8: repair `bf9d4c6d` message, retain original large commit by documented choice, make review commits focused. |
| P3-9 bulky/session plans | R1.6a/R1.6b: compact durable baseline and mutation trial tables; local run, driver not in repository. |
| P3-10 active DB pins | R1.5: separate static fixture defaults from active-row IDs captured on run creation and restored from pinned rows, including legacy missing pins. |
| P3-11 shared-file comments | R1.0: refresh owners and post deduplicated exact comments before touching an owned file. |

## R1.0: Ownership and synchronization preparation

**Dependencies:** None. **Files:** Metadata only. **Size:** S. **Implementation complete:** all ten open-PR file lists and existing comments were checked; the five exact combined notices below were posted and independently read back. Receipt URLs are recorded in `216-coordination.md`. Fresh independent Standards and Spec reviews both returned zero findings.

- Refresh every open PR's complete file list and existing comments, then identify ownership for all planned paths. New owners require the same check. Review notes exempt #300's non-overlapping TEST_PLAN changes; do not invent another comment there.
- Post one combined coordination update per remaining owner, covering PIPELINE_SPEC and missing file names, and verify each returned comment URL. The user request to address all feedback covers these required coordination notices; no additional broad approval round is needed.
- Inspect FYI #304: preserve its verifier Retry-After semantics if it lands first, `IgnoreMatcher(policy_base_sha=...)` and added policy-budget regression. Whichever PR lands second rebases. No order assumption replaces fresh main inspection.

**Verification:** [Five verified coordination receipts](216-coordination.md) cover the verifier paragraph, its initial contracts-table pin qualifier and omitted shared-file names. All owners were checked before edits; future owners require a fresh notice. No owned-file edit bypasses the comment gate.

## R1.1: Rebase with new run pins and factory consumer

**Dependencies:** R1.0. **Files:** `app/modules/reviews/infrastructure/run_repository.py`, `tests/test_run_prompt_pins_postgres.py`, `docs/plans/130-run-pins-plan.md`; commit-message metadata. **Size:** M.

- Safely rebase current PR onto latest main. In the known repository conflict keep main's `severity_threshold_value(...)` and this PR's `allowed_v2_rules=None`. Preserve all upstream run-pin/threshold/trace changes and #304 behavior if merged.
- Move the new test's deleted-factory import to `tests.policy_snapshot_fakes`; point the #130 plan's factory reference there. Audit every five factory names through all tracked Python, counting actual current consumers; no app or production review module imports tests.
- Correct publication-record commit subject to `docs(reviews): record PR publication and board status (#216)` or add its `Refs #216` footer. Never reuse results measured before runtime-changing rebase as current head evidence.

**Verification:** Demonstrate the post-rebase import/collection failure before the minimal import fix, then full collection, global Ruff/format/mypy and focused run-pin PostgreSQL tests with isolated service URLs. Recheck semantic conflict by AST/diff; no new behavior test for a pure import rename. Fresh Standards/Spec reviews.

**R1.1 implementation evidence:** Rebased onto `ef4e8b68b9443509a84f0c0ec273d08837b425c6`.
The implementation was published as one original logical unit in `4fa702e5`; splitting the
already reviewed baseline would add history-only conflict risk. Its only semantic rebase
conflict retained main's `severity_threshold_value(...)` and inline `allowed_v2_rules=None`.
Publication metadata was published as `f00d1b92` with a valid `(#216)` subject. Upstream run pins,
publication thresholds and trace behavior are preserved; #304 is not merged at this base.
The new run-pin consumer first failed collection with the missing production factory, then
passed all nine PostgreSQL tests after its import moved. An AST audit of 603 tracked Python
files found 13 test consumers, all five definitions only in the test helper, no stale factory
imports and no `app`/`review` imports of tests. Current collection: 6,608 tests. Ruff, format
(599 files) and mypy (589 source files) pass. Full-suite proof remains pending R1.8; previous
6,521-test results are historical and do not prove this runtime-changing rebase. Fresh independent R1.1
Standards/Spec reviews both reported zero findings.

## R1.2: Close public decision/publication coverage gaps

**Dependencies:** R1.1. **Files (three):** `tests/test_reflection_pass.py`, `tests/test_execute_review.py`, `tests/test_review_output_v2.py` (production files mutated only temporarily). **Size:** S.

- Add an attributed `.review/rules.md` or `AGENTS.md` candidate with verifier `insufficient_context` and `requirement_refs=[]`. Public verify succeeds and returns that candidate in rejected with the exact status. Mutant adding `insufficient_context` to step-3 matching-ref-required statuses must fail this test.
- Assert literal `published.allowed_v2_rules`: rules-only snapshot is `frozenset({".review/rules.md"})`; rules+AGENTS snapshot is `frozenset({".review/rules.md", "AGENTS.md"})`. Deleting either allowlist insertion must fail the appropriate assertion. Service-defaults preservation stays separate.
- Add a warning-threshold flow with supported low/info candidates: low publishes, info drops; assert exact list, counts/drop reasons and backend summary. The mutant treating every non-all threshold as critical must fail. Reuse the current publication helper with a configurable threshold rather than duplicate its fake ports.
- Separate model rejection from six independent public-parser cases (dict/string/bytes by E2/FSD1 trailing newline). This makes both current M11 probes reproducible from committed tests without a private supplemental test. Each parser assertion must execute even if the model validation also fails.

**Verification:** RED for each isolated step-3/allowlist/warning mutant, byte-restored GREEN, all three full affected files and required static checks; record new named killer IDs. Fresh Standards/Spec reviews.

R1.2 implementation and independent fresh Standards/Spec reviews are complete; both review axes reported zero findings.

## R1.3: Pin exact verifier requirement sources

**Dependencies:** R1.2. **Files:** `tests/test_reflection_evaluation.py`. **Size:** S.

- Assert the requirement-ref source-list clause itself contains the exact three literal policy sources in both v1 and v2. Removing any list member must fail even if a later sentence still mentions it; evidence sources remain separately asserted.
- Correct module/class docstrings that claim both-pass coverage. Keep immutable prompt bytes and checksums unchanged; reviewer injection defenses remain in their dedicated suite.

**Verification:** Six isolated list-member removal probes (three sources by two versions) fail semantic assertions, not checksum/budget errors; restore and run the full verifier evaluation file plus static checks. Fresh Standards/Spec reviews.

R1.3 implementation complete: six selective source-list deletions fail the named semantic assertion, each followed by exact restoration and all 55 evaluation tests passing. Ruff, format (599 files) and mypy (589 source files) pass. Immutable prompt bytes and checksum registry are unchanged. Fresh independent Standards/Spec reviews both report 0 findings.

## R1.4: Reuse the public missing-document constructor

**Dependencies:** R1.3. **Files:** `review/scripts/eval_live.py`, `tests/policy_snapshot_fakes.py`. **Size:** S.

- Replace eval's duplicate private constructor and the test factory's repeated dataclass construction with `policy_documents.missing_document`; keep the public test factory signature/return behavior where existing consumers depend on it.
- Preserve absent-document values, all arm snapshots/digests and request outputs. No dependency from production/evaluation code into tests and no unnecessary new public compatibility helper.

**Verification:** Existing missing-document/policy/evaluation suites and byte/value comparisons before/after, global Ruff/format/mypy. No redundant implementation-mirroring tests. Fresh Standards/Spec reviews.

R1.4 implementation complete: both consumers reuse the existing public constructor; the test factory signature is preserved. Before-change policy/offline-eval coverage passes all 362 tests. The combined capture of seven missing-document values, 12 evaluation arm snapshots/digests and eight fake-provider request envelopes is byte-identical before/after. All 950 affected tests and global Ruff/format (599 files)/mypy (589 source files) pass. Fresh independent Standards/Spec reviews both report 0 findings.

## R1.5: Align current and historical contract documentation

**Dependencies:** R1.4 and ownership gate. **Files (five):** `docs/PIPELINE_SPEC.md`, `review/README.md`, `docs/TEST_PLAN.md`, `docs/plans/120-plan.md`, `docs/plans/120-todo.md`. **Size:** M.

- Fix PIPELINE's verifier paragraph: missing Reflection with snapshot is retryable `internal_error`, not terminal policy integrity; document three-attempt/DLQ consequence. Describe inline `allowed_v2_rules=None` and the separate verified allowlist accurately. Preserve neighboring PR edits and worker composition boundary.
- Use actual `SqlAlchemyReviewOutputRepository.get_post_processing_input` in the three erroneous class references. Distinguish fixture/dataclass defaults from run-pinned active conventions/verifier DB rows; legacy absent pins remain absent/incomplete. Recheck exact post-rebase code instead of calling v2 universal runtime defaults.
- Keep adjacent unreadable resource and duplicate check-ID integrity errors unchanged with a clear deliberate disposition in durable evidence and PR body. Do not broaden #211 or enable #130 worker wiring.

**Verification:** Grep for stale class/error/allowed_v2 claims; named tests/links exist, direct inspection matches currentmain. Documentation-only: no new tests. Fresh Standards/Spec reviews.

R1.5 implementation complete: the five documentation files match retryable `internal_error` with three total attempts/dead-letter exhaustion, inline legacy `allowed_v2_rules=None`, the actual repository method owner and creation-time active-row prompt pins restored by ID. Missing legacy pins remain absent/incomplete. Resource-read and duplicate-ID integrity classifications are deliberately retained, with the PR disposition in `216-verification.md`. Direct code/named-test/link/resource/stale-claim and diff checks pass. Fresh independent Standards/Spec reviews: 0/0.

## R1.6: Historical mutation run and compact evidence

**Local run; driver not in repository.** R1 measured 120 mutation records:
113 live probes (92 historical, 11 original extras, 10 review regressions), four
domain equivalents, one diagnostic-only exploration and two superseded sites.
Sixteen representative probes initially passed control GREEN → named behavioral
RED → exact restoration → GREEN. The complete 113-probe run followed in R1.8.
The driver, catalog and safety-test module were later removed in R2 by the tech
lead's scope decision. The behavioral regression tests remain.

The four evidence documents retain the baseline/result matrix, M01–M15 trial
and disposition tables, named public test nodes and historical evidence boundaries.
R1.2/R1.3 covered the newly discovered step-3 empty-reference survivor and masked
source-list assertion. No executable-driver instructions or links remain.
Independent Standards/Spec reviews of these R1 slices reported zero findings.

## R1.7: Provider schema compatibility disposition

**Dependencies:** R1.6b. **Files:** `docs/plans/130-verifier-gateway-plan.md`, `docs/plans/130-verifier-gateway-todo.md`, `docs/plans/216-verification.md`; optionally one existing schema-contract test file only if justified by a real regression. **Size:** M.

- Trace actual `provider_schema` and strict `response_format` behavior; establish whether lookahead is preserved. Consult authoritative provider/engine documentation for the configured endpoint and separate documented support from assumptions.
- Keep local schema/model domain parity and newline rejection. Write the unresolved real-provider acceptance gate into the actual #130 PR F plan/todo files listed above, with exact schema SHA, strict request payload, primary/fallback routes, observed provider outcome and #217 reference; provide a reproducible contract probe or documented alternative if feasible, with no paid call here.

**Verification:** Cited primary sources plus local transport/schema tests. The outcome is a reasoned compatibility disposition, not a falsely successful provider run. Fresh Standards/Spec reviews.

**Implementation complete:** strict transport/regex preservation verified against current source; official compatibility/routing sources cited. The #130 plan/todo now carry an unchecked release gate before F2 v4 activation, linked to #217, without blocking dormant F1. Frozen raw schema and canonical strict payload hashes reproduce locally. Existing offline gateway/schema/parser suites: 185 passed, zero skips. Provider acceptance remains UNKNOWN; no paid/live call. Fresh independent Standards/Spec reviews both report 0 findings.

## R1.8: Final proof and update existing PR

**Dependencies:** R1.0–R1.7, each reviewed clean. **Files:** Evidence/coordination metadata and PR metadata; publisher commits reviewed changes. **Size:** S.

- Refresh main/ownership, rebase safely again if runtime changed, then run source-quiescent final gates with disposable PostgreSQL and RabbitMQ configured: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`. No skipped required integration slice, stale result identity or unexplained baseline failure.
- Replay applicable M01–M15 probes and all new feedback mutants against the final source; record named assertion killers and restored GREEN, alongside the four equivalences/two superseded sites and diagnostic-only exploration. Independent fresh whole-PR Standards and Spec reviewers must report zero findings.
- Publisher stages explicit paths, makes focused conventional review commits, pushes existing PR and rewrites its four-section body for current final behavior/counts/base and every hygiene disposition. Reply in all four review threads with fix/disposition and new SHA; address the P3 list in one concise review-round summary. Authors never resolve reviewer threads. Preserve non-draft/no-requested-reviewer, no closing keywords or Development link. Attach the existing PR and monitor current-head CI. Human approval remains pending.

**Verification:** Final gate evidence and current CI agree; diff contains no mutants/private artifacts/unrelated changes; all15 review items have a durable disposition. Clean up only resources created for this task after verification. Do not merge or close #216.

Historical R1.8 preflight safely rebased onto `9243fd67` (HEAD `f00d1b92`), preserving reviewed dirty changes and upstream checked-paths, trace/persistence and run-pin behavior. The existing service-default-ID test first failed eleven cases with missing `checked_paths`; adding that required argument restored all 178 tests in the bounded six-file slice. All five factories remain test-only, with 13 consumer files across 606 tracked Python files. Nine catalog fingerprints across two upstream-changed targets were audited and refreshed without changing mutant edits, node selections or semantic classifiers; all 120 records validate. Four affected source-target probes (parser, repair, shared validator and warning threshold) each passed control GREEN → named behavioral RED → restored GREEN in an isolated snapshot. Ruff, format (604 files) and mypy (594 source files) pass. Final verification is complete on the reviewed dirty tree: all 113 live catalog probes were killed by named behavioral failures and restored GREEN; full Linux pytest with dedicated PostgreSQL/RabbitMQ passed **6,660 tests, zero failures/errors/skips, two existing deprecation warnings, in 923.56 s**. All 1,207 input hashes matched before/after replay and pytest, with input identity `1d4a19f5983de54234f5f109bc5c4d04636ba53fff3bf1ae85418f794ea85192`. After a narrow driver-classifier fix, 17 safety cases and eight representative probes passed; fresh whole-PR Standards/Spec reviews reconfirmed 0/0 and global Ruff/format/mypy passed. The complete replay was then rerun on that exact final driver. Evidence metadata changes only the four #216 documents; verification records the stable 1,203-file projection. The final evidence delta also passed independent Standards/Spec reviews (0/0). Six focused review-fix commits were published in `77d544edc8636284d6131cd7a1baf0884dc7b8bb`; all four P2 threads received SHA-bearing replies, and one summary covers all eleven P3 items plus #130 F1/#304 ordering. CI run `40219321992` succeeded on that exact head: 5,620 unit + 268/258/330/184 integration = 6,660 tests, zero skips. The exact issue remains open and its Board 12 item On Review; no new reviewer was requested. Owned-resource cleanup removed only three containers, four volumes and one network after ownership checks; the shared image, worktree and backups remain. Publication receipts and the distinction between this verified code head and later documentation metadata are recorded in coordination and verification. The reviewer closed the four R1 threads on 26 October; the new R2 thread remains reviewer-owned and current-head approval remains pending.

## Remaining uncertainties

- Main and overlapping PRs may move during the loop; fresh ownership and semantic rebase govern every later edit.
- Provider-specific regex acceptance cannot be inferred from Python jsonschema parity or an unrelated provider's documentation; unresolved acceptance must stay an explicit rollout gate.
- The 6,660-test R1 result superseded the earlier 6,521 result for that historical tree. The R2 rebase requires fresh verification.
