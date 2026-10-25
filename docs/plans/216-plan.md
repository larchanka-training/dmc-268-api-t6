# Implementation plan: Reflection regression tests and documentation (#216)

## Status and baseline

Planning completed and the user approved this plan, the public test seams,
the proposed coordination comments and the two exact PR #133 body corrections
with `ок` on 2026-10-25. The newly discovered #291 coordination comment was
separately approved with `Да, опубликовать`. Task 0 is complete: all current
shared-file comment gates are cleared and PR #133's two body corrections were
applied and verified. See `docs/plans/216-coordination.md` for authorization,
refreshed overlaps, comment URLs and exact body verification. No implementation,
mutations, services or commits were made during planning/task 0.

- Repository: `larchanka-training/dmc-268-api-t6`.
- Worktree: dedicated managed checkout for `chore/216-reflection-tests-docs`,
  referenced below as `$DMC216_WORKTREE`.
- Inspected original main: `e6d3077cfcb5584f8af0524ce4a0c8a658b2c5fb` (2026-10-25).
- Latest adopted main: `f8b13103f908b26e3e210c3f78b125f21bfbdaba`; the full
  service-backed gate was measured on `4faa03d` and carried across the later
  documentation-only synchronization by exact runtime/test/resource identity.
  All 103 final behavioral mutation replays are killed and restored green.
  Proof is complete; publishing remains pending. See task 16 evidence.
