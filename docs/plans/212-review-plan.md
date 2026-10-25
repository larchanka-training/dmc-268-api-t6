# PR #280 review round 1 — implementation plan

Address review on `5503114`, then rebase on current `main` (`05a96bc` when planned).
The user authorized proceeding without plan confirmation. No API, queue, digest,
cache-key, snapshot-version, or migration-ID change is required.

## Comment disposition

| Review comment | Action and acceptance evidence |
| --- | --- |
| P2 thread `4370445662`, follow-up `4370452472` | Fix only `review_prompt_repository.py`: choose Run threshold, NULL fallback to current Repository threshold. Real PostgreSQL regression proves pinned `warning` survives Repository → `critical`; historical NULL reads current nondefault value. Preserve missing-run/missing-settings behavior. Leave `execute_review.py` and `store_review_output.py` unchanged. |
| P2 thread `4370445672` | Add `20261024_0038` first in CICD downgrade-loss registry and examples, BACKEND_ARCHITECTURE revision inventory, SD downgrade inventory and Run/snapshot schema descriptions. State both constraints and all four columns are lost, enum remains, re-upgrade returns NULL pairs and historical live fallback. ERD addition is optional; omit because its existing Run list is explicitly partial. |
| P2 thread `4370445678` | Update RFS §7.2's current state and TEST_PLAN retry/rerun row. Distinguish implemented threshold/cap pins from future prompt/profile pinning; cite existing real-DB regressions. |
| P3-1 | Extract one exhaustive infrastructure enum→literal converter, used by snapshot, prompt and postprocessing repositories; guard NULL at callers, no casts. Standardize enum construction in webhook/rerun persistence. Type parametrized threshold as `SeverityThresholdValue`; remove the avoidable ignore. |
| P3-2 | Add independent literal digest and conventions policy-key goldens obtained from unchanged fixture input at baseline `1ae74b1`, with source commit noted. Check historical and changed pins against the same literals; retain existing formula oracle. Do not generate expected values from changed implementation during tests. |
| P3-3 | Remove redundant field assertion immediately following snapshot identity assertion. |
| P3-4 | Add unit rows for pinned pair different from Repository pair and historical NULL pair whose live values differ from defaults. Assert exact threshold/cap and retain allowed-v2 guard coverage. |
| P3-5 | Clarify RFS introductory paragraph and SD Р-6: threshold/cap pinning is implemented for new legacy runs; historical NULL pairs use live fallback. |
| P3-6 | Remove temporary-log paths, local DSN/port and agent-coordination narrative from existing 212 plan/todo. Keep durable commands, measured results and useful ownership facts. New review documents contain no machine-specific evidence links. |
| P3-7 | Rebase keeps main's worker polling implementation with its explanatory broker comment. Avoid a duplicate production/test change. Include `Refs #245` in follow-up commit footer (alongside #212); no history rewrite solely for the footer. |
| P3-8 / stack FYI | Current main has no 0038: retain ID `20261024_0038` and parent `20261020_0037`, preserving #289's dependency. Refresh migration graph immediately before publication; if main advances migration head, coordinate rather than silently invalidating stack. |
| Existing stack coordination comment `6387289216` | Publisher posts a public one-line warning in PR #280 BEFORE force-push, then uses force-with-lease after rebase. Reply with final SHA to all three P2 threads and summarize P3 dispositions; request review from API-verified author `axyi`. Never resolve review threads. |
| Existing coordination comments (#130 E, #216, #211) | Preserve those boundaries: no edits to execute/store paths, no removal of factory helpers unrelated to this review, preserve accepted provenance/prompt/reflection wording while editing only pin state. |

## Implementation boundaries

Use a small infrastructure helper such as `reviews/infrastructure/severity_threshold.py`
with the current exhaustive `match` mapping. No infrastructure type moves into the
application layer. Optional values remain explicit at the snapshot boundary. A query
`coalesce(Run.severity_threshold, Repository.severity_threshold)` or the equivalent
row selection solves the prompt path without touching ReflectionPass orchestration.
Reuse existing `tests/test_review_prompt_pins_postgres.py` for the new DB cases if
its fixtures fit; otherwise use the existing publication-settings PostgreSQL suite.

Rebase file intersections since baseline: `run_repository.py`, PS, CVS,
`test_run_origin_postgres.py`, `test_sqlalchemy_run_repository.py`, and
`test_worker_shutdown.py`. Preserve main's merged DiffMap behavior and tests while
retaining publication settings. Keep main's broker explanation. The two changes
share the same polling algorithm. CI now uses xdist for nonintegration and four
pytest-split integration shards; do not restore old workflow/dependency versions.

## Ownership coordination before editing

Exclude PR #280 itself. Exact overlaps against the fetched open-PR file lists:

| PR | Files this review may touch |
| --- | --- |
| #291 | RFS, SD, TEST_PLAN |
| #290 | run_repository.py (shared converter extraction only) |
| #289 | policy_snapshot_unit_of_work.py, rerun_store.py, webhook_runs.py |
| #283 | run_repository.py, RFS, SD, TEST_PLAN, test_sqlalchemy_run_repository.py |
| #279 | CICD |
| #277 | RFS, SD, TEST_PLAN |
| #254 | BACKEND_ARCHITECTURE, CICD, SD, test_review_context_stage.py |
| #227 | run_repository.py, BACKEND_ARCHITECTURE, SD, TEST_PLAN, test_sqlalchemy_run_repository.py |
| #220 | rerun_store.py, run_repository.py, webhook_runs.py, SD |

The prompt repository and its current DB test file have no listed owners. Post
coordination comments for the listed overlaps before implementation; planning
writes only the two new review documents. Runtime source paths above are relative
to `app/modules/reviews/infrastructure/`; test names are under `tests/`, specs under
`docs/`. No GitHub posting or rebase is performed by the planning agent.

## Verification and publication

Follow the task list. Start red with prompt-repository DB and nondefault unit cases,
then implement converter and query, align docs, and rebase with semantic checks.
Run Ruff lint/format, mypy and full pytest after rebase. Run focused migrated
PostgreSQL suites and relevant worker checks with services; report skipped tests
accurately. CI's zero-skip integration and complete test-collection aggregate must
pass on the pushed head. Review standards and spec independently, fix findings,
then publish replies with actual commit IDs and request review. Refresh PR body to
include reflection-source fix, complete migration documentation, baseline goldens,
review verification and preserved stack/migration compatibility. Keep issue open.

## Risks and open questions

Main's DiffMap SQL changes can conflict with fake-row expectations: merge query
columns by meaning, not one side wholesale. Historical NULL behavior must not be
accidentally converted to default or snapshot pinning. Baseline goldens must be
computed from the same unchanged input fixture. No blocking question remains.
