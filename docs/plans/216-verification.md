# Verification record for #216 / PR #303

This document distinguishes historical measurements from the current review work.
The [plan](216-plan.md), [checklist](216-todo.md) and
[coordination receipts](216-coordination.md) track remaining acceptance gates.
No historical full-suite result proves the current runtime-changing rebase.

## Historical round 1 measurements

| Source identity / slice | Result | Evidence boundary |
| --- | --- | --- |
| Pristine `e6d3077cfcb5584f8af0524ce4a0c8a658b2c5fb` | 5,684 passed, 2 failed, 0 skipped; 2,795 s | Original full run was red. Code-index timing and origin-concurrency failures each passed three unchanged focused reruns; their causes remain unproven. |
| Original final source `4faa03dd7accfa161048f584b834b314fa1ab5b6` | 6,521 passed, 0 failed, 0 skipped, 2 warnings; 1,680.47 s | Full Linux run with dedicated PostgreSQL/RabbitMQ. All 594 executable input hashes matched before/after. |
| Original published `bf9d4c6ddb3522567476af85a92c3acac06a3f3e` | CI 6,521 passed, 0 skipped; 103 isolated live mutation probes killed/restored | Historical only. Adoption of `f8b13103` then changed documentation only; later main changed runtime. Review found an additional live survivor outside those 103 probes. |
| R1.1: local before rebase, with dirty fixes | Collection 6,608; run-pin PostgreSQL slice 9 passed, 0 skipped, 2 existing warnings | Deleted-factory import failed first; minimal import migration restored collection. AST audit: 603 tracked Python files, 13 test consumers, five factory definitions only in the test helper, no stale production imports or production-to-tests dependency. |
| R1.2: decision/publication/parser | Six isolated mutant cycles; restored three-file suite 125 passed after every cycle | Step-3 status expansion, two allowlist removals, warning threshold and two newline relaxations. |
| R1.3: verifier source list | Six semantic mutant failures; restored evaluation file 55 passed after each | Three requirement-source members × both prompt versions. Immutable prompt bytes/checksums unchanged. |
| R1.4: missing-document helper reuse | Existing baseline 362 passed; expanded affected slice 950 passed, 0 skipped, 2 existing warnings | Seven document values, 12 evaluation snapshots/digests, eight fake-provider requests and helper signature preserved. Combined capture SHA-256 `980579f86307074daa2ddf8dbfa58f7a7d308aa241d909d99ca6df830966d12f`. |
| R1.5: five contract documents | Named-code/test/link/resource/stale-claim and diff checks passed | Documentation only; no fresh runtime result claimed. |
| R1.6a: local mutation run; driver not in repository | 120 catalog records validated; 16 representative live probes killed/restored; 12 safety cases passed | Includes all ten new review probes. This is **not** a complete 113-live-probe run. Ruff passed; format 601 files; mypy 591 source files. |
| R1.6b: four evidence documents | Portability, link, named-node, command and bounded-file audits passed | Documentation only; fresh Standards = 0, Spec = 0. |
| R1.7: provider compatibility disposition | Strict transport traced; official sources checked; local contract digests reproduced | Live acceptance UNKNOWN. Unchecked F2 gate recorded in #130 plan/todo, linked to #217; 185 offline tests passed, zero skips. Fresh independent reviews: 0/0. |
| R1.8 preflight: base `9243fd67c1d4ed3beba2bfbab12d8ed85ee22f4e`, HEAD `f00d1b925a42f1990ad3f77b0fa349ab5ceb37cd` plus dirty fixes | Collection 6,655; bounded six-file slice 178 passed, 0 skipped, 2 existing warnings; Ruff passed, format 604 files, mypy 594 source files | Required `checked_paths` call argument first failed eleven cases and mypy, then passed after the minimal integration fix. The local run accounted for 120 mutation records after nine audited fingerprint updates. This bounded preflight precedes the final classifier fix and final proof below. |
| R1.8 final: same base/HEAD plus reviewed dirty fixes | **6,660 passed, 0 failed, 0 errors, 0 skipped, 2 existing warnings; 923.56 s**. Global Ruff passed; format 604 files; mypy 594 source files. **113/113 live probes killed/restored GREEN**, zero unexplained survivors. | Dedicated PostgreSQL/RabbitMQ and isolated task vhost; all 1,207 input hashes matched before/after complete replay and full pytest. Fresh whole-PR Standards = 0, Spec = 0, reconfirmed after the final classifier fix. Code/evidence publication, exact-head CI and owned-resource cleanup subsequently completed; see the publication proof below. |

