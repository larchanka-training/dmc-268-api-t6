# Plan: graph RAG v1 decision record and canon amendments (#140)

## Status and scope

The plan was approved by the sprint tech lead (axyi) on 2026-10-14. Specification:
https://github.com/larchanka-training/dmc-268-api-t6/issues/140, epic #139.
Checked against `origin/main` `362f758`. The deliverable is documentation only: no code, no
migration, no dependency. The ADR is approved separately by ndovnar on the PR head;
implementation (#141–#147, ui#102) stays blocked until then.

Inputs the ADR must not reopen: epic decisions D1–D4 (2026-10-12) and the accepted answers to
the tech-lead review F1–F6 (2026-10-13, accepted 2026-10-14 in
https://github.com/larchanka-training/dmc-268-api-t6/issues/139#issuecomment-6175812556).
That acceptance adds one requirement: deleting or renaming an exported symbol must be
analysed through the old and new revisions, not lost when an unconfirmed edge is dropped (R7).

Decided with the tech lead on 2026-10-14:

- ADR location: `docs/adr/0001-graph-rag-v1.md`, written in Russian like the canon.
- Index retention: 30 days without use (configurable), on top of immediate event purge.
- Owner module of index tables and indexer: new `app/modules/code_index`.
- Signatures are not stored; the index keeps pointers only (R2).

## Verified facts on main

- No ADR convention exists. Change maps already used in the canon: CVS §12
  «Существующее положение | Целевое изменение», RULES_FORMAT_SPEC §12 «Место | Требуемое изменение».
- SD header line 7 says `Р-1…Р-15`; Р-13 is amended in place, no new Р-number.
- Anchors linked from other docs must not change: SD `#9-сборщик-контекста-4-уровня`,
  `#граница-mvp-и-будущего-rag`, `#сборка-и-бюджет`; TEST_PLAN §2.3 heading (cites Р-13).
- `ContextProvider`, `BudgetAllocator` and `ManifestBlock` are target contracts, absent from
  code. `app/modules/reviews/application/process_run.py:243` discards the L1–L4 context
  result; the LLM receives only L1 (`app/modules/reviews/infrastructure/llm/models.py`).
  L0–L4 assembly with budget belongs to #130, prompt envelope, pre-send guard and manifest to
  #120 (PR #133). #144 and #146 therefore also depend on #130 and #120.
- AST v1 (#130, PR #136): `app/modules/reviews/application/ast_symbols.py`
  (`AstSymbol.references` are unresolved names), `ast_resolution.py` (`RESOLVER_VERSION =
  "resolver/v1"`, per-run caps 20 imported files / 10 symbols per file / 100 per run,
  `ImportResolution` values resolved/external/ambiguous/dynamic/excluded/unsupported/missing,
  `symbol_id = f"{revision[:12]}:{path}#{index}"` at :118-120), extractor
  `app/modules/reviews/infrastructure/tree_sitter_facts.py` (`QUERY_VERSION = "facts/v1"`,
  `profile_version()`), subprocess parser `subprocess_source_parser.py`.
- No tarball/archive client and no GitLab read client. GitHub compare exists only as
  `get_merge_base` (`github_vcs.py:234`, `per_page=1`). v1 is GitHub-only.
- Fencing precedent: `reserved_generation`/`applied_generation` (migration 0013,
  `app/modules/workspaces/application/link_github_installations.py`).
- Queues are RabbitMQ via aio-pika (`app/modules/reviews/infrastructure/amqp.py`); leader work
  runs in `WorkerProcess.leader_tick` under an advisory lock (`common/infrastructure/db/leader.py`).
- `usage_events.run_id` is NOT NULL (`app/modules/analytics/infrastructure/models.py:36`).
- `installation_repositories.removed` / `installation.deleted` only disable access
  (`repositories/application/sync_installation_repositories.py:167,190`); no per-repo data
  is deleted today.
- Prompts: `review/prompts/<key>.vN.md`, active `review.system.v2`; seeding refuses edits of an
  existing version (`app/bootstrap/seed_prompts.py`).
- 7-day blob cache: `cached_file_blobs`, `BLOB_CACHE_TTL = 7 days`.

## Architecture decisions

| # | Topic | Decision |
|---|-------|----------|
| R1 | Р-13, SD §9, CVS §1 | RAG v1 = per-repository symbol graph (definitions, imports, edges) with pointers: path, line range, blob sha, `indexed_commit_sha`. No embeddings, no code text. `RagContextProvider` returns the same `ContextPayload` as the deterministic provider. v2 (vectors) only after the #146 gate |
| R2 | Signatures vs pointers | Pointers only. By R6 a signature is re-extracted from the PR-revision blob, so a stored one never reaches the prompt; signature text is a code fragment and would fall under the 7-day blob limit; matches D2 and the curator's «мы не храним эти файлы» |
| R3 | SD §2, §10, §13 data | New data class «производные метаданные кода»: paths, qualified names, kinds, line ranges, blob/commit sha, edges; no bodies, literals, comments or docstrings. Key `repository_id`. Purged immediately on `installation_repositories.removed`, `installation.deleted`, toggle off. TTL 30 days since `last_used_at` (a review read or a successful refresh), configurable, purged by a leader job. The 7-day blob rule is unchanged; RAG text comes from the existing blob cache at the PR revision |
| R4 | BACKEND_ARCHITECTURE | Module `code_index` owns `code_index_states` (one row per repository), files, symbols, edges tables; `repository_id NOT NULL` on every row, FK `ON DELETE CASCADE` only as a safety net, no unscoped repository methods; DDL is #141. Indexer = consumer of RabbitMQ queue `index.repo` (exchange `code_index`) with its own prefetch. Ports: `GraphRetriever` in `reviews.application` (consumer owns the port, `.agents/rules/backend.md` §3 item 2), index purge port in `repositories.application`; implementations in `code_index.infrastructure`. #130 extractor and resolver are reused, not duplicated; where the shared code lives is #142's call |
| R5 | Revisions (CVS §2.1, §3.2, §5.2) | New §2.1 row `index_commit_sha`: selects candidates only, never a block revision. Text is read at `head_sha` (RIGHT) or `diff_old_sha` (LEFT). A manifest block with the index revision is a guard violation (`policy_context_violation`). A candidate absent at the PR revision → omission `rag_candidate_missing` |
| R6 | Edge confirmation (F5) | Every used edge is re-resolved by the #130 resolver at the PR side's revision; range and signature are re-extracted from that blob; default-branch coordinates are never carried over. Unconfirmed → omission `rag_edge_unconfirmed`. Reads and parsing count against the RAG slot |
| R7 | Deleted / renamed export (ndovnar 2026-10-14) | Importer I still imports M.X at `head_sha` but the resolver returns `missing` (X gone from M, or M gone/moved) → candidate `rag_broken_reference`, not an omission. Two blocks, each with one revision: I's import and use sites at `head_sha` (RIGHT, `exact_lines`) and X's definition at `diff_old_sha` (LEFT, `signature`). No cross-revision gluing (CVS §3.2), each block passes the per-block revision guard. If I no longer imports X at `head_sha`, the PR fixed it — no candidate |
| R8 | Edge kinds (F6) | `imports` (file → module), `defines` (file → symbol), `uses_imported` (file → module + exported name bound by a resolved explicit import). Not traversed: receiver calls, methods via values, barrel re-exports, package exports, aliases, dynamic imports, implicit Go cross-file links (they stay unsupported/ambiguous per #130). «Callers» in v1 = files with a confirmed edge to the changed symbol or module. Depth: one reverse hop beyond L4. Resolver extensions are separate work. Give three examples of context beyond L4: a caller of a changed function, a deleted export, importers of a changed TS interface |
| R9 | Symbol identity (F4) | Logical key `(repository_id, path, qualified_name, kind, disambiguator)`; `symbol_id` from PR #136 is never stored. Edge target = `(resolved module path, exported name)`, not a symbol row id, so rewriting the target file leaves no edge to an invalid row; deleting the target makes the edge unresolved, never pointing at another symbol. Changes that affect import resolution (file add/delete/rename, `__init__.py`, `index.ts`, `package.json`, `go.mod`, `tsconfig` paths) re-extract importers; above the limit → full. `facts`, `resolver` and profile versions are stored on the state row; a change → full |
| R10 | Fencing (F1) | Monotonic `generation` on `code_index_states`, following the `reserved/applied_generation` precedent. Purge and toggle-off increment it and delete rows in one transaction; enable increments it and schedules a full build. A task carries its generation; every write batch checks it inside its own transaction; on mismatch the task stops without writing and without retry. Publication = compare-and-set on `(generation, base indexed_commit_sha)`. Readers see a consistent snapshot: rows carry `valid_from`/`valid_to` publication numbers, readers filter by the published number, superseded rows are garbage-collected after publish. Network calls stay outside transactions |
| R11 | Incremental (F2) | Change set = VCS compare `indexed_commit_sha...target` on the default branch, not the last PR diff. Tasks of a repository coalesce: the target lives on the state row, the queue message only wakes `{repository_id, generation}`. Full when: no index; compare incomplete or truncated; target not a descendant (force push); default branch changed; more than 500 changed files or too many resolution-affecting files; extractor version changed; ignore in `.review/rules.md` changed. Publish only if generation and base `indexed_commit_sha` are unchanged since task start. Invariant: `indexed_commit_sha = C` ⇒ the whole permitted tree is indexed at C. Ordering comes from generation and CAS, not commit ancestry |
| R12 | Triggers (#143) | Enable → full. PR merged into the default branch → target = new default HEAD. Leader reconcile compares default HEAD for enabled repositories (interval configurable; covers direct pushes). Before a review, a lagging index gets an update enqueued; the review never waits. Toggle off → no index access and no `context.rag` step. Toggle on but index not ready → review without RAG, trace `rag: not_ready` with a reason. A lagging ready index is still used (candidates are confirmed at the PR revision); the lag goes to the trace |
| R13 | Limits (F3), starting values, env `CODE_INDEX_*` | downloaded 100 MiB; unpacked 1 GiB; archive entries 100 000; a file over 1 MiB is skipped (not an error); parsed files 20 000; symbols 500 000; edges 2 000 000; task deadline 15 min; storage 512 MiB per repository and 2 GiB per Workspace; concurrency 2 global and 1 per installation; incremental up to 500 changed files. Streaming tar.gz parse in memory, no disk, running total checked. Reject absolute paths, `..`, symlinks, hardlinks, devices, FIFOs. Exceeding a limit → status `failed`, `error_code = limit_exceeded:<limit>`, no retry and no automatic restart until the toggle is re-enabled; review runs without RAG. Retry only on transient errors (5xx, 429) with SD §7.1 delays. Statuses for #145: `empty`, `building`, `ready`, `failed` + `error_code`; `stale` is derived (ready, behind default HEAD) |
| R14 | Budget (SD «Сборка и бюджет», CVS §4.1) | Order L1 → L4 → RAG → L2 → L3. Ceiling 4 000 tokens; at most 20 blob reads, 10 candidate files and 3 sites per file per run. RAG takes what is left after L1 and L4 and displaces only L2/L3; L1 and L4 are never displaced. Priority inside the slot: broken reference > callers of a changed exported symbol > importers of a changed module |
| R15 | Role, manifest, ignore, injection | `source_role = retrieved_excerpt`, `source_kind = source_code` (RULES_FORMAT_SPEC §8.1 mapping). ManifestBlock: `selection_reason` ∈ {`rag_reverse_edge`, `rag_broken_reference`}; `source_ref` carries `index_commit_sha` and the edge kind as selection metadata. Ignore applies twice: at indexing time on HEAD and again with the run's pinned ignore to every candidate (RULES_FORMAT_SPEC §5, §8.4). The block is a typed untrusted section of the #120 envelope. New prompt version `review.system.v3` (Р-6); eval baseline re-captured without mixing runs (#82) |
| R16 | Spend accounting (D3, #145) | v1 indexing makes no LLM or embedding calls: no `usage_events` rows and no fake `run_id`. Operational counters (bytes, files, symbols, edges, duration, GitHub requests) live on the state row. RAG tokens in a review prompt are part of that run's `usage_events` and fall under `daily_budget_usd`. #147 decides how to record spend without a run before embeddings ship |
| R17 | PIPELINE_SPEC | §2: a `context.rag` row in both tables. Request: index status, `indexed_commit_sha`, lag. Response: `used` / `not_ready` + reason, candidates found / confirmed / dropped by reason, tokens. Written only when the toggle is on. §6: RAG never sets a run `error_code`; `rag_*` are coverage reasons like `ast_*` (last paragraph of §6) |
| R18 | TEST_PLAN, #146 gate | New section. Corpus ≥ 24 cases, each with a repository snapshot at the case revision: 10 defects visible only through a reverse edge (3 of them deleted/renamed exports), 6 control (L4 suffices), 4 depth-2 (expected misses in v1, groundwork for v2), 2 with an injection inside retrieved code, 2 without defects. A/B: same prompt version and model, arm A without RAG, arm B with RAG; offline replay with the frozen index snapshot; 3 runs per arm. Contribution metric: ΔRecall on the reverse-edge subset. Gate to open #147 (all must hold): ΔRecall ≥ +15 pp; on the whole corpus Precision drops ≤ 5 pp, Critical Recall not lower, Hallucination Rate up ≤ 2 pp, median tokens per run ≤ +30 %; zero followed injections. A decision gate, not a required CI check (§5 stays report-only). Gate fails → #147 closed as not planned |

## Work isolation and ownership

Branch `docs/140-graph-rag-adr` from `origin/main`, worktree `../dmc-268-api-t6-140`.
Open PRs touching the same files were notified on 2026-10-14: #148 and #124 (SD «Сборка и
бюджет»), #154 (PIPELINE_SPEC §6, TEST_PLAN §3), #155 (SD §15). Whoever merges second rebases.

## Ordered tasks

1. `docs/adr/0001-graph-rag-v1.md` (new, Russian): header table (status «Предложено», date,
   owner, issue, epic, approver ndovnar), context, decisions R1–R18 grouped by topic, consequences,
   change map «Место | Требуемое изменение», dependencies (#130, #120, GitHub-only v1), open
   points for v2 (#147).
2. Canon amendments (Russian, match the style of the touched section):
   - SYSTEM_DESIGN: Р-13 rewritten in place; Р-6 extended (index revision is never pinned,
     the prompt version is); §2 Backend row; §9 «Граница MVP и будущего RAG» and «Сборка и
     бюджет»; §10 index row and «не кэшируем» line; §13 «Данные клиента» and «Лимиты контекста».
   - CONTEXT_AND_VERIFICATION_SPEC: §1, §2.1, §3.1 `source_role`, §3.2, §3.4 `rag_*` reasons,
     §4.1, §5.1 ManifestBlock, §5.2, §12; version bump in the header.
   - RULES_FORMAT_SPEC: §5, §8.1 mapping, §8.4; version bump.
   - BACKEND_ARCHITECTURE: «Интерфейсы и инфраструктурные реализации» (fix the «без миграции»
     claim, name module, indexer process and retriever port); «Целевая модульная структура
     кода» (`code_index`).
   - PIPELINE_SPEC: §2 `context.rag` rows; §6 last paragraph.
   - TEST_PLAN: new gate section; §4 note on repository snapshots for RAG cases.
3. Commits: `docs(adr): add graph RAG v1 decision record (#140)` (ADR + this plan), then
   `docs: align canon with graph RAG v1 ADR (#140)`.

## Verification approach

- Every AC of #140 maps to a concrete passage (reviewer checklist).
- Anchors: changed docs rendered with `gh api markdown` (gfm, repo context); every
  `*.md#…` link in changed files and every incoming link from `docs/` resolves.
- grep: no unamended «RAG не входит в MVP», «не требует новой ORM-модели», full-repo-indexing
  exclusion left in the canon.
- Gates: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest`.
- Manual check of anchors on rendered GitHub after push (DoD).