- Specification: [issue #216](https://github.com/larchanka-training/dmc-268-api-t6/issues/216),
  including [PR #133 review 5575342693](https://github.com/larchanka-training/dmc-268-api-t6/pull/133#pullrequestreview-5575342693).
- Related #219 and #211 are still OPEN. #219's implementation
  [PR #230](https://github.com/larchanka-training/dmc-268-api-t6/pull/230) merged
  2026-10-24. Issue status does not override the actual merged code.
- Companion checklist: `docs/plans/216-todo.md`.

## Current code changes the interpretation of the issue

Issue line numbers refer to old main `a50be1a`; locate symbols rather than patching
those lines. Main now uses stable defaults IDs (`E2`, `N1`, etc.), verifier v2,
post-fit review budget checks, provenance checks, candidate-level reviewer drops,
and deterministic summaries after severity filtering. Do not restore old behavior
just to make an obsolete mutation applicable.

The exact/lowercase slug map and the secondary positional-check `any` expression
no longer exist in `ReflectionPass.verify`. They are **superseded/not applicable**,
not equivalent mutants. Preserve their intent with stable-ID/foreign-ID and
requirement-source tests. The old test named
`test_reflection_pass_service_defaults_exact_case_slug_matching` now tests `E2`,
so rename its description accurately when touching that test.

`test_execute_review_run_severity_threshold_critical_drops_medium_clean_summary`
already combines a snapshot, Reflection Pass and a critical threshold. Reuse it as
AC evidence and prove its sensitivity; add a distinct supported-above-threshold
case only if needed to kill the relevant mutant. Avoid redundant tests.

`review.verifier.v2.md` is the current builder default and pinned factory binding;
v1 remains a supported historical resource. Do not change published prompt bytes
or checksum pins as part of semantic regression tests.

## Design and scope decisions

Keep Clean Architecture, existing public use cases and fake ports. No dependency,
migration, live provider call, prompt rollout or runtime worker wiring is needed.
The agreed test seams proposed for approval are `ReflectionPass.verify`,
`VerifierPromptBuilder.build_verifier_prompt`, `PromptBuilder.build_prompt` /
`build_manifest`, public budget guards, JSON/Pydantic parsing, `ExecuteReviewRun`,
and post-processing/publication outputs. This satisfies the TDD skill's request to
confirm seams before adding tests.

For existing correct behavior, RED is the new test failing under its isolated
mutant; GREEN is restoring the original code. Do not change correct production
code to manufacture a failing test. For schema parity and hygiene, demonstrate a
real public failure first when behavior changes, then make the smallest fix.

### Mutation coverage matrix (issue part 1)

Every row is recorded in `docs/plans/216-verification.md` during development with
base SHA, exact isolated patch, command, baseline outcome, final killer test and
result. That evidence file does not exist yet.

| ID / issue item | Current probe and required evidence | Task |
| --- | --- | --- |
| M01 exact slug and exact-over-lowercase precedence | Historic map removed by #230: record source/history evidence, superseded. Test pinned stable-ID acceptance, wrong ID/foreign rule rejection; no positional or case-fold fallback. | 1, 2 |
| M02 secondary `any` -> `all` with shared positional check | Historic expression removed: superseded. Probe current stable check-ID lookup and shown-line binding instead; duplicate stable IDs must fail closed. | 1, 2 |
| M03 contradicted / insufficient requirement refs | For every status, reject malformed/hidden/non-policy requirement refs. `contradicted` with attributed rule needs matching source; `insufficient_context` may have no refs but must validate any supplied refs. Probe skipping validation for each status and supported-only validation. | 2 |
| M04 verifier guard boundary / system omitted | At exact send budget model is called; one token over is rejected before model. System plus user counts must matter. Use public inputs/fake counter seam, not private method replacement. Distinguish mandatory-envelope and final-send guards. | 2 |
| M05 empty verifier checksum | Blank checksum fails before verifier network call even when version/resource are otherwise valid. Probe removal of empty-check guard. | 2 |
| M06 policy `>` -> `>=` | Literal configured small cap with exactly-at-limit accepted and limit+1 rejected, including error limit/actual. | 2 |
| M07 defaults escaping: check text / rule name | Parameterize tag-breakout and quote payloads independently in both reviewer/verifier user envelopes. With canonical rule names required now, use a controlled pinned-default mapping fixture for adversarial name while exercising public builder; record why unknown name alone is not an escaping test. | 3 |
| M08 full long rules document | At least 250 numbered policy lines, distinctive lines 200/201/final; assert actual rendered text in both prompts and manifest visibility. Verifier can cite a late requirement. Truncation at 200 must fail. Stay within policy budget. | 3 |
| M09 insufficient_context publication | Snapshot ExecuteReviewRun with real ReflectionPass and fake model decisions: supported and insufficient candidates; only supported reaches publisher, rejected reason/counts retained. Probe allowing insufficient through. | 4 |
| M10 all candidates' attribution / positive sources | Valid first and invalid later candidates; only offending candidate drops under current main. Positive AGENTS.md and service-defaults stable-ID flows retain attribution through public publisher seam. Probe first-item-only validation. | 4 |
| M11 service-defaults trailing newline | Reject `service-defaults:E2\n` via v2 parser and attribution repair public output. Probe old `^...$` plus match behavior at each applicable site. | 5, 12 |
| M12 manifest fields/counts | Noncontiguous RIGHT ranges give literal sum, not min..max span; no snapshot gives run_id=None; omitting stage or blocks raises TypeError. Probe separate mutations. | 3 |
| M13 schema/model constraints | Draft202012 and Pydantic shared valid/invalid examples; maxLength, closed objects at every object node, array min/max, rule_name domain, 128/129 and 64/65 boundaries. Detect both narrowing and widening. Keep runtime-only cross-field semantics explicitly separate. | 5 |
| M14 verifier prompt meaning | Semantic assertions for trust boundary, statuses, one decision/candidate, policy/check_id sources and example. Exercise historical v1 explicitly and current v2 separately; do not use checksum-only evidence. | 6 |
| M15 reflection + severity threshold | Existing combined test plus mutation removing/moving threshold; assert exact published list, dropped reasons, counts and post-threshold deterministic summary. | 4 |

Before adding tests or refactoring production code, manually apply each applicable
mutation **one at a time** to the pristine implementation. Run the focused public
seam first; a named failing test conclusively kills the mutant and its existing
killer is reused. For focused survivors, run the relevant unit importing tests, recomputed
from current imports (the historical 47-file union is now 55), plus literal resource
readers and the focused seam. The approved isolated archive uses the exact base,
unset service URLs and `-m "not integration"`; narrow integration expansion is
reserved for an observed persistence/wiring risk. Restore just that patch immediately and verify exact
source bytes. Record a green importer corpus only as preliminary gap evidence,
never as a whole-suite survivor or automatic equivalence. Reserve full-suite mutant
runs for unexplained behavior risk; the full pristine and final gates remain
mandatory. This risk-based execution refinement was explicitly approved by the
coordinator after estimating repeated PostgreSQL migration costs. The issue's
historical green result must be rechecked, not assumed.

A surviving mutant is not automatically equivalent. An equivalent classification
requires reachable-input/domain proof explaining why no observable result differs.
An absent historic site is superseded. An unexecutable probe/environment error is
blocked, not green. The PR must list each equivalent/superseded item and rationale;
no live non-equivalent survivor is acceptable. Re-run applicable probes against
final code; when a refactor removes a site, map its observable invariant to the
new code and repeat the corresponding probe there. Never commit mutants.

### Schema change is intentional

The v2 JSON schema currently accepts `rule_name="Security"` while the runtime
rejects it. Add a rule_name pattern matching the current public lexical domain:
`.review/rules.md`, `AGENTS.md`, or `service-defaults:` followed by ASCII letters,
digits, underscore or hyphen, with an absolute end check that rejects a trailing
newline under JSON Schema regex semantics. Null remains accepted. The schema
must not limit IDs to a stale lowercase slug or just the built-in ID inventory;
manifest validation owns applicability to the pinned defaults version. Examples
include `service-defaults:E2`, `service-defaults:FSD1`, null, unknown raw name,
empty ID and trailing newline. Boundary literals are specified independently of
the implementation, and schema/model acceptance is tested in both directions.

### Hygiene dispositions (issue part 3)

| Review item | Proposed disposition | Task |
| --- | --- | --- |
| P3 9 private render calls / duplicated escaping | Promote a small coherent public rendering surface in prompt_builder; use its existing xml_text/xml_attribute in reflection; consolidate the two policy-document renderers without changing bytes. Update external callers. | 7 |
| P3 10 dead profile dict fallback / 12000 | Remove unreachable dict/get branch; typed ExecutionProfile is authoritative. Name/reuse a single default-policy-budget constant where defaults are declared, retaining configurable per-run caps. | 8 |
| P3 11 app test factories | Move five make_* fixture factories to tests/policy_snapshot_fakes.py; migrate nine current consumers in three small batches, then remove production definitions. Keep production checksum registry and contracts in app. Re-scan after rebase for added consumers. Task 11 also adapts the newly discovered eval_live.py consumer through public dataclass construction without a tests dependency; the bounded batch remains five files. | 9–11 |
| P3 16 private ID regex import | Expose a named public lexical validator (or public constant if simpler) used by model and post-processor; preserve strict full-string matching and test newline rejection through public boundaries. | 12 |
| P3 17 missing reflection assembly error | Raise existing RunFailure("internal_error") for missing reflection with snapshot; keep genuinely incomplete/corrupt snapshots as SNAPSHOT_INTEGRITY_ERROR. Assert no model/publication calls and existing author-facing internal-error text. | 13 |
| P3 19 prompt disk read | Reject removing package resource loading in this PR: current resource is version-selected and raw-checksum validated against snapshot; choosing a DB-backed pinned-content resolver belongs with #130 wiring. Avoid a second resolver/cache invalidation policy. Document this explicit disposition and residual disk I/O in our PR, with #290 as coordination context. | 15 |
| P3 20 allowed_v2 local | Inline None and preserve fail-closed legacy behavior and explanatory comment; never enable attribution based merely on snapshot-row existence. | 14 |

Each row must end as fixed or explicitly rejected in the PR body; the proposed
P3 19 rejection is a conscious scope decision, not a claim that disk I/O vanished.

Task 7 remains within five source/test files: prompt_builder.py, prompt_budget.py,
reflection_pass.py, tests/test_reflection_pass.py and tests/test_prompt_builder.py.
The two existing test modules contain four and one external changed-file renderer
calls respectively; migrate every external caller to the coherent public API.
Place byte-preservation assertions in test_prompt_builder.py rather than expanding
this task into test_prompt_builder_markdown.py. Do not keep private compatibility
aliases solely for tests. This is a bounded file-list refinement, not a new task.

## Documentation corrections (issue part 2)

Task 15 edits only review/README.md, docs/TEST_PLAN.md, docs/plans/120-plan.md,
and docs/plans/120-todo.md, after refreshing main and ownership checks.

- README current prompts: name current verifier **v2** and historical verifier v1
  accurately. Qualify reviewer evidence_refs as snapshot/v2-path behavior; live
  worker/gateway rollout remains #130 at this base. Explain that legacy parsing
  silently discards evidence_refs rather than validating/persisting them.
- TEST_PLAN §3.4: locate the heading (now around 476, not old 333). Name reviewer
  injection tests in test_prompt_builder_markdown.py separately from verifier
  TestPromptInjectionDefenses; `[INST]` may remain literal untrusted user data,
  while system text remains protected. Describe legacy evidence stripping.
  Leave #211's neighboring AC7/Option(a)/non-code/pre-send status reconciliation
  to #277 and preserve its eventual merged wording.
- 120 plans: correct the DB attribution claim (current legacy default is None),
  distinguish log ID/status counters from durable structured verifier reasons,
  accurately describe superseded slug tests and their historical scope, document
  legacy evidence stripping. Use verified review links: round 2 **5557688806**,
  round 5 **5568780909**. Do not imply new #219 behavior shipped in #133.
- Recheck all current-state statements after rebase/any #130 merge. Historical
  phase descriptions stay historical with explicit present-state notes.

PR #133 is already merged. The user explicitly authorized the following two
exact body corrections with `ок` on 2026-10-25; both were applied and independently
re-read that day. No comment was posted there, and the rest of the historical
body was preserved byte-for-byte. Authorized exact line replacements:

1. `очистка саммари от галлюцинированных дефектов при полном отклонении кандидатов.`
   -> `очистка саммари от галлюцинированных дефектов при любом отклонении кандидатов, включая частичное.`
2. `### 6. Тестовое покрытие (13 файлов, ~6 300 строк)`
   -> `### 6. Тестовое покрытие (13 файлов, 6 472 добавленные строки)`

6472 is the sum of additions for the 13 files whose path starts with `tests/`,
independently rechecked from the paginated PR #133 files API on 2026-10-25, not
final file lengths. The updated body equals the original with only these two
replacements; reversing them recovers the original bytes. There is no external
#133 deferral. Full verification is in `216-coordination.md`.

## Overlap gate and proposed coordination comments

AGENTS.md/backend rules forbid editing another open PR's files without a comment
there. The user explicitly authorized the exact comments below with `ок` on
2026-10-25, separately from general plan approval. After refreshing every open
PR's file list and checking for duplicates, the exact comments were posted once
on #290/#280/#229/#227/#283/#277/#220. #252 had already merged and was omitted.
New overlap #291 received separate explicit approval (`Да, опубликовать`) and its
exact comment was posted once. All current shared-file gates are cleared; refresh
open PR file lists again before development/publishing as required by this plan.
See `216-coordination.md` for actual overlaps and confirmed comment URLs.

| Target | Proposed exact comment |
| --- | --- |
| [#290](https://github.com/larchanka-training/dmc-268-api-t6/pull/290) | `Coordinating #216: I plan mutation-sensitive Reflection tests and small hygiene changes in application/reflection_pass.py, application/execute_review.py, application/review_output.py and infrastructure/run_repository.py under app/modules/reviews/, plus tests/test_reflection_pass.py, tests/test_reflection_evaluation.py, tests/test_execute_review.py and tests/test_rules_md_injection.py. Scope: public rendering calls, shared ID validation, fixture imports, internal-error classification and removal of the unused allowed_v2 local. I will preserve #130 gateway interfaces/decision persistence, rebase against merged main, and adapt the tests if your interfaces land first.` |
| [#280](https://github.com/larchanka-training/dmc-268-api-t6/pull/280) | `Coordinating #216: I plan to move test-only make_* factories out of app/modules/reviews/application/policy_snapshot.py, name the unchanged default policy budget, and remove the unused allowed_v2 local in infrastructure/run_repository.py. I will preserve publication pins and snapshot fields and rebase if #212 lands first.` |
| [#229](https://github.com/larchanka-training/dmc-268-api-t6/pull/229) | `Coordinating #216: planned overlaps are app/modules/reviews/application/review_output.py and findings_post_processor.py (public strict service-defaults ID validator), infrastructure/run_repository.py (inline unused allowed_v2=None), tests/test_review_output.py and tests/test_findings_post_processor.py (newline regression), tests/test_rules_md_injection.py (fixture import), and tests/test_sqlalchemy_run_repository.py if needed for preserved legacy behavior. No DiffMap/publication-coordinate semantics change is intended; I will rebase and preserve your changes.` |
| [#227](https://github.com/larchanka-training/dmc-268-api-t6/pull/227) | `Coordinating #216: planned overlaps are app/modules/reviews/application/review_output.py and findings_post_processor.py (public ID validator), infrastructure/run_repository.py (unused allowed_v2=None), tests/test_review_output.py and tests/test_findings_post_processor.py (newline regression), tests/test_llm_gateway.py, tests/test_repo_conventions.py and tests/test_rules_md_injection.py (fixture imports), tests/test_sqlalchemy_run_repository.py if required, and docs/TEST_PLAN.md §3.4 (injection-test attribution and legacy evidence stripping). I will preserve overview/run.step changes and rebase as needed.` |
| [#283](https://github.com/larchanka-training/dmc-268-api-t6/pull/283) | `Coordinating #216: planned overlaps are app/modules/reviews/application/review_output.py and findings_post_processor.py (strict public ID validator), infrastructure/run_repository.py and tests/test_sqlalchemy_run_repository.py if needed (preserve legacy None attribution), and docs/TEST_PLAN.md §3.4 (injection assertions and legacy evidence stripping). I will preserve clean/empty-addition behavior and rebase if #213 lands first.` |
| [#277](https://github.com/larchanka-training/dmc-268-api-t6/pull/277) | `Coordinating #216: I plan only docs/TEST_PLAN.md §3.4 corrections for reviewer versus verifier injection-test references, [INST] remaining as untrusted user data, and legacy evidence_refs stripping. #211 owns the nearby AC7/Option(a), non-code response and implementation-status wording. I will preserve that scope and rebase after your merge.` |
| [#252](https://github.com/larchanka-training/dmc-268-api-t6/pull/252) | `Coordinating #216: docs/TEST_PLAN.md §3.4 will identify separate reviewer/verifier injection tests, clarify [INST] as user data, and document legacy evidence_refs stripping. No corpus, metric, paid-capture or A/B harness changes are intended; I will preserve your evaluation additions and rebase as needed.` |
| [#220](https://github.com/larchanka-training/dmc-268-api-t6/pull/220) | `Coordinating #216: I plan a small hygiene edit in app/modules/reviews/infrastructure/run_repository.py to inline the unused allowed_v2=None local while retaining its fail-closed legacy behavior. I will preserve GitLab intake/credentials/publication behavior and rebase against your merge.` |
| [#291](https://github.com/larchanka-training/dmc-268-api-t6/pull/291) | `Coordinating #216: I plan only docs/TEST_PLAN.md §3.4 corrections for reviewer versus verifier injection-test references, [INST] remaining as untrusted user data, and legacy evidence_refs stripping. I will preserve #291's test-strategy updates and rebase after your merge.` |

#289 run-pins, #260 historical-plan cleanup and #254 RAG are read-only dependencies
for this plan: do not edit their unique owned paths, so no comments there. The
120 plans are not in #260's current file list. No AGENTS/rule/skill updates are
planned. Start non-overlapping proof/schema/envelope work while comments or
merges for shared files are pending, but do not temporarily mutate an owned
production file before the coordination gate either.

## Verification, review and publishing

Use uv throughout. First `uv sync --locked`; establish a full pristine baseline.
An unrelated PostgreSQL container occupies host 5432. Do not reuse, migrate or
stop it. Start only postgres/rabbitmq from this worktree with a unique disposable
compose project `dmc216`, `POSTGRES_PORT=55432`, `RABBITMQ_PORT=55672`,
`RABBITMQ_MANAGEMENT_PORT=55673` (check availability), and dedicated volume names
provided by the project. Use explicit local fake credentials if environment values
would otherwise be inherited. Docker socket/network and this managed worktree's
writes may require sandbox escalation; no services have been started yet.

Create isolated RabbitMQ vhost `test216`, grant app permissions, and use:

```sh
DMC216_WORKTREE="$(git rev-parse --show-toplevel)"
cd "$DMC216_WORKTREE"
export TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:55432/app
export TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:55672/test216
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest -rs --strict-markers
```

Fixtures create/migrate/drop random PostgreSQL schemas; do not run integration
with xdist against one DB/vhost. Both env vars matter: PostgreSQL alone leaves
RabbitMQ tests skipped. Capture full passed/failed/skipped counts and require no
unexplained skips. A failed service startup, Alembic failure, baseline red test or
resource limit is a visible blocker to completion, not permission to skip gates.
Do not introduce dependency/config changes merely to hide unrelated failures.
Run focused tests per slice. The measured pristine pytest took 2,795.29s, so
checkpoints A/B/C use affected regression suites plus ruff, format and mypy, with
focused DB tests for relevant repository/persistence behavior. Run one full
quiescent pytest with both services after all changes, together with all four
final gates. Repeat the full suite only for a failure, rebase or a new concern
that invalidates that evidence; test-only intermediate milestones do not
otherwise trigger a repeated complete suite. This is a verification-frequency
refinement, not a reduction of final acceptance requirements.

After every task, update evidence/todo and have a fresh review agent use the
code-review skill against the recorded base; its Standards and Spec checks run
in independent parallel agents. Report hard violations separately from smells.
Resolve findings through development agents; no coordinator implementation.
Before publishing: rebase onto current main, refresh overlaps and current docs,
rerun affected proof/full gates, and obtain zero findings on both axes.

Publisher loads pull-request skill, uses `chore/216-reflection-tests-docs`, stages
only intended paths, commits conventionally with `Refs #216`, and never uses
--no-verify. PR title proposal: `test(reviews): harden reflection regressions (#216)`.
PR body: What / Why / How to verify / Refs, template checklist, exact mutations
and equivalent/superseded explanations, every hygiene disposition, any external
#133 deferral, and full test evidence. No closing keyword or Development link.
Readiness/reviewer is chosen by user per publishing skill; do not select a
reviewer or merge/close the issue. Attach the created PR to this chat. Board 12
status is part of issue DoD; do not change unrelated fields. The tech lead closes
#216 after AC verification; an approving review on final head and resolved threads
remain merge requirements, not claims provided by internal agent reviews.

## Risks and pending decisions

1. **Approval received:** user approved this plan/public seams with `ок` on
   2026-10-25, including the reasoned P3 19 retained-loader disposition.
2. **External actions completed:** all applicable authorized coordination comments
   and both #133 body corrections are verified in `216-coordination.md`. The new
   #291 comment was separately authorized and posted; recheck for new overlaps
   before future edits/publishing.
3. **Baseline changes:** #130/#212 and other active PRs may change fixtures,
   persistence and signatures; refresh baseline and update plans, never apply old
   issue assumptions blindly.
4. **Environment:** disposable PG/RabbitMQ, Docker/image access and writable
   worktree are needed for full proof. Actual green baseline is not yet measured.
5. **P3 19:** the approved plan retains the version/checksum checked package
   resource loader here; document its reasoned rejection and residual disk I/O.


## Integration B after synchronized main

Base is `4faa03dd7accfa161048f584b834b314fa1ab5b6`. The merged #290 suites
introduce three additional factory consumers, extending task 11's old nine to
twelve. After integration A's independent Standards and Spec reviews returned
zero findings, integration B moves their imports to the extracted test helper.
The bounded scope is `tests/test_gateway_answer_validation.py`,
`tests/test_gateway_verifier_model.py`, and `tests/test_llm_verify_trace.py`.
The coordinator explicitly approved adapting one fixture in the first file:
its old `rule_name="rules.md"` now fails the intentionally tightened schema
before the intended model-only semantic check. Positive integer coordinates
`line=30`, `start_line=31` preserve that semantic failure, with the first finding
still low severity before the later critical finding and the error still
reporting raw finding index 0. No production behavior or assertion is weakened.

All twelve consumers and seven additional gateway/verifier/trace suites pass
661 cases with no skips on the dedicated Linux runner and disposable PostgreSQL.
Global Ruff, format and mypy pass; exact evidence is in `216-verification.md`.
This is compatibility checkpoint evidence, not the final complete-suite proof.