R1.0–R1.7 and the final R1 evidence each completed independent Standards and
Spec reviews with zero findings. The two full-suite warnings were existing
Starlette/httpx and anyio deprecations. These results predate the R2 rebase.

## Local mutation accounting

**Local run; driver not in repository.** The replay driver, catalog and their
safety-test module were removed in R2 by the tech lead's scope decision. Existing
mutation-killing regression tests remain. The tables below retain measured trials
and domain dispositions; they are not a supported executable mutation command or
proof of the current rebased tree.

| Trial origin | Live | Equivalent | Diagnostic only | Superseded |
| --- | ---: | ---: | ---: | ---: |
| Historical 97-member inventory | 92 | 4 | 1 | 0 |
| Original additional probes | 11 | 0 | 0 | 0 |
| R1 review regressions | 10 | 0 | 0 | 0 |
| Historical sites outside the 97 | 0 | 0 | 0 | 2 |
| **Total: 120 records** | **113** | **4** | **1** | **2** |

The original 103 live executions were bounded measurements. The step-3
`insufficient_context`/empty-reference survivor and masked source-list assertion
were discovered later and independently covered by the R1 regression tests.
The completed R1 local run killed and restored all 113 live probes. The first
attempt had stopped after 15 kills on a classifier false rejection; that attempt
was not counted as complete. Seventeen driver safety cases and eight representative
probes passed before the successful complete run. The deleted tool is outside
this PR's final deliverable.

The R1 final measurement used base `9243fd67` and published ancestor `f00d1b92`
plus reviewed dirty files. Its 1,207-path input identity was
`1d4a19f5983de54234f5f109bc5c4d04636ba53fff3bf1ae85418f794ea85192`;
all hashes matched before/after mutation trials and pytest. Its 1,203-path
projection excluding the four #216 metadata documents was
`3dc621081dd795d1232689dc6b2e527037d41ac7a5b8156d8787ed784122616a`.
Those historical identities included the subsequently removed tool and cannot
identify the R2 tree. No removed-tool digest or reproducibility claim is current.

## M01–M15 coverage map

The table names representative committed public killers from the historical local
run; driver not in repository. Test names below are selectable pytest node prefixes;
parameterized cases expand normally. Prompt semantic kills failed meaning assertions,
rather than only checksum or budget guards.

