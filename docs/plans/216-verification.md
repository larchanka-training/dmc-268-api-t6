# Verification and reproduction for #216 / PR #303

This document distinguishes historical measurements from the current review work.
The [plan](216-plan.md), [checklist](216-todo.md) and
[coordination receipts](216-coordination.md) track remaining acceptance gates.
No historical full-suite result proves the current runtime-changing rebase.

## Measured baselines and current state

| Source identity / slice | Result | Evidence boundary |
| --- | --- | --- |
| Pristine `e6d3077cfcb5584f8af0524ce4a0c8a658b2c5fb` | 5,684 passed, 2 failed, 0 skipped; 2,795 s | Original full run was red. Code-index timing and origin-concurrency failures each passed three unchanged focused reruns; their causes remain unproven. |
| Original final source `4faa03dd7accfa161048f584b834b314fa1ab5b6` | 6,521 passed, 0 failed, 0 skipped, 2 warnings; 1,680.47 s | Full Linux run with dedicated PostgreSQL/RabbitMQ. All 594 executable input hashes matched before/after. |
| Original published `bf9d4c6ddb3522567476af85a92c3acac06a3f3e` | CI 6,521 passed, 0 skipped; 103 isolated live mutation probes killed/restored | Historical only. Adoption of `f8b13103` then changed documentation only; later main changed runtime. Review found an additional live survivor outside those 103 probes. |
| R1.1: base `ef4e8b68b9443509a84f0c0ec273d08837b425c6`, HEAD `a73e1a9d51729b067f2e3b7cf846997f7c0358a6` plus dirty fixes | Collection 6,608; run-pin PostgreSQL slice 9 passed, 0 skipped, 2 existing warnings | Deleted-factory import failed first; minimal import migration restored collection. AST audit: 603 tracked Python files, 13 test consumers, five factory definitions only in the test helper, no stale production imports or production-to-tests dependency. |
| R1.2: decision/publication/parser | Six isolated mutant cycles; restored three-file suite 125 passed after every cycle | Step-3 status expansion, two allowlist removals, warning threshold and two newline relaxations. |
| R1.3: verifier source list | Six semantic mutant failures; restored evaluation file 55 passed after each | Three requirement-source members × both prompt versions. Immutable prompt bytes/checksums unchanged. |
| R1.4: missing-document helper reuse | Existing baseline 362 passed; expanded affected slice 950 passed, 0 skipped, 2 existing warnings | Seven document values, 12 evaluation snapshots/digests, eight fake-provider requests and helper signature preserved. Combined capture SHA-256 `980579f86307074daa2ddf8dbfa58f7a7d308aa241d909d99ca6df830966d12f`. |
| R1.5: five contract documents | Named-code/test/link/resource/stale-claim and diff checks passed | Documentation only; no fresh runtime result claimed. |
| R1.6a: portable replay | 120 catalog records validated; 16 representative live probes killed/restored; 12 safety cases passed | Includes all ten new review probes. This is **not** a complete 113-live-probe run. Ruff passed; format 601 files; mypy 591 source files. |
| R1.6b: four evidence documents | Portability, link, named-node, command and bounded-file audits passed | Documentation only; fresh Standards = 0, Spec = 0. |
| R1.7: provider compatibility disposition | Strict transport traced; official sources checked; local contract digests reproduced | Live acceptance UNKNOWN. Unchecked F2 gate recorded in #130 plan/todo, linked to #217; 185 offline tests passed, zero skips. Fresh independent reviews: 0/0. |
| R1.8 preflight: base `9243fd67c1d4ed3beba2bfbab12d8ed85ee22f4e`, HEAD `f00d1b925a42f1990ad3f77b0fa349ab5ceb37cd` plus dirty fixes | Collection 6,655; bounded six-file slice 178 passed, 0 skipped, 2 existing warnings; Ruff passed, format 604 files, mypy 594 source files | Required `checked_paths` call argument first failed eleven cases and mypy, then passed after the minimal integration fix. All 120 catalog records validate after nine audited fingerprint updates. This bounded preflight precedes the final classifier fix and final proof below. |
| R1.8 final: same base/HEAD plus reviewed dirty fixes | **6,660 passed, 0 failed, 0 errors, 0 skipped, 2 existing warnings; 923.56 s**. Global Ruff passed; format 604 files; mypy 594 source files. **113/113 live probes killed/restored GREEN**, zero unexplained survivors. | Dedicated PostgreSQL/RabbitMQ and isolated task vhost; all 1,207 input hashes matched before/after complete replay and full pytest. Fresh whole-PR Standards = 0, Spec = 0, reconfirmed after the final classifier fix. Code/evidence publication, exact-head CI and owned-resource cleanup subsequently completed; see the publication proof below. |

