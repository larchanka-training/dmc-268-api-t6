# #207 implementation checklist

**Gate: human approved the plan and external coordination on 2026-10-23.**
See [207-plan.md](207-plan.md) for the selected design and counts.

## Task 1: Approve and coordinate

**Description:** Confirm the concrete policy design and prepare safe file ownership.

**Acceptance criteria:**

- [x] Human approves authoritative same-commit tree/blob policy (+1 to +3 requests).
- [x] Recheck main/open PRs; with direct user authorization, comment on every still-open
      overlapping PR (#228, #229, #230, #227, #220), or wait for merge and rebase.
- [x] Dedicated PostgreSQL baseline runs without skips once infrastructure is available.

**Verification:** Baseline no-PG result is 245 passed, 0 skipped in 12.98s at
`66077f292bcd829d1807c520f48b48fd053891f2`; Dedicated PG baseline completed: 41 passed, 0 skipped in 46.86s.
Human approval and all five PR coordination comments are recorded by the coordinator.

**Dependencies:** None. **Files likely touched:** planning artifacts only. **Scope:** S.

## Task 2: Prove hidden-policy and HTTP contracts fail first

**Description:** Separate pinned policy objects from archive contents in test fakes
and add regressions for exported-away policy.

**Acceptance criteria:**

- [x] Hidden rules / hidden parent directory exclude the policy's target; truly missing
      rules remain empty without error, at the exact archive commit.
- [x] Missing versus unreadable tree/blob, invalid document types/bytes/limits, and
      policy failure without publication have literal expected outcomes.
- [x] HTTP counters cover present policy (+3), missing parent (+1), missing document
      (+2), unchanged (2), and attempted requests on error; no AGENTS read occurs.

**Verification:** Run the new focused tests against old code and record meaningful
failures; preserve existing passing baseline. Confirm network tracker sees no DB
transaction during policy reads.

**Dependencies:** Task 1. **Files likely touched:** `tests/test_build_repository_index.py`,
`tests/test_github_snapshot_source.py`. **Scope:** S.

## Task 3: Read authoritative policy and invalidate old indices

**Description:** Extend the provider-neutral port, implement counted Git tree/blob
reads with the review loader semantics, and wire the single policy path into builds.

**Acceptance criteria:**

- [x] `rules_document(commit_sha)` returns normalized policy with reliable absence;
      every tree/blob request is counted and deadline bounded outside transactions.
- [x] Full and incremental parse that policy; errors release with `policy_*`, preserve
      previous publication, and never fall back to empty masks.
- [x] `index-exclusions/v2` changes the existing limits profile and forces full even
      when an old unsafe publication has the same commit; UNCHANGED stays two requests.

**Verification:** Task 2 regressions turn green; existing no-PG baseline and
`test_code_index_layers.py`, review policy and exclusion tests pass. Audit every fake
implementing the protocol; adjust method-call versus real-HTTP count models explicitly.

**Dependencies:** Task 2. **Files likely touched:** `application/vcs.py`,
`application/build_index.py`, `application/exclusions.py`,
`infrastructure/github_snapshot_source.py` under `app/modules/code_index/`; tests from
Task 2. If this exceeds five files in one working slice, land protocol/adapter and
use-case wiring as consecutive small changes without requesting review mid-contract.
**Scope:** M per slice. No migration or dependency.

## Checkpoint: policy boundary

- [x] Focused regression and adapter suites green; failed reads cannot publish.
- [x] Literal request table agrees with successful and failed request telemetry.
- [x] Version bump blocks unsafe old-index UNCHANGED path.

## Task 4: Prove full/incremental equality in PostgreSQL

**Description:** Extend real-store scenarios with policy bytes absent from archives.

**Acceptance criteria:**

- [x] An unchanged hidden policy across two commits leaves the first target build
      genuinely incremental and equal to a fresh full at that same target commit.
- [x] Compare normalized files/symbols/edges/imports; assert ignored files and their
      storage counts are absent, with changed-policy full fallback separately covered.
- [x] No-document and unreadable-document scenarios preserve specified behavior;
      indexer integration tests run against a dedicated database with zero skips.

**Verification:** `TEST_DATABASE_URL=<dedicated-db> uv run pytest -m integration -rs
 tests/test_code_index_indexer_postgres.py`; record exact results. Run relevant store
and queue integration suites without sharing the DB with other runs.

**Dependencies:** Task 3. **Files likely touched:**
`tests/test_code_index_indexer_postgres.py` and relevant shared test fake only if needed.
**Scope:** S.

## Task 5: Publish canonical semantics in repository docs

**Description:** Establish stable SD `index-row-contract` anchor and link consumers.

**Acceptance criteria:**

- [x] SD §7.4 and CVS §3.5 specify archive limits, Python/TS export names and
      `facts.exports` join, Go directory/member semantics, module-only edge cases,
      unresolved import statuses and absent speculative name edges.
- [x] RFS §5 and ADR §9 replace archive-as-absence-proof claims with complete pinned
      Git-tree evidence and safe release on uncertainty; ADR §3 links row canon.
- [x] SD/PIPELINE counts agree with 5/6/7 full, 6/7/8 incremental and 2 unchanged;
      PR cost/version rationale is documented.

**Verification:** Review docs against resolver/symbol-row code and PR #176; check links,
including the future main anchor after merge. The publisher-owned #144 handoff is
a publication step described below; repository documentation implementation is complete.

**Dependencies:** Tasks 1, 3, 4. **Files likely touched:** `docs/SYSTEM_DESIGN.md`,
`docs/CONTEXT_AND_VERIFICATION_SPEC.md`, `docs/PIPELINE_SPEC.md`,
`docs/RULES_FORMAT_SPEC.md`, `docs/adr/0001-graph-rag-v1.md`. **Scope:** M.

## Checkpoint: ready for review

- [x] All implementation/documentation AC and the RFS/ADR correction are accounted for.
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [x] `uv run pytest` — 5136 passed, 0 skipped, 2 existing dependency warnings in 1037.04s.
- [x] Dedicated indexer PostgreSQL proof: 51 passed, 0 skipped in 59.51s.
- [x] Complete code-index integration: 163 passed, 0 skipped, 298 deselected in 158.78s.
- [x] Rebased on `origin/main` at `5d183fe`; ownership comments and final diff reviewed.
- [x] Independent full-feature review reports Standards 0 / Spec 0, including queue fix.

## Publication step

The publisher creates the Conventional Commit and PR with What/Why/How to verify/Refs,
explaining request cost and version invalidation without a closing keyword or new dependency.
After the PR URL exists, the coordinator/publisher posts the authorized #144 handoff to
`docs/SYSTEM_DESIGN.md#index-row-contract`. At the development-to-publisher handoff,
that external comment has not been posted yet; no follow-up repository patch solely
to insert its URL is required.

## External pre-merge requirements

A human approving review on the current head and resolution of every review thread remain
required before merge. The branch must be current with main at that time. The canonical
main anchor becomes available after merge; the tech lead checks AC and closes #207.
Development has made no commits or pushes and does not claim these external events occurred.


## Historical first-slice verification record (2026-10-23)

RED: 25 missing-port adapter failures; 8 meaningful hidden-policy/build/version failures;
6 malformed-tree failures. GREEN: 381 passed, 0 skipped in 11.44s for Tasks 2–3 focused
and shared-policy regression suites. Ruff lint, Ruff format check, mypy (518 files), and
`git diff --check` pass. Task 4/5 and final complete-suite gates await boundary review.
See the plan's implementation evidence for commands and scope. No commits or pushes.

First review fixes: independent literal policy digest; bounded shared `Retry-After`
classification. RED 7 failed/3 passed; GREEN 270 passed in 8.69s; Ruff, format, mypy green.
Await fresh boundary review before Task 4/5.

Tasks 4/5 now implemented on base `5d183fe` after clear policy-boundary review.
Dedicated indexer 51 passed / 0 skipped; new scenarios separate original policy objects
from export-filtered source archives. Full integration/full-suite gates are pending
completion; coordinator owns #144 external handoff. Human current-head approval and
resolved review threads remain required before merge. No commits/pushes by development.

Final full-feature review: Standards 0 / Spec 0, including queue fake correction.
The missing queue fake tree route produced RED; focused GREEN 1 passed in 4.98s;
complete code-index integration GREEN 163 passed / 0 skipped in 158.78s.
Final full pytest with both dedicated services is running; XML evidence is in /tmp.


## Final validation (2026-10-23)

All Tasks 1–5 implementation and four mandatory gates are complete. Ruff lint and
format pass (529 files); mypy passes (526 source files). Full suite on the dedicated
PostgreSQL and RabbitMQ services: **5136 passed, 0 skipped, 2 existing dependency
warnings in 1037.04s**. JUnit `/tmp/issue207-final-pytest.xml` confirms all 461
code-index cases passed without skips or errors. Separate code-index integration:
163 passed / 0 skipped; indexer: 51 passed / 0 skipped. Independent final review:
Standards 0 / Spec 0. No commits/pushes by development. Publisher handoff is ready;
external publication and pre-merge steps remain prose requirements above.