| Group | Invariant and live probes | Representative committed killer |
| --- | --- | --- |
| M01 | Exact stable IDs; foreign/case-folded IDs rejected (2 live; 1 superseded). | `tests/test_reflection_pass.py::test_reflection_pass_service_defaults_stable_id_and_legacy_slug_rejection` |
| M02 | Existing unique checks and shown reference lines; builder rejects duplicates (3 live; 1 equivalent; 1 superseded). | `tests/test_reflection_pass.py::test_reflection_pass_validates_service_defaults_check_id` |
| M03 | Every evidence/requirement reference and attributed status validated; insufficient empty-ref candidate rejected without invalidating the pass (6 live). | `tests/test_reflection_pass.py::test_reflection_pass_rejects_attributed_insufficient_context_without_requirement_refs` |
| M04 | Final system+user and mandatory envelope budgets include exact-boundary cases (5 live; 1 diagnostic). | `tests/test_reflection_pass.py::test_reflection_pass_final_budget_counts_system_and_user`; `::test_verifier_mandatory_budget_accepts_exact_limit` |
| M05 | Pinned raw verifier checksum is nonempty and matches (1 live; 1 equivalent). | `tests/test_reflection_pass.py::test_reflection_pass_rejects_blank_verifier_checksum_before_model_call` |
| M06 | Typed run policy cap respected above/below the shared 12,000 default (4 live). | `tests/test_policy_budget.py::test_pre_send_guard_uses_run_policy_cap`; `::test_execution_profiles_keep_default_policy_cap` |
| M07 | Defaults names/check text escaped as untrusted XML user data (2 live). | `tests/test_reflection_evaluation.py::test_service_defaults_fields_are_escaped_in_both_user_envelopes` |
| M08 | Entire under-budget policy rendered, including late citation; over-budget rejected (1 live). | `tests/test_reflection_evaluation.py::test_under_budget_long_rules_render_all_lines_in_both_prompts`; `::test_reflection_validates_late_requirement_against_long_policy` |
| M09 | Insufficient-context decisions never published (1 live). | `tests/test_execute_review.py::test_execute_review_snapshot_never_publishes_insufficient_context` |
| M10 | Every candidate checked; supported rules/AGENTS/defaults preserved; exact rules-only and rules+AGENTS allowlists (5 live). | `tests/test_execute_review.py::test_execute_review_snapshot_checks_attribution_after_first_candidate`; `::test_execute_review_snapshot_preserves_supported_policy_attribution` |
| M11 | Trailing newline rejected independently by model, dict/string/bytes parser and repair boundary (3 live). | `tests/test_review_output_v2.py::test_v2_parser_rejects_rule_id_trailing_newline`; `tests/test_findings_post_processor.py::test_postprocessor_preserves_findings_and_repairs_only_invalid_service_default_ids` |
| M12 | Manifests require stage/blocks, count shown RIGHT lines across gaps and omit run identity without snapshot (4 live). | `tests/test_prompt_builder_markdown.py::test_public_manifest_counts_only_shown_right_lines_across_gaps`; `tests/test_policy_snapshot.py::test_call_manifest_constructor_requires_stage` |
| M13 | Schema/model closed objects, lexical parity and independent at/over bounds; no vacuous empty-array restriction (49 live; 2 equivalent). | `tests/test_schemas_v2.py::test_schema_and_model_close_every_object`; `::test_schema_and_model_string_limits`; `::test_schema_and_model_array_bounds`; `::test_rule_name_schema_and_model_share_lexical_domain` |
| M14 | Both packaged versions retain trust, all judgments, one-decision contract, JSON example, exact requirement-source list and separate code evidence (24 live). | `tests/test_reflection_evaluation.py::test_verifier_prompt_distinguishes_code_evidence_from_policy_check_sources`; `::test_verifier_prompt_defines_each_judgment_from_visible_context` |
| M15 | Severity filtering precedes clean summary/count publication; warning retains low and drops info (3 live). | `tests/test_execute_review.py::test_execute_review_snapshot_warning_keeps_supported_low_and_drops_info`; `::test_execute_review_run_severity_threshold_critical_drops_medium_clean_summary` |

### Four domain-equivalence proofs

1. **M02 downstream duplicate-ID guard:** in supported production state the pinned
   registry is immutable; the same frozen rules are rendered, and duplicate IDs
   reject in the builder before provider await. Rebuilding the same IDs after the
   await cannot introduce a duplicate. Without a snapshot, nonempty rules fail
   missing checksum before insertion; empty rules cannot duplicate. The reachable
   builder guard has a separate live probe. Mutation of application globals by a
   provider is outside this supported domain.
2. **M05 isolated empty-check term:** SHA-256 of the raw text is always a nonempty
   64-character hex string. Empty/absent expected checksum already differs from
   that actual digest, so removing only `not expected_checksum` changes no typed
   accepted input. The permissive allow-empty short circuit is a separate live probe.
3. **M13 verifier `evidence_refs.minItems: 0` removal:** every array already has a
   nonnegative length; deleting zero does not widen its domain. Narrowing to one
   is independently non-equivalent and tested.
4. **M13 verifier `requirement_refs.minItems: 0` removal:** the same length argument
   applies independently; narrowing to one has its own live probe.

**Diagnostic-only M04:** changing the mandatory-envelope equality check can change
which diagnostic appears before the following zero-budget guard. It is not strict
equivalence, and is outside the required original final-send-guard acceptance.

**Two superseded sites:** #230 removed the historical exact/lowercase slug map and
secondary positional `any` expression. Their original sites are absent; they are
superseded rather than equivalent. Current stable-ID, line and duplicate probes
retain their relevant intent without fabricating a current mutation site.

## Original seven hygiene dispositions

These are the original issue's P3 numbers, separate from the eleven round-1 P3 items
mapped in the plan. New dependencies, prompt byte edits and worker rollout remain
outside scope.