R1.0–R1.7 each completed fresh independent Standards and Spec reviews with zero
findings on both axes. R1.8 preflight adopted `9243fd67`, preserving upstream
checked-paths, trace/persistence, severity thresholds and run pins. All five factory
definitions remain test-only: 13 test consumer files, 606 tracked Python files,
no stale production-factory imports or production-to-tests dependency. Nine catalog
edit entries across `review_output.py` and `findings_post_processor.py` retained
their exact anchors, contexts, occurrence selections and intended invariants;
only their stale target-file fingerprints changed in that integration slice. Prompt/schema bytes and mutation semantics were preserved. A later narrow classifier fix correctly recognizes pytest's rewritten `E assert` and expected `DID NOT RAISE TypeError` reports while rejecting incidental exception text in unexpected RuntimeError/TypeError reports. The first replay attempt stopped after 15 kills at that false classifier rejection and restored exactly; it was not a surviving behavioral mutant. Seventeen driver safety cases and eight real representative probes passed before the complete 113-probe replay and full suite were rerun successfully.

The two full-suite warnings are existing dependency deprecations: Starlette testclient's httpx integration and the anyio BlockingPortal alias. No test was skipped and no failing baseline result was waived.

## Portable mutation inputs and accounting

The committed [catalog](../../review/scripts/reflection_mutations.json) is the
complete executable mapping: stable IDs, classifications, exact guarded edits,
source fingerprints, named public test selections and accepted behavioral failures.
The [driver](../../review/scripts/replay_reflection_mutations.py) validates all
records by default. [Safety tests](../../tests/test_reflection_mutation_replay.py)
cover stale/ambiguous edits, unsafe paths, escaping symlinks, interruption,
collection/setup/checksum false positives, import provenance, timeout child-process
termination and changed-identity reuse. No private supplemental test is required.

| Catalog origin | Live | Equivalent | Diagnostic only | Superseded |
| --- | ---: | ---: | ---: | ---: |
| Historical 97-member inventory | 92 | 4 | 1 | 0 |
| Original additional probes | 11 | 0 | 0 | 0 |
| R1 review regressions | 10 | 0 | 0 | 0 |
| Historical sites outside the 97 | 0 | 0 | 0 | 2 |
| **Total: 120 records** | **113** | **4** | **1** | **2** |

The original 103 live executions were bounded measurements, not complete mutation
coverage. Adding `insufficient_context` to step 3's matching-requirement-ref status
set survived because no attributed empty-ref candidate exercised it. R1.2 now
proves successful verification followed by per-candidate rejection for both rules
and AGENTS attribution. The old source-list assertion also allowed removal of its
`service_defaults` member because a later sentence still contained that tag.
R1.3 isolates the actual three-member requirement-source clause in both versions.
Neither gap is hidden by the old zero-survivor statement.

Historical reviewed input identities at R1.6a:

| Input | SHA-256 |
| --- | --- |
| Driver raw bytes | `f03d52f899b382ece7bc7dcf4d9e91ff48c068268744462d39d57814ac9cb6b7` |
| Catalog raw bytes | `9c45341ed9c21ee2219e1a0525a74b4aeedf1591ba71aa4fc3fd3f179ac88285` |
| Catalog canonical JSON used by `identity.json` | `b0f948594c93116fbde76a219655511d16600ea5eb28476d5787eceb5fc02401` |
| Safety test raw bytes | `f3546600d27c4311191ae841c3e7d8b438ac8bffe10c8058453f0fe4374af797` |
| Representative snapshot input identity | `7a9bd4578681b059670daa9d3aa8d48c5b3dc4a2e2650e93020c04c41ec28018` |

The representative snapshot records HEAD `a73e1a9d` **and dirty source hashes**;
it does not attribute uncommitted fixes to that commit. Canonical and raw catalog
digests differ by serialization. Future source changes require explicit catalog
review; stale fingerprints must fail instead of silently replaying different code.

