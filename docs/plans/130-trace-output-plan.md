# Implementation plan: #168 part 6 "trace and output" in #130 F (PR F3)

## Status and baseline

- Planned 2026-10-27 on `origin/main` `f40cabf3`. F1 #328, F4 #329, F2 #332, #304 (#247 A2), #220
  (GitLab), C #324 and T3 #301 are merged. v4 is the active review prompt, so new runs take the
  snapshot path unless a repository overrides the prompt.
- Issue: #168, AC line "Ч.6, трассировка и вывод" with its dashboard sub-bullet (#246). Tech-lead
  decision 6396093271 puts these items into #130 F, so this is F3 of
  [130-snapshot-path-plan.md](130-snapshot-path-plan.md). Task list:
  [130-trace-output-todo.md](130-trace-output-todo.md).
- Worktree: `dmc-268-api-t6-130f3`. Branch: `fix/168-trace-and-output`. Test DB and vhost: `imp130f3`.
- Commit format, per the coordinator's brief: `fix(reviews): … (#168)` with `Refs #130` in the body.
  This replaces the parent plan's `(#130)` subjects and its branch name `fix/168-part6-trace`, because
  all six items are #168 AC items.
- `file:line` anchors are read at `f40cabf3`. Open-PR hunks are read at their bases: #283 and #330 are
  on `88bbc073`, 12 commits behind.

## Scope: the part-6 items that are F3's

The #168 AC splits part 6 into four lines.

- **F3 (this plan):** "трассировка и вывод", with the dashboard captions (#246).
- **Not F3:**
  - "загрузка политики" is items 1-5 of 6397547271 and 6428531989. PR 2 = #338 (item 5, staging per
    document), "PR 1" (items 1-3: the 64 KiB stream, `actual`, the classifier) and "T6" (item 4: ignore
    source lines and the guard message) belong to another session.
  - Parser isolation is #288, already ticked.
  - The commit-message filtering decision is already ticked.

## Item table

| # | Item | Status at `f40cabf3` | Change (one line) | Tests (red first) | Files | Open PR overlap |
|---|---|---|---|---|---|---|
| 1 | `context.build` once per attempt | **open**. Two records: the stage's (`review_context_stage.py:606-626`, before conventions) and the prompt read's (`run_trace.py:136-179`, wired at `worker.py:294-295`, after `llm.repo_conventions`). The prompt read's `budget.used` is chars//4 (`run_trace.py:166-173`), not the pinned counter | delete `TracedReviewPromptRepository`; the stage writes `context_payloads` from its own summary (D1) | one build per attempt on both paths and per provider composition; payload row = the build's response; no row on a skip; a store error fails the attempt; a retired origin is fenced at the write | `run_trace.py`, `worker.py`, `review_context_stage.py`, `gitlab_reviews.py`, `run_lifecycle_store.py:762` (Q2), PS, MONITORING, `overview.json` | #283 (stage `run`, PS, live pin), #330 (PS, e2e), #340 (Bitbucket stage) |
| 2 | ignored files out of `level_used` / `budget.used` | **partly**. Since T3 #301 a withheld file has no `review_patch` (`get_run_diff.py:74-85`), so it is never in `files` or `budget.used` (`:160-165`). But its name is in the prompt read's `omitted_files` (`run_trace.py:164`), so in the trace and in `context_payloads` (the #165 note in 6239860585). The stage counts ignored files only as a number (`review_context.py:462-477`; pinned by `test_review_context_stage.py:253-287`) | closed by item 1: the record that names them is removed | the payload row and the one trace record contain no ignored path, and `ignored_files` counts it | as item 1 | as item 1 |
| 3 | `diagnostic_path` escaped (CVS §9.1 `:1289`, RFS `:1033-1035`) | **open**. The raw provider name (`review_context.py:461`) goes into the trace (`review_context_stage.py:196`): no escaping, no size bound | a reversible escape plus a size bound, in the trace serializer only (D2) | control, format and line-separator characters escaped; backslash doubled; ordinary Unicode kept; bound with a cut on an escape boundary; canonical `path` untouched | `diff_map.py` (helper), `review_context_stage.py:196`, PS `:172` | `diff_map.py`: #283 `:178-238`, #340 `:29-43`; the helper at `:171` avoids both. No hunk of either PR in `context_summary` |
| 4 | excluded files as one shared counter (RFS §5.3) | **done on the snapshot path** (F1-4): `execute_review.py:265-275` and `prompt_builder.py:277-278`, pinned by `test_review_context_blocks.py:415`. **Legacy:** names are filtered from both lists (`execute_review.py:250-254`) and there is no counter, by design (`test_legacy_review_gets_neither_excerpts_nor_the_counter`, `:463`). On the snapshot path a caught build failure gives no counter either (`:454`) | none (Q3) | none | none | none |
| 5 | typo «маска длинна» | **open**, at `check_runs.py:55`, not `:52`. The same text also reaches the rules-status API through `failure_reason` (`rules_status.py:111`) until #342 replaces it with English texts | «…или слишком длинная маска.» | the exact text, and no `_ERROR_TEXTS` value contains «длинна» | `check_runs.py:55` | #283 `check_runs.py` hunks at `:120-180`: no overlap |
| 6 | `_LazyPolicySource._locators` | **open, dead**. It is written at `worker.py:696` and never read; the dict at `:644` is never filled | delete `:644`, `:658`, the `:691` parameter and `:696` | none: a refactor; ruff, mypy and the factory tests guard it | `worker.py` | #340 `worker.py` hunks miss `:600-713` |