| Original item | Final disposition and evidence |
| --- | --- |
| P3 №9 — external private renderers | Public `PromptBuilder.render_policy_rules`, `render_policy_agents`, `render_service_defaults`, `render_conventions` and `render_changed_files`; external callers migrated. Public XML escaping reused. Shared private policy rendering stays module-local. |
| P3 №10 — dead 12,000 fallback | Read typed `ExecutionProfile.max_policy_tokens` directly; `DEFAULT_MAX_POLICY_TOKENS` supplies one shared default. M06 tests exact/one-over caps for fast/deep and configured caps on both sides of the default. |
| P3 №11 — application test factories | Five factories live in `tests/policy_snapshot_fakes.py`; all 18 R2-audited consumers migrated. Eval uses public production dataclasses/helpers and never imports tests. R1.4 reuses public `policy_documents.missing_document` with byte/value parity. The historical R1.8 recount was 13; main F1 added five consumers, migrated in R2. |
| P3 №16 — private ID regex | Public strict ID validator shared by parser/model and repair; M11 independently covers all public boundaries, M13 covers schema lexical parity including trailing newline. |
| P3 №17 — missing Reflection | Missing component raises existing `RunFailure("internal_error")` before draft/publication. This deliberately changes old terminal policy failure to **retryable server failure: three total attempts, then failed/dead-letter to `reviews.dlq`**. Actual unready/corrupt snapshot remains integrity failure. |
| P3 №19 — verifier prompt disk read | Deliberately retain version-selected package loading and raw-checksum verification. Main F1 retains a lazy per-attempt cached prompt builder. A pinned-content resolver remains outside #216 scope; package I/O and raw-checksum verification stay explicit. |
| P3 №20 — inert `allowed_v2` local | `SqlAlchemyReviewOutputRepository.get_post_processing_input` passes `allowed_v2_rules=None` inline. Snapshot-row existence alone never preserves unverified v2 attribution; the explicit pipeline carries a separate `VerifiedReviewOutput` allowlist. |

Adjacent unreadable verifier resources and duplicate defaults check IDs deliberately
retain terminal `policy_snapshot_integrity_error`: they validate/load bound material.
Broadening them would independently change retries beyond this missing-component
fix. The PR body must state this bounded disposition and the three-attempt consequence.

`tests/test_execute_review.py::test_execute_review_fail_closed_when_snapshot_present_without_reflection_pass`
asserts server classification and no draft/publication. Its neighboring
`::test_execute_review_fail_closed_when_snapshot_not_ready` preserves the integrity code.
`tests/test_review_run_worker.py::test_retryable_failures_retry_twice_then_fail_into_the_dead_letter_queue`
covers exhaustion; `::test_failures_without_retry_fail_at_once_and_are_acked` covers terminal codes.

## Contract documentation and rollout boundary

The actual legacy adapter method owner is `SqlAlchemyReviewOutputRepository`.
Active conventions/verifier DB row IDs are captured on run creation and restored
by ID/version/checksum; fixture/dataclass v2 defaults do not choose every run's
runtime prompt. Legacy missing pins stay absent/incomplete. Main F1 now supplies
a lazy Reflection pass through the production attempt provider; version selection
keeps legacy runs on their existing path. #216 does not activate v4.