R1.8 post-rebase catalog raw SHA-256:
`3359ac475a10cc6d5a84509282b090be4bbea9f8c5aaed0f86283e7cc276199a`;
canonical JSON SHA-256 used by the driver:
`52d6cb4d06cb573f0a2dede1c6d4bc18d110499f597fbc7e822fc16980c04b9c`.
These catalog hashes remain current after final verification. Four affected
source-target probes (`M11-parser-newline`, `M11-repair-newline`,
`M11-shared-validator-newline`, `R1-warning`) each passed control GREEN → named
behavioral RED → exact restoration → GREEN; live source was unchanged. This
limited post-rebase check was followed by the complete final replay described below.

### Final measured inputs and metadata continuation

The final measured source is base `9243fd67c1d4ed3beba2bfbab12d8ed85ee22f4e`, HEAD `f00d1b925a42f1990ad3f77b0fa349ab5ceb37cd` **plus reviewed dirty files**. It is not attributed to HEAD alone. The three intentional new inputs are the committed-path driver, catalog and safety test.

| Final input / evidence | SHA-256 or result |
| --- | --- |
| All 1,207 measured input path/hash entries, canonical sorted JSON | `1d4a19f5983de54234f5f109bc5c4d04636ba53fff3bf1ae85418f794ea85192` |
| Driver raw bytes | `57328ab78d792a0a821d81e551510ed62cf67ecb649c84953fdad87caf72ba1b` |
| Safety test raw bytes | `7df8ca29fbab7164b10867bcac5c042b8866b59e72b2f3738f4cb85549305a51` |
| Catalog raw bytes | `3359ac475a10cc6d5a84509282b090be4bbea9f8c5aaed0f86283e7cc276199a` |
| Catalog canonical sorted JSON | `52d6cb4d06cb573f0a2dede1c6d4bc18d110499f597fbc7e822fc16980c04b9c` |
| Complete live replay | 113/113 control GREEN → named behavioral RED → exact restoration → GREEN; source unchanged for every probe |
| Full-suite input preservation | All 1,207 hashes unchanged after replay and again after pytest |
| Stable projection excluding exactly `216-plan.md`, `216-todo.md`, `216-verification.md`, `216-coordination.md` under `docs/plans/` | 1,203 entries; `3dc621081dd795d1232689dc6b2e527037d41ac7a5b8156d8787ed784122616a` |

The projection hashes `json.dumps(path_to_sha256, sort_keys=True).encode()` with SHA-256, using the same measured input set with only those four metadata paths removed. This final evidence update changes only those four documents; all executable, test, catalog, prompt, schema and other input bytes remain exactly the passing version. The full 1,207-file identity labels the pre-evidence measured tree; it is not falsely reused as the identity of rewritten metadata. The final evidence delta passed independent Standards/Spec reviews (0/0). Subsequent publication/CI/cleanup proof is recorded below; later metadata changes preserve the same 1,203-file projection.

## M01–M15 coverage map

The table names representative committed public killers; the catalog is authoritative
for every variant and parameter selection. Test names below are selectable pytest
node prefixes; parameterized cases expand normally. Prompt semantic kills must
fail meaning assertions, rather than only checksum or budget guards.

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
| P3 №11 — application test factories | Five factories live in `tests/policy_snapshot_fakes.py`; all 13 currently audited consumers migrated. Eval uses public production dataclasses/helpers and never imports tests. R1.4 reuses public `policy_documents.missing_document` with byte/value parity. R1.8 recount confirms 13 consumer files. |
| P3 №16 — private ID regex | Public strict ID validator shared by parser/model and repair; M11 independently covers all public boundaries, M13 covers schema lexical parity including trailing newline. |
| P3 №17 — missing Reflection | Missing component raises existing `RunFailure("internal_error")` before draft/publication. This deliberately changes old terminal policy failure to **retryable server failure: three total attempts, then failed/dead-letter to `reviews.dlq`**. Actual unready/corrupt snapshot remains integrity failure. |
| P3 №19 — verifier prompt disk read | Deliberately retain version-selected package loading and raw-checksum verification. A pinned-content resolver/cache belongs to remaining #130 composition work; adding a second resolver here expands scope. Residual runtime package I/O is explicit. |
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
runtime prompt. Legacy missing pins stay absent/incomplete. Both worker
compositions still omit Reflection; no v4 activation is claimed by #216.

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

## Reproduce from committed inputs

