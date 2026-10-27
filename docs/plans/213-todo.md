# #213 — Review round 5 checklist

Plan: [213-plan.md](213-plan.md). Source requirements:
[review 5659553559](https://github.com/larchanka-training/dmc-268-api-t6/pull/283#pullrequestreview-5659553559)
and the snapshot-test docstring follow-up. Checked items record completed
implementation, verification and publisher preparation. Publication itself follows
the external release protocol below.

## R5-1. Integrate actual main

- [x] Check actual main and open-PR ownership; coordinate overlapping files before
      editing and rebase the feature branch.
- [x] Preserve main's F2 activation, policy/verification, monitoring, provider
      authority and prompt/attempt behavior together with #213 coverage contracts.
- [x] Verify one Alembic head and immutable merged revisions; adjust only the own
      unmerged migration and its expectations if actual main requires it.
- [x] Pass focused migration, settings/pins, provider and coverage checks after
      resolving semantic conflicts.

## R5-2. Protect both render labels

- [x] Assert the whole `Ещё причин: {n}` line and its correct remaining count in
      `test_clean_without_text_reasons.py`.
- [x] Assert the exact `Текстовые изменения не проверены` summary for no coverage.
- [x] Demonstrate failures for independent label mutations and an off-by-one
      count mutation, restore production bytes and pass focused tests.

## R5-3. Complete and synchronize specifications

- [x] Retain all existing general/specialized reason rows and add the missing
      lowercase partial label, exact `done_well` template, checked-file count,
      confirmed-finding count and `**Coverage:**` label to the canonical table.
- [x] Keep the canonical block identical across all five specifications; expected
      test strings come from those specification literals.
- [x] State in CVS §2.2 and mirrors/TEST_PLAN that GitLab lacks independent
      ordinary-file proof here, so empty additions remain `missing_patch`/partial.
- [x] Replace “На ветке #213” with “После #213” while preserving current F2 attempt
      semantics and one cache-miss ordering block.
- [x] Check test-reference validity and current provider/migration wording.

## R5-4. Correct the legacy-path docstring

- [x] Describe a run bound to v3 or lower, created before the F2 seed or with
      pre-0039 pins, in `test_snapshot_path_live_pin.py`.
- [x] Preserve all test assertions and current legacy/snapshot-path behavior.

## R5-5. Supply active user-documentation tracking

- [x] Check UI ownership and read `how-it-works.mdx` in full before editing.
- [x] Correct the `clean` row and explanation for complete, proven no-text,
      partial-risk, partial-without-risk and unavailable-coverage cases; include
      the provider-specific GitLab limitation.
- [x] Pass the UI repository's required gates and docs build for the page change.
- [x] Publish a docs-only UI PR referencing API #213/#283 and request the explicit
      core-page full-review phrase required by CONTRIBUTING §8.
- [x] Prepare the API PR body with active UI198 tracking in place of UI189;
      keep the required human core-page approval explicit.

[UI PR #198](https://github.com/larchanka-training/dmc-268-ui-t6/pull/198)
contains the page correction; explicit human core-page review remains required.

## R5-6. Verified final candidate

- [x] Freeze 1,364 regular files and two symlinks; postproof hashes, sizes, modes,
      targets and file set match the tested candidate before this factual edit.
- [x] Pass `uv run ruff check .`, `uv run ruff format --check .` (742 files) and
      `uv run mypy .` (732 sources).
- [x] Pass the current full `uv run pytest`: 9,402 passed, zero failures/errors/
      skips/duplicate nodes, two existing dependency warnings, with PostgreSQL
      and RabbitMQ cases; preserve assertions, deadlines and thresholds.
- [x] Retain the valid 7,686-case unit proof on unchanged inputs and 100% core
      line/branch coverage: `review_output` 392/132 and `llm.answers` 183/28.
      No new dedicated unit command after the integration-fixture fix is claimed.
- [x] Account for 501 targets (277 integration, 224 non-integration) as 496 retained
      passes plus five fresh affected PostgreSQL passes. All target and unit nodes
      pass once in the current full run, which has 1,716 extra cases versus unit.
- [x] Pass PostgreSQL 0043 → 0042 → 0043 → 0039 → 0043 catalog checks and remove
      the probe schema; preserve merged migrations and downgrade guards.
- [x] Kill both label mutations and the off-by-one count mutation; restore bytes,
      pass focused tests and retain identical five-spec blocks/test references.
- [x] Pass corpus 24/24, distribution and rules 8/8. Recorded replay is unchanged
      from pristine main: 10/24 valid and two existing baseline warnings; no fresh
      complete quality baseline or live/paid capture is claimed.
- [x] Pass controlled actual-HTTP gateway checks: two POST 200, 71/400 patch lines,
      8,907 bytes, absent tail and retained loss/output/usage/call/attempt metadata.
      Imported hashes match; this does not claim live provider or publication proof.
- [x] Obtain zero-finding whole-feature Standards/Spec reviews and zero-finding
      final delta reviews. The F2 fixture correction retains all 37 assertions;
      original failures remain diagnostic evidence, not successful gates.
- [x] Record actual scoped results without environment traces; only these two
      plans change after verification, leaving 1,362 other files unchanged.

## R5-7. Completed publisher handoff

- [x] Prepare What / Why / How to verify / Refs for the whole final feature with
      UI198 tracking, four reproducible reviewer commands and actual scoped proof.
- [x] Define intended-path staging, normal hooks, one #213 feature commit and an
      exact lease, with actual main and remote-head checks before publication.
- [x] Prepare the new P2 thread 4382883915 reply and all P3/docstring dispositions;
      require the fix SHA and exact-head green CI before posting them.
- [x] Define renewed review of the existing Ready PR, preserving open #213, closed
      old threads and reviewer-owned resolution; no merge or Development link.

The external release protocol is to recheck main, publish the reviewed feature,
verify successful CI on its exact head, update the prepared API body, post the
open-thread reply and dispositions, and request the established reviewer again.
Execution receipts are recorded in the PR after those actions succeed. This
handoff does not claim those later actions or human UI page approval executed.

## Historical context

The previous published head `9de9f17d` passed 9,229 tests and CI. That historical
result is separate from the verified 9,402-case round-5 candidate above.