Reviewer injection tests and verifier-only injection tests are named separately in
[TEST_PLAN](../TEST_PLAN.md). `[INST]` is absent from system instructions but can
remain literal untrusted user data. Both verifier versions declare Source Authority
and Trust. Legacy output strips `evidence_refs`; explicit verified output carries
provenance, while persisted verify trace excludes free-text reason and keeps null
`manifest_id`. Historical #133 did not prove mixed-case/exact-key slug precedence;
#230 superseded that mapping. Summary cleansing applies after any rejection,
including partial rejection. [PR #133](https://github.com/larchanka-training/dmc-268-api-t6/pull/133)
body corrections were independently read back: 13 changed test files, 6,472 added
lines, corrected partial-rejection wording. Full body fingerprints are in coordination.

The absolute-end `(?![\s\S])` lookahead passes unchanged through
`answers.provider_schema` (only `$schema`/`$id`/`$comment` stripped) into transport's
`strict: true` schema. Reviewer v4 selects v2; v1–v3/no snapshot select v1.
[RE2 explicitly excludes negative lookahead](https://github.com/google/re2/wiki/syntax),
but that is not proof EUrouter uses RE2. The [EUrouter chat contract](https://www.eurouter.ai/docs/api/chat)
and [Mistral structured-output guide](https://docs.mistral.ai/studio/conversations/structured-output/custom)
do not establish this pattern's acceptance. [EUrouter routing](https://www.eurouter.ai/docs/concepts/routing)
may select different serving providers, so a model alias alone is insufficient.
The known catalog entries `mistral-small-4` and `mistral-small-3.2-24b` supply
EUrouter endpoint defaults; they do not automatically select the runtime routes.
`LLM_MODEL` is required and review fallback remains absent unless
`LLM_FALLBACK_MODEL` is configured. Actual deployment configuration was not inspected.

**Live provider acceptance: UNKNOWN.** The unchecked [#130 F2 release gate](130-verifier-gateway-plan.md#pr-f2-release-gate-strict-v2-schema-acceptance-pending-217)
and [checklist](130-verifier-gateway-todo.md#remaining-pr-f2--provider-acceptance-release-gate-217),
linked to [#217](https://github.com/larchanka-training/dmc-268-api-t6/issues/217),
block v4 activation, not dormant F1 preparation. They require frozen raw schema and
canonical strict payload hashes plus synthetic full request/SHA, intended
primary/fallback and observed serving provider/model, restrictions, UTC timestamp,
request/run ID and unrepaired valid outcome. Test every allowed serving route or
restrict rollout to tested routes. Schema 400, repair/fallback substitution,
`prompt_json`, mocks/local parity and historical v1 runs do not prove intended
strict-route acceptance. No paid/live call or activation was performed.

Current frozen raw schema SHA-256 is
`d3755ccfac5088b7f5e988d9004b7724897f660d065fc842d793285a313301ac`;
canonical complete `response_format` SHA-256 is
`4558c8250dbbec8e85fe0d71e79013c64eeea19d784cdb491f4d65319b90a673`.
The #130 plan includes a credential-free local reproduction command. Local schema,
parser newline and gateway selection/transmission tests preserve the contract;
their offline result must not be relabeled as remote provider acceptance. The existing
`tests/test_gateway_answer_validation.py`, `tests/test_schemas_v2.py` and
`tests/test_review_output_v2.py` suites passed: **185 passed, 0 skipped**.

## Run the canonical gates

Prerequisites: Git checkout, uv, Python 3.13 and a supported Linux toolchain
(including Bash, jq and Git for workflow tests). Provision dedicated PostgreSQL
and RabbitMQ with an isolated task vhost, and verify readiness first. The URL
placeholders are caller inputs, not pre-existing personal resources.

```sh
uv sync --locked --all-groups
export TEST_DATABASE_URL='<disposable-db-url>'
export TEST_RABBITMQ_URL='<isolated-task-vhost-url>'
export RABBITMQ_URL="$TEST_RABBITMQ_URL"
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Record actual base/head and dirty-input identity, counts, warnings and unexplained
failures/skips; compare source hashes before/after. A runtime-changing rebase needs
fresh proof. Historical mutation trials were a local run; driver not in repository.
Remove only resources created for verification and retain shared infrastructure.

## Current round 2 integration and verification

Rebased the nine published #216 commits onto main `1417faa716294865c196decdf5f19867237d234b`,
which includes #324 and F1. The rebased old tip is `f145b70c34eec48a837b5248311e3ce79eb6b5cd`.
Expected conflicts in `prompt_builder.py`, `reflection_pass.py` and
`tests/test_execute_review.py` retained main's F1 gateway/snapshot flow, exact
budget fitting, cited context excerpts and F1 regression tests, alongside #216's
public rendering/XML helpers and mutation-killing tests. The three replay-tool
files are deleted. F1 added five factory-consumer files and additional local imports
in an existing consumer; all six files now import the existing test helpers. The
missing `RunFailure` import is restored, the new F1 missing-component test reflects
the accepted retryable failure, and the exact-budget regression uses the pinned
profile cap rather than removed environment overrides. Fresh independent
implementation reviews reported Standards = 0 and Spec = 0. The [#217 gate
comment](https://github.com/larchanka-training/dmc-268-api-t6/issues/217#issuecomment-6413779899)
was posted and read back; provider acceptance remains UNKNOWN. The R1 counts
above are historical.

The current measured tree is **rebased ancestor `f145b70c` plus reviewed R2 dirty
changes**, not that clean commit alone. Its 1,223-file projection excludes exactly
`docs/plans/216-plan.md`, `216-todo.md`, `216-verification.md` and
`216-coordination.md`. It hashes the compact sorted JSON path-to-SHA-256 mapping
(`json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()`) with
SHA-256:
`36f1a8eee8ceb851aff6d41a8daae435b530c0cbf220cfbe0566852ac73386d3`.

| R2 verification boundary | Observed result |
| --- | --- |
| Fast integration slice | Global Ruff passed; mypy 611 source files; seven test files 236 passed, two existing warnings, 11.94 s. |
| First complete canonical run | **RED: 6,983 passed, 1 failed, 0 skipped, two existing warnings; 2,161.64 s**. `test_first_signal_deadline_bounds_running_work_and_runner_teardown[blocked_loop]` hit its unchanged eight-second subprocess timeout with no stdout. All 1,223 source hashes matched before/after. This is not a successful full gate. |
| Unchanged shutdown file on bind-mounted source | 4 failed, 9 passed; all four failures were eight-second subprocess timeouts, while the initially failing blocked-loop variant passed. |
| Unchanged shutdown file on native Linux-volume source | Three complete 13-case runs: **39/39 passed**. Same interpreter, dependencies, services, resource caps, assertions and timeout; all 1,223 input hashes matched the worktree. |
| Fresh complete native-copy canonical run | **GREEN**: Ruff passed; format **621 files**; mypy **611 source files**. Full pytest: **6,984 passed, 0 failures, 0 errors, 0 skipped, two existing warnings; 1,156.88 s** (JUnit duration 1,156.689 s). All 1,223 hashes matched before/after and match the live worktree. |

`app/worker.py`, `app/bootstrap/worker_shutdown.py` and
`tests/test_worker_shutdown_process.py` are identical to main `1417faa7`.
A bounded import-only comparison measured 4.75–8.61 s total startup/exit on the
bind-mounted source versus 1.98–2.44 s on the byte-identical native Linux-volume
copy. This supports startup/filesystem latency as an explanation; it **does not
prove the exact causal stage of the original uninstrumented timeout**. No product
source, tests, assertions or timeouts were changed, and the first red run is not
waived as a proven infrastructure flake. Focused passes do not replace the full
canonical rerun. The native copy's actual module origin was checked, and both
projections were unchanged after the focused diagnostics.

The complete native-copy run has finished GREEN, with the same source identity
before/after; the live worktree matches that projection. It used the same Python
interpreter, dependencies, services and resource caps as the first run. The two
warnings are the existing Starlette/httpx and anyio deprecations. Only the four
excluded evidence documents changed during verification. The first red run and
its unproven timeout cause remain recorded; the successful complete rerun supplies
the current gate proof. Fresh independent final evidence reviews reported
Standards = 0 and Spec = 0. Publication, exact-head CI and owned-resource cleanup
remain pending.

## Historical round 1 publication and cleanup

The reviewed code/evidence was published as `77d544edc8636284d6131cd7a1baf0884dc7b8bb` over base `9243fd67c1d4ed3beba2bfbab12d8ed85ee22f4e`. All 1,207 pre-commit input bytes were preserved across six focused review-fix commits. The final evidence delta passed fresh independent Standards/Spec reviews with zero findings.

[CI run 40219321992](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40219321992) completed **successfully on that exact published head**. Unit tests: **5,620 passed**; integration shards: **268 + 258 + 330 + 184 passed**; total **6,660 passed, zero test skips**. CI reported eight unit warnings and two warnings per integration shard; these are separate from the two warnings in the local full-suite measurement. Four conditional image-publish/deployment jobs were skipped for the PR; those are job conditions, not skipped tests. All executed lint/type/configuration/security/build/smoke jobs succeeded.

The PR body and four SHA-bearing P2 replies were read back exactly. One summary covers all eleven P3 items and the #130 F1/#304 merge-order coordination. The exact repository issue remains OPEN and its Board 12 item On Review. PR #303 remains non-draft with no new requested reviewer and no closing issue links. The four round 1 reviewer-owned threads were subsequently closed by the reviewer on 26 October. No merge, issue closure or approval was performed. Receipt links are in [coordination](216-coordination.md).

After that successful CI, ownership labels and network endpoints were verified before removing exactly three owned containers, four volumes and one network. The owned-resource inventory is empty; evidence was archived, and the shared image, managed worktree and backup refs/stashes were preserved.

That R1 publication-metadata continuation changed only the four #216 documents. Its historical 1,203-entry projection excluding precisely those documents remained `3dc621081dd795d1232689dc6b2e527037d41ac7a5b8156d8787ed784122616a`. The code/evidence SHA above is a verified historical checkpoint; later documentation commits do not claim that a commit can contain its own SHA. Current PR checks are available from [PR #303](https://github.com/larchanka-training/dmc-268-api-t6/pull/303/checks).