Prerequisites: Git checkout, uv, Python 3.13, project dependencies and a supported
Linux toolchain (including Bash, jq and Git for workflow tests). Use dedicated
PostgreSQL and RabbitMQ services for the full suite, with an isolated task vhost.
The placeholders below are caller-provided inputs; they do not name pre-existing
personal resources. Start/provision those services and verify readiness first.

```sh
uv sync --locked --all-groups
uv run python review/scripts/replay_reflection_mutations.py
uv run python review/scripts/replay_reflection_mutations.py --execute \
  --output '<scratch-dir>/reflection-replay'
```

Choose a fresh output directory **outside** the checkout; existing output is refused.
`--ids R1-step3 R1-rules-allowlist R1-agents-allowlist R1-warning` selects a bounded
subset. For intentional untracked replay inputs, pass their relative paths via
`--extra`; the published committed catalog/test inputs need no supplemental files.
The driver copies Git-listed dirty bytes, records HEAD plus input hashes, excludes
secrets/caches/Git and escaping symlinks, creates its own environment and verifies
imports come from the copy. It strips provider/service credentials and blocks test
network access. Dependency synchronization may need package-registry access.

Each live probe runs control GREEN → one exact edit → named behavioral RED → exact
restoration → GREEN. Structured pytest reports reject collection/setup/import,
timeout and checksum-only false kills. Public exceptions explicitly accepted by a
probe remain valid behavioral failures. Child processes are terminated and awaited
before restoration. `identity.json`, `results.json`, per-probe patch/XML/log files
are written inside the selected output directory. Results are never resumed.
SIGKILL may leave only the disposable copy; the source checkout is never mutated.

After the complete replay, with source quiescent, run the canonical full gates:

```sh
export TEST_DATABASE_URL='<disposable-db-url>'
export TEST_RABBITMQ_URL='<isolated-task-vhost-url>'
export RABBITMQ_URL="$TEST_RABBITMQ_URL"
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Record final base/head plus dirty/input identity, counts, warnings and zero unexplained
failures/skips; compare executable input hashes before/after. A runtime-changing
rebase invalidates older gate proof. The recorded final tree has completed the full
113-live replay and fresh whole-PR 0/0 reviews. Reviewed final evidence metadata and green CI on the published code/evidence head are recorded below; later changed source requires
fresh proof rather than reuse of these counts. Run replay before the full suite to avoid test timing
contention. Remove only resources created for this verification; keep shared images
and other tasks' infrastructure. No merge, issue closure or reviewer-thread resolution
is authorized by these checks.

## Verified publication and cleanup

The reviewed code/evidence was published as `77d544edc8636284d6131cd7a1baf0884dc7b8bb` over base `9243fd67c1d4ed3beba2bfbab12d8ed85ee22f4e`. All 1,207 pre-commit input bytes were preserved across six focused review-fix commits. The final evidence delta passed fresh independent Standards/Spec reviews with zero findings.

[CI run 40219321992](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/40219321992) completed **successfully on that exact published head**. Unit tests: **5,620 passed**; integration shards: **268 + 258 + 330 + 184 passed**; total **6,660 passed, zero test skips**. CI reported eight unit warnings and two warnings per integration shard; these are separate from the two warnings in the local full-suite measurement. Four conditional image-publish/deployment jobs were skipped for the PR; those are job conditions, not skipped tests. All executed lint/type/configuration/security/build/smoke jobs succeeded.

The PR body and four SHA-bearing P2 replies were read back exactly. One summary covers all eleven P3 items and the #130 F1/#304 merge-order coordination. The exact repository issue remains OPEN and its Board 12 item On Review. PR #303 remains non-draft with no new requested reviewer and no closing issue links. The four reviewer-owned threads remain unresolved for their author; no merge, issue closure or approval was performed. Receipt links are in [coordination](216-coordination.md).

After that successful CI, ownership labels and network endpoints were verified before removing exactly three owned containers, four volumes and one network. The owned-resource inventory is empty; evidence was archived, and the shared image, managed worktree and backup refs/stashes were preserved.

This publication-metadata continuation changes only the four #216 documents. The 1,203-entry projection excluding precisely those documents remains `3dc621081dd795d1232689dc6b2e527037d41ac7a5b8156d8787ed784122616a`. The code/evidence SHA above is a verified historical checkpoint; later documentation commits do not claim that a commit can contain its own SHA. Current PR checks are available from [PR #303](https://github.com/larchanka-training/dmc-268-api-t6/pull/303/checks).