## Design

### D1. One `context.build` per attempt (items 1 and 2)

**Which record survives.** The stage's record, before conventions (TL-9). TL-9 supersedes the #165
decision 6239860585 ("keep the prompt-read row"), as the G plan records in its §7.

**What it carries.**
- The stage's `context_summary` response stays unchanged.
- The prompt read's keys disappear. Their counterparts in the stage summary:
  - `files[].level_used` → `blocks[].level`;
  - `budget.used` → `tokens_used`, counted with the pinned counter on the snapshot path (R19) and the
    4-char estimate on legacy;
  - `omitted_files` → `omissions`: reasons with canonical paths, ignored files only as a count;
  - the request `{engine, token_limit}` → `{head_sha, policy_snapshot_id}`.

**What the legacy (≤v3) path records.**
- The same single stage record, whose numbers are unchanged. It counts with the composition's
  `EstimatedTokenCounter` (`worker.py:628`, `gitlab_reviews.py:146`); the counter override at
  `review_context_stage.py:610-615` applies to the snapshot path only.
- The legacy prompt is unchanged. Only its trace loses the second record.

**`context_payloads`.**
- Today its only writer is the prompt read (`run_trace.py:177`). It moves to the stage.
- New optional keyword `payloads: ContextPayloadStore | None = None` on `ReviewContextStage`. The
  protocol stays in `run_trace.py:128`. With the default, the ten test constructions stay unchanged.
- **Placement.** After the build, outside the `try` at `:607-633`: after the
  `ALL_CHANGES_IGNORED` raise (`:636-637`) and before `self._payload = payload` (`:638`). The write
  stores `step.response`.
  - Outside the `try`, a DB error fails the attempt. Inside, it would be swallowed as an optional build
    failure (`:627-633`).
  - #283 rewrites that `try`. Placing the write below it keeps the hunks apart.
