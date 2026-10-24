# Implementation plan: RAG context provider, review-side index refresh (#144)

## Status and baseline

Approved by the user on 2026-10-23 under the agent-loop skill. This document is the
English task brief for the executor of PR 1 and PR 2. It translates the approved plan
and adds no new decisions, columns, methods or scope. Where the approved plan leaves a
name or a shape implicit, this brief says **suggested** (the executor may pick another
name) or **derived** (it follows from the cited code or from a repository rule, not from
a new decision).
Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/144, epic #139, part 5.
Baseline: `origin/main` `22c621ab` (#240, landing edge). The single Alembic head is
`20261020_0037` (`code_index_wake_marker`, #143). Worktree `projects/dmc-268-api-t6-144`.
Branches, stacked: `feat/144-rag-index-review-refresh` (PR 1, from `origin/main`) →
`feat/144-rag-context-provider` (PR 2, on PR 1). PR 3 gets its own planning session
(below). Task checklist: [144-todo.md](144-todo.md).

Every `file:line` below was checked against `22c621ab` on 2026-10-23. Corrections to the
approved plan applied here:

- `app/modules/reviews/application/review_context.py`: the L1-truncated early return is
  `:429` (was `:428`); the other early returns are `:382-391` (summary-only, no changes,
  all ignored) and `:442` (`tree_unavailable`); `execute` spans `:310-546`, `take()` is
  `:397-403`.
- `app/modules/reviews/infrastructure/blob_cache.py`: `SqlAlchemyBlobCache` is `:52-104`
  (was `:51-94`); the blob-cache write in `process_run.py` is `:349-354` (was `:349-353`).
- `app/modules/code_index/infrastructure/maintenance.py`: `_share_repository` `:209-214`,
  `rearm_due` `:216-226` (was `:209-222`).
- `app/worker.py`: `review_context_stage_factory` starts at `:524` (was `:525`); `:536` is
  `def stage`, the `context_builder` closure is `:537-555`.
- The port read API is `app/modules/code_index/application/index_store.py:384-414`;
  `get_state` is `:206`. ADR §3 is `docs/adr/0001-graph-rag-v1.md:105-126`.
- The `_Index` dataclass and the `index` fixture are
  `tests/test_code_index_store_postgres.py:928-955` (was `:926-952`).
- `tests/test_code_index_store_postgres.py:2473` is the `FOR UPDATE` re-check test of
  `purge_unused` (two sessions, a held lock). No SKIP LOCKED test exists in `code_index`; the
  only SKIP LOCKED precedent is `app/modules/repositories/infrastructure/rules_status.py:228`.
- SD §7.4 "index row contract" is `docs/SYSTEM_DESIGN.md:945-1003` (anchor `:945`; the
  index-row contract is on `main`). The index status paragraph is `docs/SYSTEM_DESIGN.md:1677-1689`.
- `SqlAlchemyCodeIndexSnapshotUnitOfWork` does not exist yet; it is new in PR 2.

Brief-only names are **suggested**: `ReviewIndexAccess`, `ReviewIndexUnitOfWork`,
`OpenReviewIndex`, `RagReadiness`, `RagPlan`, `RetrievedBlock`, `RagStats`,
`ContextLevel.RAG`, `GraphRetriever` (ADR §5 names it), `resolve_binding`,
`SqlAlchemyCodeIndexSnapshotUnitOfWork`, the migration slug and the test file names.

## Context

#144 is part 5 of epic #139 (Graph RAG v1). Goal: the review sees unchanged code that
imports or uses the symbols the PR changed (reverse edges of the graph), read at the PR
revision. Handed over from #143 as well: the index update requested by a review, and
extending `last_used_at` on a read.

`main` lacks the foundation for part of the 13 AC:

- `app/modules/reviews/application/process_run.py:243` calls the context stage and drops
  the `ReviewContextPayload` it returns: L1–L4 never reach the prompt. This is the
  remainder of #130 (CVS §12 item 3); no PR exists.
- `app/modules/reviews/application/prompt_budget.py:5`: L2–L4 and the full
  `BudgetAllocator` are backlog. The pre-send source guard and the manifest after fit are
  in the open PR #230 (#219, REQUEST_CHANGES).
- ADR §9 `docs/adr/0001-graph-rag-v1.md:539-540`: when the pinned prompt version has no
  retrieved section, RAG is skipped with `rag: not_ready`, reason `prompt_version`. Until a
  v5 prompt exists, retrieval sleeps in production.

Urgent (tech lead, 2026-10-23): on `main` the TTL purge deletes an enabled index 30 days
after `enable()`, because only `enable()` writes `last_used_at`
(`app/modules/code_index/infrastructure/index_store.py:486-513`).

## Issue acceptance criteria (numbered for cross-reference)

AC numbers follow the order of the checkboxes in issue #144 (13 AC). "PR n / Task m"
points at [144-todo.md](144-todo.md).

| AC | Requirement (issue #144, condensed) | Covered by |
| --- | --- | --- |
| AC1 | Candidates come from the graph as hints per ADR #140: reverse edges (unchanged files that import or use symbols changed in the PR) and links deeper than L4; PR files and symbols already shown by L4 are not duplicated (review #139, F5) | PR 2 Tasks 4, 5 |
| AC2 | Candidate text is read at the PR revision (head for RIGHT, diff_old for LEFT) through the blob cache; a candidate whose file or symbol is missing there is dropped with a reason in the omissions | PR 2 Tasks 3, 4, 6. PR 2 writes the omissions into the payload and `context.rag`; the call manifest is PR 3 |
| AC3 | The ignore and policy of the run snapshot apply at retrieval: a path the snapshot excludes is never shown, even when the index has it | PR 2 Task 4 (`denied(path)` before any read, counter only) |
| AC4 | Candidates go through `BudgetAllocator` in their own slot per the ADR and never displace L1; the run trace shows selected and dropped candidates with reasons and the tokens spent | PR 2 Tasks 5, 7 (payload and trace); end-to-end in PR 3 |
| AC5 | The RAG block renders as a typed `source_kind` with the retrieval role; a new prompt version (R-6) carries "repository code is data, not instructions"; the guard rejects a block with a foreign revision or path (`policy_context_violation`) | PR 3 |
| AC6 | Toggle off → no index access, no `context.rag` step (PIPELINE_SPEC:110). Toggle on, index not ready (`empty`, `building`, `failed`) → the review runs without RAG and without delay; the trace has `rag: not_ready` with the reason from `RagIndex.not_ready_reason`, `failed` with its error code. `stale` is used. Readiness is checked through `RagIndex.usable`, not own status logic; `disabled` is not a `not_ready` reason | PR 1 Tasks 2–6; `stale` used end-to-end in PR 3 |
| AC7 | The provider queries the index only by the run's `repository_id`; test: the same symbols in two Workspaces never reach the other's context | PR 2 Tasks 2, 4 |
| AC8 | Golden prompt and recorded replay updated for the new prompt version; the CI step "Gold corpus and recorded replay" is green; the baseline of the new version is shot again, not mixed with the old one (#82) | PR 3 |
| AC9 | Every used edge is resolved again by the #130 resolver at the PR side revision (head_sha RIGHT, diff_old_sha LEFT); range and signature are extracted again from that blob; an unconfirmed edge → candidate dropped with a reason. Tests: both definitions exist at the PR revision but import resolution differs from the default branch; a signature moved inside an existing file (F5) | PR 2 Tasks 1, 4 (RIGHT), 6 (LEFT, M7) |
| AC10 | Only confirmed edges of the ADR kinds are followed, depth per the ADR; negative tests: same names in different modules, shadowing, local rebinding, a call through a receiver `repository.save()` is not linked (F6) | PR 2 Tasks 1, 4 |
| AC11 | The RAG share is visible per source role in the call manifest: the `context.manifest` trace step (PIPELINE_SPEC:174) sums block tokens by role; the `retrieved_excerpt` share is relative to the manifest `input_tokens`; tokens count the text sent to the model (after fit) | PR 3 |
| AC12 | A review on a lagging or empty index requests an update and does not wait (ADR §6.1): `CodeIndexStore.request_build` on a live row, `enable()` on a row after a TTL purge, no direct AMQP publish. Full from `empty` at most once per target commit and once per 24 h; where the throttle lives is written in SD. Tests: a lagging index gets the wake-up marker while the review runs on; a TTL row comes back in a new generation; a repeat within 24 h queues no full | PR 1 Tasks 1–4, 7 |
| AC13 | A review read extends `last_used_at` at most once an hour per repository (ADR §4); incremental builds do not. Test: TTL (`CODE_INDEX_TTL_DAYS`) does not purge an index the review reads and purges one without reads. The "until #144" note in SD §10 is removed | PR 1 Tasks 2, 4, 7 |

DoD (gates, documents, PRs with `Refs #144` and no auto-close, approve on the current head,
threads resolved, tech-lead AC check, status in #139 and on board 12) is covered by the
process section and the external items in the todo.

## User decisions of 2026-10-23

- **U1. Three stacked PRs; #144 stays open until PR 3.** AC numbering follows the issue
  body (13 AC).
  - PR 1 now: AC6, AC12, AC13 (lifecycle, readiness, `context.rag` `not_ready`).
  - PR 2: AC1, AC2, AC3, AC4 (in the payload), AC7, AC9, AC10 (retrieval into the payload
    and the trace).
  - PR 3, after the #130 remainder and the merge of #230: AC5, AC8, AC11; the end-to-end
    check of AC4 and AC6.
- **U2. Prompt order v4 (the #130 remainder) → v5 (#144) built on v4.** One paid baseline
  shot, after v5 (#82).
- **U3. The review full-build throttle** lives in two `code_index_states` columns,
  `review_full_requested_at` and `review_full_requested_sha`; SD §10 records where it lives.
- **U4. `last_used_at` is extended when a review reaches a usable index** (`ready` or
  `stale`), even when RAG is then skipped for `prompt_version`, and on every full build a
  review queues; at most once an hour. This reading of ADR §4 (`docs/adr/0001-graph-rag-v1.md:168`)
  goes into the PR body.

## Author decisions (stated in the PR body)

- **M1. Throttle (ADR §6.1, `docs/adr/0001-graph-rag-v1.md:231`): both limits together.**
  - The key is the `target_commit_sha` of the row under the lock. A NULL target (after
    `purge()`, which clears it, `app/modules/code_index/infrastructure/index_store.py:539`)
    leaves only the 24 h limit.
  - The throttle applies to `enable()` after TTL too.
  - Derived: `purge()` stays unchanged and keeps both columns, as it keeps `last_used_at`
    (`app/modules/code_index/infrastructure/index_store.py:527`); otherwise the 24 h limit
    could not hold across a TTL purge.
  - The CHECK is "sha implies at", not equal nullness, because M1 records a NULL target
    with a timestamp; it is named `ck_code_index_states_review_full_sha_at` so it does not
    read like the lease pair; `purge()` keeps both columns (decided by the coordinator, 23.10).
- **M2. A review never writes the target.** Always `request_build(target_commit_sha=None)`:
  the review does not know the default-branch HEAD; merge, push and the reconcile own the
  target.
- **M3. No waiting.** The state row is taken `FOR UPDATE SKIP LOCKED`. When a build holds
  it, the gate only reads and writes nothing; the next review does the extension.
- **M4. `enable()` only when `purge_reason == ttl`**, re-read under the lock. Otherwise a
  race with turning the toggle off would bring the index back.
- **M5. `stale` wakes too**, if the marker is not set and no terminal refusal exists for the
  same target. This mirrors `refused` in `reconcile_head`
  (`app/modules/code_index/infrastructure/index_store.py:615-621`); `request_build`
  (`:574-586`) does not look at `last_error_code`.
  - The condition (decided by the coordinator, 23.10): no wake marker, no live build lease
    (`lease_expires_at > now` under the lock; after review r2, from the review r1 fix), and
    `last_error_code IS NULL OR last_error_code IN HEALABLE_ERROR_CODES`
    (`app/modules/code_index/application/vcs.py:29-36`). The review knows no HEAD, so "the
    same target" is the row's own `target_commit_sha`. This relies on #143: a target change
    clears `last_error_code` — `request_build`
    (`app/modules/code_index/infrastructure/index_store.py:583-584`) and `reconcile_head`
    (`:629-632`), per the ADR §6.1 row `docs/adr/0001-graph-rag-v1.md:230` — so any stored
    code belongs to the current target.
- **M6. The capability set of prompt versions with a retrieved section** is a constant in
  `app/modules/reviews/application/rag_index.py`, empty until v5. It lands in PR 2 with its
  first caller (moved to PR 2 Task 7 after review r1; PR 1 has no caller of it).
  - Derived: the gate compares it with the run's pinned prompt,
    `(snapshot.prompt_bindings.review_system_key, snapshot.prompt_bindings.review_system_version)`
    (`app/modules/reviews/application/policy_snapshot.py:136-137`). The adapter formats
    the version as `v{N}` (`app/modules/reviews/infrastructure/policy_snapshot_unit_of_work.py:125`).
- **M7. Broken reference (ADR §8.3) is in PR 2.** It is the only path that reads LEFT
  (`diff_old_sha`); without it AC2 and AC9 are only half covered.
- **M8. `context.rag` is written after `context.build`.** When the builder returns early
  (L1 truncated, no tree, summary-only), the step is `used` with a `reason` and zero
  counters. This extends the contract of `docs/PIPELINE_SPEC.md:173`. Open, decided in
  PR 2: the shape of `context.rag` after a caught build failure (PR 1 records the
  readiness; see "Trace `context.rag`").
- **M9. `resolve_binding`** is a new public method of `ImportResolver` without the L4 caps
  per run and per file. `resolve()` calls it inside; L4 behaviour does not change.
- **M10. RAG reads go through the blob cache.** Every distinct blob counts toward the limit
  of 20 reads, on a hit and on a miss alike (determinism). A failed `put` is logged; the
  run does not fail.

## Mechanism map (`origin/main` `22c621ab`)

| Mechanism | Where | What matters |
| --- | --- | --- |
| Index read API | port `app/modules/code_index/application/index_store.py:206`, `:384-414`; adapter `app/modules/code_index/infrastructure/index_store.py:1073-1194` | `incoming_edges` (`:1180-1188`), `importers_of` (`:1190-1194`), `get_state` (`:482-484`); no consumers today. `outgoing_edges` (`:1151-1178`) joins `target_name` to `qualified_name`, which is wrong for TS `default` and `export { x as y }` |
| Visibility | `app/modules/code_index/infrastructure/index_store.py:152-176`; ADR §6.2 `docs/adr/0001-graph-rag-v1.md:341-348` | READY plus the current generation, joined per statement; the state and the edges are read in one REPEATABLE READ read-only transaction before the first blob |
| Edges v1 | ADR §3 `docs/adr/0001-graph-rag-v1.md:105-126`; SD §7.4 `docs/SYSTEM_DESIGN.md:945-1003` | `imports` (`target_name = ''`), `uses_imported` (the exported name, matched through `facts.exports`); one reverse step. The index `blob_sha` comes from the GitHub archive and may differ from the Git blob OID |
| Readiness | `app/modules/code_index/domain/rag_index.py:14-70`; `app/modules/code_index/infrastructure/effective_rag.py:15-21` | `usable` / `not_ready_reason`; `RagIndex` drops `purge_reason`, so the trigger branch reads the locked row; the toggle condition is `RAG_ON`, the provider condition `GITHUB_PROVIDER` |
| Triggers | `enable` `app/modules/code_index/infrastructure/index_store.py:486-513`, `request_build` `:574-586`, `reconcile_head` `:588-636` | `request_build` returns False for purged and `failed` rows and does not touch `last_used_at`; `enable` re-arms any purged row; `_state(lock=True)` (`:408-422`) has no SKIP LOCKED option |
| Context build | `app/modules/reviews/application/review_context.py:310-546` | L1 (`:405-429`) → tree (`:431-442`) → L4 (`:489-505`) → L2/L3 (`:509-546`), all through `take()` (`:397-403`); the parser closes in `finally` (`:506-507`) |
| L4 resolver | `app/modules/reviews/application/ast_resolution.py:33-37`, `:499-583` | head only; caps per instance (`MAX_IMPORTED_FILES` 20 via `_Files.facts_for`, `review_context.py:252-260`; 10/100 symbols); only `resolve()` is public; `affected_symbols` `:150-181`, `exported_symbols` `:488-492` |
| Blob cache | `app/modules/reviews/infrastructure/blob_cache.py:52-104`; `app/modules/reviews/application/process_run.py:349-354` | key `(repository_id, blob_sha)`, 7 days; `get` works on a session factory, `put` needs an active UoW session (`blob_cache.py:82-85`), so it runs in its own short UoW (`app/modules/reviews/infrastructure/run_processing_unit_of_work.py:23`) |
| Stage and trace | `app/modules/reviews/application/review_context_stage.py:240-251` (`excluded`), `:272-374` (`run`); `app/modules/reviews/application/run_trace.py:87-121` | no `context.rag` or `context.manifest` step; `traced_step` stores `{error: {type, message}}` and re-raises |
| `context.rag` contract | PIPELINE_SPEC `docs/PIPELINE_SPEC.md:110`, `:173`; ADR §10 `docs/adr/0001-graph-rag-v1.md:543-558` | request `{index_status, indexed_commit_sha, default_head_sha, stale}`; response `{outcome, reason?, candidates{found, confirmed, dropped{…}}, tokens}`; RAG never sets `runs.error_code` |
| Slot | CVS §3.4 `docs/CONTEXT_AND_VERIFICATION_SPEC.md:404-409`; ADR §9 `docs/adr/0001-graph-rag-v1.md:498-509` | ≤4 000 tokens, ≤20 reads, ≤10 files, ≤3 places per file; displaces only L2/L3; priority broken reference > callers of a changed export > importers of a changed module |
| Step catalog | `tests/test_openapi_contract.py:112`; `contracts/openapi.yaml:914`, `:1325` | a new step `context.rag` updates the set and both descriptions; the code scan (`tests/test_openapi_contract.py:1334-1339`) finds a literal or a `*_TOOL` constant |
| Migrations | head `20261020_0037_code_index_wake_marker` | #220/#169 hold their own `20261020_0037`/`0038` |
| Layers | `app/modules/code_index/application/index_store.py:20` imports `app/modules/reviews/application/ast_resolution.py` | `reviews.application` → `code_index.application` is forbidden (a cycle); `code_index.domain.rag_index` imports only its enums, so `reviews.application` may use `RagIndex` (precedent `app/modules/repositories/application/repository_settings.py:12`). Ports live in reviews, adapters in `app/modules/code_index/infrastructure/` (precedent `app/modules/repositories/application/code_index.py` + `app/modules/code_index/infrastructure/purger.py`) |

## Out of scope

- Building the index and updating it on merge, push and the reconcile (#142, #143); the
  toggle (#145); vector search (#147).
- In PR 1 and PR 2: passing the payload into the prompt, rendering the typed block, the v5
  prompt, the pre-send guard, the evidence allowlists, the manifest step, the golden
  prompt, recorded replay and the baseline — all PR 3.
- Extending the resolver beyond v1 (receiver calls, methods through values, barrel
  re-exports): only if eval #146 shows value (ADR §3).

## Coordination (before the edit)

Comments already posted on 2026-10-23: #144 `6371695341` (the 3-PR split and the AC
layout), #230 `6371695929`, #220 `6371696387`, #227 `6371696846`, #229 `6371697254`.

Still pending: **#130** — the tech-lead decision U2 (v4 → v5, one shot). The comment must
name the v4 owner; #130 is on axyi now, while the 16.10 decision named createunix. The owner
is not decided yet, and without it PR 3 has no starting point.

Before every docs commit, re-check the open PRs and the files they touch:

- **#230**: `app/modules/reviews/application/review_context_stage.py`, `app/worker.py`,
  `tests/test_review_context_stage.py`; `app/modules/reviews/application/process_run.py`
  (PR 3).
- **#220 / branch of #169**: the migration number `0038`, the stage, `review_context.py`,
  the worker, SD, PIPELINE_SPEC, `tests/test_code_index_store_postgres.py`.
- **#227, #229**: CVS, PIPELINE_SPEC, SD.
- **#139**: a note on the 3-PR split.

## PR 1: `feat(code_index): refresh and keep the index alive from reviews`

Branch `feat/144-rag-index-review-refresh` from `origin/main`. AC6, AC12, AC13.

### Gate placement

- In `ReviewContextStage.run`, after the skip exits (the last one is
  `app/modules/reviews/application/review_context_stage.py:350`) and before `context.build`
  (`:352`).
- A new optional constructor argument `rag=None` (suggested type: the use case
  `OpenReviewIndex`). `None` keeps today's behaviour: no index access and no step. Eval and
  replay compositions that build the stage without it are unchanged.
- The gate runs after the stage's GitHub reads (`:294-311`) and makes no network call.

### Port, use case and capability set (`app/modules/reviews/application/rag_index.py`, new)

- Port `ReviewIndexAccess` with `open_for_review(repository_id: UUID) -> RagReadiness`.
  Flush-only, never commits.
- `ReviewIndexUnitOfWork` (a `UnitOfWork` exposing the access), and the use case
  `OpenReviewIndex.execute(repository_id) -> RagReadiness`, which commits.
- `RagReadiness(enabled: bool, index: RagIndex | None, default_head_sha: str | None)`;
  `index` is `None` when `enabled` is False.
- M6: the capability set constant (suggested `RETRIEVED_PROMPT_VERSIONS:
  frozenset[tuple[str, str]] = frozenset()`) — moved to PR 2 Task 7 after review r1: PR 1
  has no caller of it.
- Imports only `app/modules/code_index/domain/rag_index.py` from `code_index` (see
  "Layers").

### Decision (`app/modules/code_index/domain/review_trigger.py`, new)

A pure function: the input is the state row's fields and `now`, the output is which
writes to make. The adapter runs it on the unlocked read first, only to learn whether the
review writes, and again on the locked row with `now` read after the lock; the writes come
only from that second decision (after team review r1). Suggested inputs: the derived `RagIndexStatus` from `rag_index(...)` and
its `usable` (so status derivation stays in one place; after review r1 the decision keeps
no copy of the usable statuses), `purge_reason`, `wake_requested_at`, whether a build
lease is live (`lease_expires_at > now` under the lock, after review r1),
`last_error_code`, `target_commit_sha`, `last_used_at`, the two throttle columns and `now`.
Suggested outputs: an action (`none`, `enable`, `request_full`, `request_update`), whether
to extend `last_used_at`, and whether to record the throttle.

| Row | Action | `last_used_at` | Throttle |
| --- | --- | --- | --- |
| no row | none | — | — |
| `purge_reason` `removed` / `disabled` | none (the converge pass owns it) | — | — |
| `failed` | none | — | — |
| `building` | none | — | — |
| `purge_reason` `ttl` | `enable()` if M1 allows (M4) | set by `enable()` | recorded |
| live `empty` | `request_build(None)` if the marker is unset, no build lease is live, no terminal refusal (M5) and M1 allows | extended if NULL or older than 1 h (after review r1; the full itself is not limited by the hour, U4) | recorded |
| `ready` | none | extended if NULL or older than 1 h | — |
| `stale` | `request_build(None)` if the marker is unset, no build lease is live (after review r1) and no terminal refusal (M5) | extended if NULL or older than 1 h | — |

M1 in full (derived from the decision text): a review full is allowed when
`review_full_requested_at` is NULL or at least 24 h old, **and** the row's
`target_commit_sha` is NULL or differs from `review_full_requested_sha`. So a repeat
within 24 h is refused; after 24 h the same target is refused and a new one is allowed.

### Adapter and its transaction (`app/modules/code_index/infrastructure/review_index.py`, new)

`SqlAlchemyReviewIndexAccess(session)` (suggested) and its UoW (suggested
`SqlAlchemyReviewIndexUnitOfWork(SqlAlchemyUnitOfWork)`, like
`app/modules/code_index/infrastructure/maintenance.py:242-245`). It writes the marker and the
generation only through the public `request_build` / `enable` of
`SqlAlchemyCodeIndexStore`. The `CodeIndexStore` port and `_PORT_METHODS` do not change.
The adapter owns three statements of its own: the SKIP LOCKED select, the `last_used_at`
UPDATE and the throttle UPDATE.

One transaction, READ COMMITTED, no network:

1. The condition `RAG_ON` **and** `GITHUB_PROVIDER` first without a lock (after review
   r1): if it fails → `RagReadiness(enabled=False)` with no lock taken. Otherwise
   `SET LOCAL lock_timeout = '2s'` (after team review r1), the `repositories` row
   `FOR SHARE`, then the condition again in a statement of its own (as
   `_share_repository` / `rearm_due`,
   `app/modules/code_index/infrastructure/maintenance.py:209-226`). GitLab runs (#220) go
   through the same stage. If the condition fails → `RagReadiness(enabled=False)`; the
   state is not read and no step is written. A writer of the `repositories` row that
   outlasts the lock timeout fails the gate with a lock error: the stage records
   `context.rag` `{error}` and the review goes on without RAG, not on the deadline path.
2. The state row without a lock first (after team review r1), and the decision on it with
   `clock_timestamp()`: no row → `empty`; no writes → readiness from that read, no row lock.
3. Writes needed → the state row `SELECT … FOR UPDATE SKIP LOCKED` (M3; pattern
   `app/modules/repositories/infrastructure/rules_status.py:228`). An empty result means
   the row is locked (or gone): read it without a lock, read-only, no writes. Otherwise
   read `clock_timestamp()` after the lock (`_clock`,
   `app/modules/code_index/infrastructure/index_store.py:433-436`) and decide again on the
   locked row, which may have changed since the read; apply that decision. `last_used_at`
   and the throttle columns are written with that time.
4. Return `rag_index(...)` over the row as it stands after the writes, with
   `default_head_sha = target_commit_sha`.

### Readiness only through `RagIndex` (AC6, "not own status logic")

- `usable` / `not_ready`, the `reason` and the `error_code` in `context.rag` come only from
  `RagIndex.usable` / `RagIndex.not_ready_reason` / `RagIndex.last_error_code`. For
  `failed`, `rag_index` puts the state's `error_code` there
  (`app/modules/code_index/domain/rag_index.py:65`).
- The `review_trigger.py` table only picks the write and never reaches the trace.
- Test: a fake `RagIndex` with a patched `not_ready_reason` → its value appears in the step.

### Migration

`alembic/versions/20261023_0038_code_index_review_trigger.py` (suggested slug; the next
free number at commit time), `down_revision = "20261020_0037"`.

- Two nullable columns: `review_full_requested_at timestamptz` and
  `review_full_requested_sha varchar(64)` (derived: the same type as `target_commit_sha`;
  `tests/test_code_index_store_postgres.py:72-106` rejects `text` columns).
- CHECK `ck_code_index_states_review_full_sha_at` (decided by the coordinator, 23.10):
  `review_full_requested_sha IS NULL OR review_full_requested_at IS NOT NULL` ("sha implies
  at"). A NULL target still records the time; `purge()` keeps both columns.
- Downgrade drops the CHECK and the columns. The same objects go into the ORM model
  (`app/modules/code_index/infrastructure/models.py:35-143`).
- Derived (tests and repository rules that otherwise go red):
  - `_COLUMNS` (`tests/test_code_index_store_postgres.py:72-106`) and `_CHECKS`
    (`:167-175`);
  - the `new_columns` set of the 0033 downgrade test
    (`tests/test_code_index_store_postgres.py:832-839`) gains the two columns;
  - head assertions `"20261020_0037"` at `tests/test_code_index_store_postgres.py:727`,
    `:752`, `:878`, and `tests/test_app_reinstall_schema_postgres.py:67-69` (no database;
    runs in plain `pytest`);
  - `docs/CICD.md` §3: a new top row in the downgrade-loss table (`:244`; the rule "in the
    same PR" is `:237-238`) and the revision list `:263`.

### Trace `context.rag`

- Toggle off (or a GitLab repository) → no step.
- `not_ready` → `reason` `empty` / `building` / `failed`; `error_code` only for `failed`.
- Usable (`ready` / `stale`) → `not_ready` / `prompt_version`, because the capability set is
  empty until v5.
- Request: `{index_status, indexed_commit_sha, default_head_sha, stale}`, where
  `default_head_sha` is the index target. Suggested `not_ready` response: `{outcome,
  reason, error_code?}`; `candidates` and `tokens` belong to `used` (PR 2).
- Order (M8; decided by the coordinator, 23.10): the step is recorded once per attempt whenever the gate ran, right
  after the `context.build` try/except (`app/modules/reviews/application/review_context_stage.py:353-371`),
  on both paths — success and the caught build failure (`return None`, `:371`) — and before
  the `ALL_CHANGES_IGNORED` skip (`:372`). The gate runs before `context.build`; its result
  (or its error) is kept and recorded through `RunTrace.record` with the duration of the
  gate (derived). A re-raised build error (`PolicyError`, `RunCancelled`,
  `deadline_exceeded`) ends the attempt without the step.
- A failed gate uses the same except chain as
  `app/modules/reviews/application/review_context_stage.py:363-371`: `PolicyError`,
  `RunCancelled` and `deadline_exceeded` re-raise; anything else is logged, the step gets
  `{error: {type, message}}` (the `traced_step` shape), and the run continues without RAG.
- The tool name must be visible to the code scan of `tests/test_openapi_contract.py:1334-1339`:
  a literal at the call or a `*_TOOL` module constant.

### PR 1 tests

- Unit, decision table, with literal inputs and expected actions (every row of the table,
  the M1 boundaries at 24 h, the 1 h extension boundary, M5 with a healable and a terminal
  code).
- PG (new `tests/test_code_index_review_index_postgres.py`; fixture pattern of
  `tests/test_code_index_maintenance_postgres.py:59-120`, importing helpers from
  `tests.test_code_index_store_postgres`; the `_Index` pair `:928-955` gives two
  Workspaces). Derived: `_repository` (`tests/test_code_index_store_postgres.py:243-266`)
  leaves `rag_enabled` false, so the tests set it.
  - `stale` → marker set, the target unchanged;
  - `ttl` → generation + 1, `empty`, marker;
  - a repeat within 24 h queues no full; the same target after 24 h is refused, a new target
    is allowed;
  - `failed`, `removed`, `disabled` and no row → no writes;
  - the extension at most once an hour;
  - the race with toggle off (as `tests/test_code_index_maintenance_postgres.py:536`);
  - SKIP LOCKED: a second session holds the row (two-session holder pattern of
    `tests/test_code_index_store_postgres.py:2473`) → the gate returns at once, read-only, no
    writes.
- AC13 (PG): two repositories with `last_used_at` 31 days ago (`_unused_for_31_days`,
  `tests/test_code_index_store_postgres.py:2395`); the review reads `a` → TTL purges only
  `b`; an incremental publication does not change `last_used_at`.
- Unit, stage with a fake gate: toggle off → steps `["policy.load", "context.build"]` (as
  `tests/test_review_context_stage.py:239`); `context.rag` for every status, `failed` with
  its code; a failed gate → the run goes on; `stale` → an update is requested and nothing
  waits. A caught build failure and an `ALL_CHANGES_IGNORED` payload still get the
  step after `context.build`; a re-raised build error gets none.
- Unit, use case: commits once; a port error propagates to the stage.

### PR 1 documents

- SD §10 `docs/SYSTEM_DESIGN.md:1505` and `:1543-1545`: drop "until #144"; where the
  throttle lives (U3, M1). §11 `:1594`: the two columns and the CHECK. Derived (schema
  docs the #143 migration also updated): `:745-751` (the downgrade revision list),
  `docs/BACKEND_ARCHITECTURE.md:512-520`, `docs/erd/erd.mmd:166-182`.
- PIPELINE_SPEC `docs/PIPELINE_SPEC.md:110` (written now; position after `context.build`),
  `:123-128` (the attempt order), `:173` (`error_code?`, `{error}`, `default_head_sha` = the
  index target).
- CVS §3.5 `docs/CONTEXT_AND_VERIFICATION_SPEC.md:420-426`: the `failed` code, the review
  request without waiting.
- ADR §6.1 `docs/adr/0001-graph-rag-v1.md:231`: a link to the SD place of the throttle.

### PR 1 body notes

- Deviations: see "Deviations".
- UI follow-up (not a blocker): the UI accepts any tool name; ui must map `context.rag`
  to the CONTEXT stage in one line (see "Risks").

## PR 2: `feat(reviews): add graph candidates to the review context`

Branch `feat/144-rag-context-provider` on PR 1. AC1, AC2, AC3, AC4 (payload), AC7, AC9,
AC10.

Decision (user, 24.10): Task 1b, right after Task 1, stops reading a Python attribute name
as an imported binding (`repository.save()` → `save`) and bumps `facts/v1` → `facts/v2`;
the parser profile changes, so the #143 reconcile rebuilds published indexes as full.

### Where

- In `BuildReviewContext.execute`, after L4 and before the `finally`
  (`app/modules/reviews/application/review_context.py:506`); the parser is still open. The
  logic lives in `app/modules/reviews/application/rag_context.py` (new).
- The stage passes `RagPlan(index_commit_sha, stale)` only for a usable index and a pinned
  prompt version in the capability set (M6), which this PR adds to
  `app/modules/reviews/application/rag_index.py` with that first caller (moved from PR 1
  after review r1). The retriever and the cached blob source are built by the factory in
  `app/worker.py:537-555`.

### Targets

- For PR files with head facts: `affected_symbols`
  (`app/modules/reviews/application/ast_resolution.py:150`), walked up to the top-level
  symbol (derived: `AstSymbol.enclosing`, `app/modules/reviews/application/ast_symbols.py:95`).
- Exported names: TS/JS through `facts.exports`, Python/Go `symbol.name` of an exported
  symbol (as `exported_symbols`, `ast_resolution.py:488-492`).
- A module-level change → `(path, "")`.
- The graph path is `old_path or new_path`.

### Graph

- Port `app/modules/reviews/application/graph_retriever.py` (`GraphRetriever`, ADR §5).
- Adapter `app/modules/code_index/infrastructure/graph_retriever.py`: `get_state` plus
  `incoming_edges` for the targets (≤ ~200), in a new
  `SqlAlchemyCodeIndexSnapshotUnitOfWork`. Its first statement is `SET TRANSACTION
  ISOLATION LEVEL REPEATABLE READ, READ ONLY`; it closes before the first blob read.
- The race between the READ COMMITTED gate and the RR read: the toggle can be turned off or
  the index purged in between. When the RR read finds no READY row → `context.rag`
  `not_ready` with the fresh status, not `used` with `found = 0`. Tested.
- Derived: the blocks carry the `indexed_commit_sha` read in the RR transaction.

### Filters

In this order: dedupe by importer → PR files dropped silently → `denied(path)`
(`review_context.py:322-328`) before any read (only the `excluded` counter, no path) →
symbols in L4's `shown` (`:491-497`) dropped. Priority and limits per the ADR; over a limit
→ `rag_limit` with the path.

### Confirmation at head

- No file in the PR tree → `rag_candidate_missing`. Text and `blob_sha` come only from the
  PR tree (the index `blob_sha` may differ, SD `docs/SYSTEM_DESIGN.md:953-956`).
- Its own `_Files` and `ImportResolver`, reusing what L4 loaded, without the 20-file cap.
- `uses_imported` is confirmed when the binding resolves to the new path and the same export
  (`exported_symbols`, `ast_resolution.py:488`); `imports` when `module_target` contains the
  changed path.
- A place is an inner declaration that reads the alias without rebinding it, plus the import
  line; whole if ≤200 lines and ≤2 000 tokens, otherwise the signature.
- No places → `rag_edge_unconfirmed`; a read error → `source_read_failed`.

### Broken reference (M7)

- The LEFT tree and the facts of changed, deleted and renamed files are read before the
  graph transaction.
- The import resolves at `diff_old_sha` into a module exporting the name, and otherwise at
  head (ADR §8.3, `docs/adr/0001-graph-rag-v1.md:458-494`).
- Two blocks: RIGHT `exact_lines` at head, LEFT `signature` at `diff_old_sha`.
- No `diff_old_sha` → `old_revision_unknown`.

### Payload

- `ReviewContextPayload.retrieved: tuple[RetrievedBlock, ...]` (SD `docs/SYSTEM_DESIGN.md:1457`)
  and `rag: RagStats | None`.
- `ContextLevel.RAG`, role `SourceRole.RETRIEVED_EXCERPT`
  (`app/modules/reviews/application/context_provenance.py:31`), `selection_reason` in
  `SourceRef.reason`, the edge kind and `index_commit_sha`. The block revision is
  `head_sha` / `diff_old_sha`, never the index.
- The slot is `min(4000, remaining)` through the budget of `take()`; the blocks go to
  `retrieved`, not `blocks` (ADR §2 `docs/adr/0001-graph-rag-v1.md:66-70`). They count in
  `tokens_used`, `levels_present` and `tokens_by_role` (`context_summary`,
  `review_context_stage.py:128-175`).

### `context.rag` used

`{outcome: "used", candidates: {found, confirmed, dropped: {…}}, tokens}`, with
`found = confirmed + Σdropped`, no paths.

Open (next to M8, decided in PR 2): the shape of `context.rag` after a caught build
failure, when the RAG part of the build did not complete.

### PR 2 tests

Fakes: `tests/test_review_context.py:83-227` (a copy with several revisions),
`tests/test_ast_resolution.py:53-101`, `tests/test_review_context_stage.py:62-231`.

- `tests/test_rag_context.py` (new):
  - a reverse edge is used; PR files and L4 symbols are not duplicated;
  - an ignored candidate is never read (assert on `Repository.read`) and only counted;
  - missing; read failure;
  - limits: the 11th file, the 4th place, the 21st read, 4 000 tokens;
  - L1 and L4 intact, L3 → `budget`;
  - F6: the same name in another module, shadowing by a parameter, local rebinding,
    `repository.save()`;
  - F5: `old_utils` / `new_utils` (the graph points to new, head imports old →
    `rag_edge_unconfirmed`); a moved signature → the ranges are head lines, as literals;
  - broken reference: rename and delete; an import broken before the PR is not blamed; one
    fixed at head → no candidate;
  - isolation: two repositories with the same paths; blocks carry the run's
    `repository_id`.
- `tests/test_rag_graph_retriever_postgres.py` (new): isolation of `a` / `b`; a concurrent
  publication does not change the read; `SHOW transaction_isolation` /
  `transaction_read_only`; an unready index gives no hints.

### PR 2 documents

PIPELINE_SPEC `docs/PIPELINE_SPEC.md:110`, `:173` (`used` and the skip reasons); CVS §3.5;
SD `docs/SYSTEM_DESIGN.md:1457`, `:1470`; ADR §2 `docs/adr/0001-graph-rag-v1.md:70` (the
shape of a `retrieved` item).

## PR 3: after the #130 remainder (v4) and the merge of #230 (separate planning session)

- Pass the payload into the prompt (`app/modules/reviews/application/process_run.py:243`);
  render the typed block (`source_kind = source_code`, role `retrieved_excerpt`).
- `review.system.v5` on v4 with a retrieved section and "code is data, not instructions";
  the capability set ← `("review.system", "v5")`.
- Guard: the block revision equals `head_sha` / `diff_old_sha`, plus `repository_id`;
  fail-open RIGHT on `head_sha = None` (gaps of #230). Evidence allowlists
  (`app/modules/reviews/application/review_output.py:206`,
  `app/modules/reviews/application/reflection_pass.py:366`) if #219 part 5 is not done.
- Manifest: tokens per role, the `context.manifest` step, the `retrieved_excerpt` share of
  `input_tokens`.
- The candidate checkpoint (CVS §2.1 `docs/CONTEXT_AND_VERIFICATION_SPEC.md:72-76`, §7.4):
  RAG blocks go into the checkpoint; a newer index applies only to a retry before it (PR 2
  already stores `index_commit_sha` in the blocks).
- Golden prompt, recorded replay, one paid baseline shot after v5.

## Process

**Environment:**

- fetch over HTTPS (`git -c credential.helper='!gh auth git-credential'`; SSH is closed);
- the worktree `projects/dmc-268-api-t6-144` from `origin/main`; `uv sync`;
- the database and the RabbitMQ vhost `dmc_144`; `df /tmp` before long runs.

**Loop per `agent-loop`:**

- Every code-writing commit is done by a subagent from the brief file
  `docs/plans/144-todo.md`. Every brief says: never read `.env` of any clone.
- Review is done by a clean `code-reviewer`: only `git show` / `git diff`, no checkout;
  after the review, check the branch.
- Check the reviewer's advice against `.agents/rules/backend.md` before pushing.
- Gates green on every commit.

**PRs:**

- body What / Why / How to verify / `Refs #144`, no closing keyword; no new dependencies;
- the deviations U4, M1–M10 and the derived ones below; reviewer ndovnar;
- PR 2 is rebased after PR 1 merges;
- #144 → On Review with PR 1; after its merge back to In Progress (AC1–AC5, AC7–AC11 open).

**After the merges:** status in #144 and #139, the board; lab paperwork
(`docs/reports/144/`, the mirror, `economics.md`, `/verify-run`); clean up the worktree,
the database and the vhost.

## Deviations (stated in the PR body)

- **U4 (PR 1).** `last_used_at` is extended on a review read of a usable index even when RAG
  is skipped for `prompt_version`, and on a review full, both at most once an hour (after
  review r1: the full itself is not limited by the hour); the reading of ADR §4.
- **M1–M10** as stated above; the M1 CHECK and the M5 condition are decided by the coordinator, 23.10.
- **GitLab (PR 1).** A GitLab repository with RAG on counts as disabled: no index source
  (#97), no step.
- **Lease (PR 1; after review r2).** No wake while a build holds a live lease. This narrows
  the literal AC12 «отстающий индекс получает маркер пробуждения»: a `stale` row under a live
  lease gets no marker from the review. The leader reconcile and the wake dispatch heal the
  row after the lease ends.
- **Step position (PR 1).** `context.rag` is written after `context.build`, not "inside" it
  as `docs/PIPELINE_SPEC.md:110` says today; the doc is updated.
- **Contract fields (PR 1).** `default_head_sha` is the index target; the response adds
  `error_code` for `failed` and `{error}` for a failed gate.
- **`old_revision_unknown` (PR 2; decided by the coordinator, 23.10).** A broken-reference candidate without
  `diff_old_sha` is dropped with this existing reason (M7); it joins the `dropped` keys of
  PIPELINE `:173`.
- **Extractor (PR 2; decided by the user, 24.10).** Scope touches the #130 extractor: Task 1b
  changes the Python name reads of `tree_sitter_facts.py` and bumps it to `facts/v2`.

## Risks

- `0038` is also taken by the branch of #169/#220: whoever rebases second renumbers.
- **Resolved: `context.rag` and the UI.** The UI accepts any tool name: `tool: z.string()`
  in dmc-268-ui-t6 `src/entities/run/model/schemas.ts:84,107` (`origin/main` `5471899`, per
  the coordinator; not visible from this repository). An unknown tool is simply not mapped
  to a stage by `TOOL_STAGE` (`src/entities/run/lib/runProgress.ts:31-42`). So `context.rag`
  breaks nothing. Follow-up: a one-line ui change mapping `context.rag` to CONTEXT, named
  in the PR 1 body; not a blocker.
- The parser CPU limit of 5 s per attempt
  (`app/modules/reviews/application/ast_symbols.py:18`) is shared with L4: on large PRs
  RAG may get `ast_timeout`.
- PR 2 does not work in production until v5; the `used` path is covered only by tests.
- Edges that exist only at the PR revision are not in the graph (per the ADR); a test pins
  `found = 0`.

## Open questions

- **Open, pending for the user:** the v4 owner for the #130 remainder. PR 3 stays blocked
  on it.
- **Open, decided in PR 2:** the shape of `context.rag` after a caught build failure (M8).
- Resolved (decided by the coordinator, 23.10): PR 1 records `context.rag` after the build on both paths and before the
  post-build skip ("Trace `context.rag`"); the CHECK `ck_code_index_states_review_full_sha_at`
  (M1); the M5 condition; `old_revision_unknown` as a listed deviation.

## Verification

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy . --exclude '^docs/reports/'   # CI runs `uv run mypy .`; the exclude skips the lab-local ignored reports
uv run pytest
TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_144 \
TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:5672/dmc_144 \
  uv run pytest -m integration -rs tests/test_code_index_*_postgres.py tests/test_rag_*.py \
  tests/test_app_reinstall_schema_postgres.py tests/test_worker_queue_postgres.py \
  tests/test_initial_migration.py   # 0 skips
```

- `uv run pytest` locally: 7 failures of `test_deploy_monitoring` come from the
  `~/.local/bin/sh` wrapper; run without it in `PATH`.
- `tests/test_alembic_heads.py` carries no `integration` marker and runs in the plain gate.
- Migration (PR 1): `uv run alembic heads` shows one head; upgrade → downgrade → upgrade on
  a clean database.
- Manual e2e after PR 1 (local compose, dev App 172685946, `axyi/dmc268-t6-sandbox-dev`):
  1. RAG on, index `ready` → a PR in the sandbox → `context.rag` `not_ready` /
     `prompt_version`, `last_used_at` moved;
  2. `last_used_at` − 31 days by SQL → a review → the TTL task does not purge the index;
  3. a TTL purge → a review → a new generation, the marker, the index `ready` again;
  4. RAG off → no `context.rag` step, no index access.
- PR 2: e2e cannot show `used` (the capability set is empty); tests cover it; end-to-end in
  PR 3.