- **Coverage of attempts.**
  - Every attempt that built a payload gets the row, including summary-only.
  - A skip gets no row (`_record_nothing_to_review` and the build's `all_changes_ignored`), as today.
  - A caught build failure gets no row (Q2).
- **Schema version (Q2(a), refined in T1).** The upsert sets `schema_version=2` in its update
  clause too (`run_lifecycle_store.py:762-766`): `set_` held only `summary`, so a retry over a
  schema-1 row would keep 1 with the new shape. Pinned by the `existing` case of the re-targeted
  `test_run_context_origin_postgres.py` test.
- **Fence.** `save_context_payload` locks the run origin and raises `RunCancelled`
  (`run_lifecycle_store.py:749-759`). That now happens before conventions.
  - Between conventions and the review call, the production fence is `AttemptCheckpoint`
    (`handle_review_run.py:191-197`), which `CheckpointedProvider.draft_review` runs (`:244-248`).
  - The test that relied on the prompt-read fence is re-targeted (see "Tests that flip").
- **Wiring.** The stage-building functions pass the attempt's trace as `payloads`:
  - `review_context_stage_factory.stage` (`worker.py:661-677`);
  - the GitLab bundle (`gitlab_reviews.py:156-170`);
  - #340's Bitbucket bundle (`bitbucket_reviews.py` ~`:295`, not on main).
- **Types.**
  - A protocol `ContextTrace(RunTrace, ContextPayloadStore, Protocol)` in `run_trace.py`.
    `TransactionalRunTrace` already satisfies it (its docstring at `:58` says so).
  - Callable parameters are contravariant, so every annotation on the path narrows from
    `RunTrace | None` to `ContextTrace | None`:
    - `GitHubAdapters.context_stage` (`worker.py:573`);
    - the factory's return type (`:610`) and its inner `stage`;
    - `gitlab_reviews.py:103`;
    - the test wrapper `test_run_policy_context_origin_postgres.py:547-557`.
  - The other call sites pass `None` or a `TransactionalRunTrace` (`:234`; `test_rag_review_context_postgres.py:225`;
    `test_snapshot_path_e2e_postgres.py:325` through `compose_worker_process`).
  - The protocol, not the concrete class, keeps fakes with both methods usable.
- **Removed orphans** (all created by this removal):
  - `TracedReviewPromptRepository` and `FAST_TOKEN_LIMIT` (`run_trace.py:132-179`);
  - their imports `GeneratedConventions`, `ReviewPromptInput`, `ReviewPromptRepository`
    (`run_trace.py:14-18`);
  - the import at `worker.py:144` and the wrap at `:294-295`;
  - found in T1: `process_review_run`'s `engine` parameter, which only fed the wrap, with its two
    callers (`worker.py:899`, `test_snapshot_path_live_pin.py:366`).
- **Compositions without a stage.**
  - The only ones with a trace are in tests: `test_worker_queue_postgres.py`, `test_worker.py` and
    `test_queue_messages.py`. In production, `github_adapters` (`worker.py:738-748`) and the GitLab
    bundle always build a stage.
  - Such a composition now records no `context.build` and writes no payload. The wrapper is not kept
    for them.

### D2. `diagnostic_path` (item 3)

- A helper `diagnostic_text(path: str) -> str` in `diff_map.py`, next to `is_supported_path`, with
  its own character bound `MAX_DIAGNOSTIC_CHARS` next to `MAX_PATH_BYTES` (which counts UTF-8 bytes).
- **Escaping.** Backslash becomes `\\`. Every character of category Cc, Cf, Cs, Zl or Zp becomes
  `\xNN`, `\uNNNN` or `\UNNNNNNNN`. All other characters, Cyrillic included, are kept.
  - The escape is reversible, so the CVS §9.1 lossless identity is not lost.
  - It replaces nothing with `?`, unlike `policy_errors._safe_excerpt`.
- **Bound.** Up to 1 024 characters of escaped text (`MAX_DIAGNOSTIC_CHARS`); a longer text is cut on
  an escape boundary and gets `…` on top. The escape is reversible, the truncation is not.
- **Where.** Only in `context_summary` (`review_context_stage.py:196`).
  - `ContextOmission.diagnostic_path` stays raw, as `test_review_context.py:687` pins.
  - The canonical `path` is unchanged.
- **A NUL is not a red case.**
  - The stage reads stored `code_change_diffs`, whose `filename` is `String(1024)` (`models.py:153`).
    PostgreSQL character types reject a NUL, and a name over 1 024 characters is not stored either.
    That ordering is read from the code, not tested.
  - A git provider cannot send one anyway. The escape still renders one as `\x00`.
  - The raw name reaching the stage is therefore at most 1 024 characters, but escaping can expand it
    up to 10 times, so the bound is still needed.
- **Placement.** The helper goes after `is_supported_path` (`diff_map.py:155-170`). #283's
  `diff_map.py` hunks start at `:178` and #340's are at `:29-43`.

### D3. Excluded-files counter (item 4)

- No code change. The gaps are recorded for Q3:
  - legacy has no counter;
  - the snapshot path has no counter after a caught build failure.
- In both cases the names are already hidden (`execute_review.py:253-254`).

### D4 and D5

- **Item 5.** The text becomes «В блоке review-ignore слишком много масок или слишком длинная маска.»
- **Item 6.** A pure deletion. `PullRequestLocator` stays imported, because `:690` uses it.

## Trace contract consumers and API impact

- **openapi.**
  - `RunAction.response` is any JSON, and `tool` is free text (`contracts/openapi.yaml:1308-1361`).
  - The `RunStatus` description (`:915-924`) lists tools, not counts.
  - So there is no openapi edit. The ui Zod snapshot (`UI_CONTRACT_COMMIT`, `test_ui_zod_contracts.py:27`)
    needs no re-pin.
- **The API response does change in content.**
  - `GET /api/runs/{id}/actions` and the SSE step stream lose the `context.build` after
    `llm.repo_conventions`, and the indices after it shift by one.
  - The tech lead accepted it (Q1).
- **UI consumer: a behaviour regression.**
  - ui `main` `a1f75136`, `src/entities/run/lib/runProgress.ts:111-119`: when a `policy.load` exists,
    "context done" needs a non-failed `context.build` *after* `llm.repo_conventions`.
  - After F3 that never happens. The Context stage stays "process" for the whole review, and a failure
    in analysis shows as a Context error.
  - `llm.repo_conventions` is written on a cache hit as well as on a miss
    (`conventions.py:456-464`, `conventions_unit_of_work.py:82-90`). So the ui rule "context done once a
    non-failed `llm.repo_conventions` landed" holds on both.
  - ui#196 (createunix) touches `RunDetailPage.progress.test.tsx`. The ui mocks
    (`mockRunTimeline.ts:35-42`, `:195-197`) show two builds.
- **PIPELINE_SPEC rows:** `:110` and `:172` (the stage's record), `:113` and `:176` (the prompt read's),
  the order bullets `:126` and `:129-130`, and the canon `:135`.
- **Dashboards.**
  - `overview.json:560` and `:618` and `MONITORING.md:61` say the step is written twice.
  - After F3 the counts halve and the p95 shifts. Older attempts keep two records.
  - No alert or query keys on `context.build`.

## Tests that flip (grep list; the authoritative list is the step-0 red set)

Step-0 red set (2026-10-27, `f40cabf3` with the wrap deleted, full suite, PG and RabbitMQ on
`imp130f3`, `GITHUB_ACTIONS=true` so the GitLab broker-guarded files run): the three
`test_snapshot_path_e2e_postgres.py` tests, the two live-pin tests and
`test_full_path_queued_running_publishing_succeeded_is_readable_over_rest`; plus the three
local-only `test_gitlab_admin_postgres.py` CLI failures (the `operator.py` shadow, red at the base
too). No test was skipped. `test_run_context_origin_postgres.py` builds the class itself, and the
policy-context and GitLab worker rows are new pins, so none of them is in the set. The table needs
no addition.

| Test | What it pins today | After F3 |
|---|---|---|
| `test_snapshot_path_e2e_postgres.py:471-500` (`_SNAPSHOT_PATH_STEPS`, `_LEGACY_STEPS`, the comment `:471-474`) | two builds on both paths | one build; add: the payload row equals the stage response |
| `test_snapshot_path_live_pin.py:553-559` (`_legacy_responses`, prompt-read tuple), `:613-622` (`_assert_legacy_run` tools) | the legacy trace with two builds | one build; the provider messages are unchanged (the legacy prompt does not move) |
| `test_worker_queue_postgres.py:701`, `:704-708` | composition without a stage: one build, `level_used == "L1"` in the payload | no build and no payload row. The queue/REST property stays; the per-provider rows are pinned in the e2e and GitLab worker tests |
| `test_run_context_origin_postgres.py:30`, `:74-216` (`test_paid_conventions_context_build_preserves_terminal_payload`) | the prompt-read write fences a retired origin after the paid conventions call | re-target: (a) the stage's write with a retired origin → `RunCancelled`, the terminal payload kept, 0 LLM calls; (b) the post-conventions fence through `CheckpointedProvider` + `AttemptCheckpoint`, still 1 paid call |
| `test_run_policy_context_origin_postgres.py:622-625` | "the prompt read's own `context.build` is not pinned here" | pin exactly one build |

## Docs: in F3 versus G2

**In F3, with the behaviour:**
- PIPELINE_SPEC:
  - delete the rows `:113` and `:176`; move "тот же summary сохраняется в `context_payloads`" into `:172`;
  - `:172`: drop «переходная»; `context_payloads` is written from this response (none on a skip or a
    caught failure); `omissions[].diagnostic_path` is escaped and bounded (CVS §9.1);
  - `:126`: one `context.build`;
  - `:130`: "omitted_files входа промпта" is replaced. Corrected in T1: the stage's record does not
    list the allowed files either (`blocks: []`, their lines in `reviewable_lines`); its `omissions`
    holds only the unusable files and the ignored count (`review_context.py:455-477`).
  - `:110` (fix round 1): drop «переходная»; the «#168 ч.6» clause says the record is the attempt's
    only `context.build`, before conventions; «Результат пока не передаётся модели» stays for G2;
  - the canon `:135` (fix round 1): one `context.build` before conventions, in the order of `:126`.
- `MONITORING.md:61`, `overview.json:560`, `:618`: once per attempt since F3; attempts before F3 show two.

**G2 (G plan P2, P5, P6), stays there:**
- the rest of `:110` («Результат пока не передаётся модели (остаток #130)»);
- the snapshot-path order bullets `:125-131`, rebased on F3's `:126` and `:130`;
- optionally `:111` "а не после `context.build` входа промпта", which becomes moot;
- the RFS §5.3 note on the legacy counter (Q3 accepted).

The G plan's P2 text "F3 removes the prompt-read record" then becomes past tense.

## Open PRs and coordination

| PR | F3 files it touches | Overlap |
|---|---|---|
| #283 ndovnar | `review_context_stage.py` (the `run` `try` block `:612-626`, `_record_nothing_to_review`); PS `:106-188`; `test_snapshot_path_live_pin.py:512` (+16 inside `_legacy_responses`); `test_gitlab_worker_postgres.py:280-290`; `check_runs.py:120-180`; `diff_map.py:178-238` | **Real:** PS (every F3 line) and the live pin (same function). **Avoided by placement:** the stage write goes below the `try`; the GitLab assertion goes before `:280`; `check_runs.py:55` is apart. #283's `coverage` key would then land in the payload row too |
| #330 axyi | PS `:173` and `:177`; `test_snapshot_path_e2e_postgres.py` (hunks end at `:467`; a new test uses `steps.index("context.build")`) | PS `:172` and `:176` are **adjacent**, so whoever merges second rebases. The e2e change is compatible with one build |
| #340 ndovnar | `worker.py` (imports `:165`, settings, `execute` `:909+`); `diff_map.py:29-43`; new `bitbucket_reviews.py` with its own `ReviewContextStage` and `trace: RunTrace \| None` | **Semantic:** without `payloads=trace` and the `ContextTrace` annotation, Bitbucket runs get no payload row, or mypy fails after the rebase. No textual overlap (the `worker.py` hunks miss `:572-696`) |
| #337, #338, #339, #334, #342 | none of F3's files | none. Note to #342: the typo text reaches rules-status through `failure_reason` until #342 lands |

Comments (texts approved before posting):
- **#283:** the PS lines and the live-pin function, with who rebases. The stage write sits below the
  `try`, so the `coverage` key lands in the payload.
- **#330:** the PS `:172`/`:176` adjacency.
- **#340:** pass the attempt trace as `payloads` in the Bitbucket stage, plus one row-count assertion.
- **ui:** a new issue for `runProgress` (Q1), with an FYI on ui#196.
- **#168:** the AC note on item 4 (Q3).

## Decided by the tech lead on 2026-10-27

Recorded per the coordinator's brief; one line per decision.

- **Q1** accepted: the prompt read's `context.build` is removed (once per attempt, the stage's record survives, TL-9) and the API content change is accepted (one fewer action, shifted indices, no schema change); a backward-compatible ui change goes first (ui issue and PR by the coordinator: "context done" is a non-failed `llm.repo_conventions`, the old rule kept for logs without `policy.load`); F3 merges only after that ui change is seen live on staging-ui, and the ui change reaches prod before the tech lead's prod deploy of F3; F3 is written now, the merge gate is the coordinator's.
- **Q2** accepted, both parts: (a) `context_payloads.schema_version=2`; (b) a caught build failure writes no row, with the known limitation that a retry whose build fails keeps the earlier attempt's row.
- **Fix round 1** (PS `:110` and the canon `:135`): fixed in F3, not G2, but only what F3 makes false.
- **Fix round 1** (wiring guard): an AST test in a new file checks that every `ReviewContextStage(...)` call in `app/` passes `payloads`, and that it finds at least the GitHub factory and the GitLab bundle.
- **Q3** accepted: item 4 is met (names are hidden on both paths, the counter is v4-only, legacy without the counter is pinned by a test); recorded in the AC check, the RFS §5.3 note goes to G2.

## Already done or wrong in the sources

- **Already done.**
  - Item 4 on the snapshot path (F1-4).
  - Item 2's counts (T3 #301); only the names leak, and item 1 removes them.
- **Stale anchors.**
  - The AC's `check_runs.py:52` is `:55`.
  - The AC's `worker.py:537, 551, 583, 588` and the brief's `~:610` are `:644`, `:658`, `:691`, `:696`.
  - The parent todo's `review_context_stage.py:184` is `:196`, and `run_trace.py:131-174` is `:136-179`.
- **Superseded.**
  - 6239860585 (the prompt-read row survives, after conventions) and 6396093271 ("order per #165"),
    both by TL-9.
- **Commit format and branch.** The parent plan's `(#130)` subjects and branch name are replaced by the
  brief's.

## Tasks (details, mutants and "done when" in the todo)

0. `docs(plans): plan the #168 part 6 trace and output items (#168)`, with `Refs #130`.
1. `fix(reviews): record context.build once per attempt (#168)`: items 1 and 2, docs and dashboards.
2. `fix(reviews): escape diagnostic paths in the context trace (#168)`: item 3.
3. `fix(reviews): correct the ignore limit check-run text (#168)`: item 5.
4. `refactor(worker): drop the unused policy source locators (#168)`: item 6.

Every task body carries `Refs #130`. Tasks 2-4 are independent of task 1. Task 1 is written now; its
merge waits for the ui change seen live on staging-ui (Q1).

## Gates (every task; `uv sync` first if `.venv` is missing; `df -h /tmp` before long runs)

```
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/imp130f3 \
TEST_RABBITMQ_URL=amqp://app:app@localhost:5672/imp130f3 uv run pytest
```

Known local-only noise:
- 7 failures in `tests/test_deploy_monitoring.py` from the `~/.local/bin/sh` wrapper (clean PATH, and
  basetemp outside the worktree).
- Never read any `.env`.
