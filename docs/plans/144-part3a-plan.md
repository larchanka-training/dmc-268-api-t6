# Implementation plan: retrieved RAG blocks in the snapshot-path prompt (#144 part 3a)

## Status and baseline

Approved by the tech lead on 2026-10-26. This is the English task brief for the executor of PR 3a.
It records decisions already taken and adds no scope. **Suggested** marks a name the executor may
change; **derived** marks something that follows from the cited code or a repository rule.

- Issue: https://github.com/larchanka-training/dmc-268-api-t6/issues/144 (AC 5, 11; part 3 of 3).
- Base: PR #328 (#130 F1, branch `feat/130-snapshot-path`) head `a9269bbd`, itself stacked on #324.
  This PR stacks on F1 and is retargeted to `main` after F1 merges. Line numbers below are at
  `a9269bbd`; re-check them before editing.
- Branch `feat/144-rag-retrieved-section`; worktree `projects/dmc-268-api-t6-144p3`; test DB and
  vhost `dmc_144p3`.
- Earlier parts: `docs/plans/144-plan.md` (PR 1 and 2, merged as #254). ADR
  `docs/adr/0001-graph-rag-v1.md` §8–§9 and CVS §3.5/§5.1/§5.2/§7.4 are the canon.

## Context

F1 added the snapshot review path: an ordered excerpt slot in `ReviewContext`, the L2–L4 render, a
single fit, a stored manifest per call (`context_manifests`, the `context.manifest` action with
`tokens_by_role`) and the guard after the fit. It left the RAG slot to #144:

- `ReviewContext.retrieved` exists (`prompt_builder.py:185-187`); `execute_review.py:262-286` fills it
  only when `has_retrieved_section(...)` is true, and `RETRIEVED_PROMPT_VERSIONS` is empty
  (`rag_index.py:15`). Nothing renders, fits, manifests or re-shows `retrieved`.
- The excerpt render (`prompt_builder.py:670-707`) zips text lines against range line numbers
  strictly; a mismatch raises a terminal `PolicyError(CONTEXT_VIOLATION)`.
- The fit drops whole excerpt blocks in `_EXCERPT_DROP_ORDER = ("L3", "L2", "L4")`
  (`prompt_budget.py:499-530`); `"RAG"` is not in the tuple (`.index` raises), and the gates at
  `:451`/`:455` look only at `context_blocks`.
- The manifest excerpt loop (`prompt_builder.py:509-528`) counts tokens with the pinned counter over
  the rendered excerpt. `ManifestBlock` has `tokens`, `repository_id`, `truncated` and, for RAG,
  `selection_reason`, `index_commit_sha`, `edge_kind` (all `None` by default).
- The verifier manifest is `build_manifest(replace(context, changed_files=…, context_blocks=shown))`
  (`reflection_pass.py:616`): it keeps the whole `retrieved` tuple, which the verifier never sees.
  `_cited_excerpts` (`:213`) reads `context_blocks` only.
- The guard (`prompt_budget.py:275-431`) is fail-open on the non-LEFT branch when `head_sha` is None
  (`:379`) and checks no `repository_id`. The same fail-open is in `review_output.py:291` and
  `reflection_pass.py:859`.
- The snapshot path maps L4 ids through `snapshot_block` (`review_context.py:133-147`):
  `L4:{revision[:12]}:{sha256(stage_id)[:16]}`. The RAG stage id `RAG:{path}:{ranges}`
  (`review_context.py:268-281`) embeds the path and can pass the 128-character `EvidenceRef.block_id`
  limit (`review_output.py:111`).

## Decisions

**D1. A typed retrieved section.** The snapshot prompt gets its own untrusted section for RAG blocks
(ADR §9 "Инъекции"), rendered after `<context_excerpts>` with the same `<excerpt …>` element
(block_id, level `RAG`, path, revision, side, representation). Whether the section is shown follows a
flag on `ReviewContext` set from the bound prompt version (`has_retrieved_section`), not from a
non-empty tuple: with the flag and no blocks the section renders empty, so both A/B arms (ADR §12) run
one version. Container name **suggested**: `retrieved_excerpts`.

**D2. RAG text invariant.** A RAG block's text is its range lines one to one (`rag_context.py:893`,
`:932` build it so). A place that would break the invariant is dropped when the slot is filled, with
an omission (**suggested** reason `rag_candidate_missing`), and never reaches the render, where it
would fail the run.

**D3. Snapshot RAG id.** On the snapshot path a RAG block is named
`RAG:{revision[:12]}:{sha256(stage_id)[:16]}`, mapped at the same line as `snapshot_block` in
`execute_review.py:268` (**derived** from F1's L4 rule). The revision prefix carries the side (LEFT is
`diff_old_sha`, RIGHT is `head_sha`). The payload and the `context.build` trace keep the stage id.

**D4. Fit order and groups.** Drop order `("L3", "L2", "RAG", "L4")`, the reverse of ADR §9 :507
(L1 → L4 → RAG → L2 → L3; RAG displaces only L2 and L3). RAG blocks go by place group: a place goes with
the LEFT definitions it read (CVS §3.5, a broken-reference place enters with its definitions or not at
all). A definition shared by two places is emitted once (`review_context.py:672-689`), so it is kept
while any kept place lists it. Within RAG, drop from the last admitted place (lowest slot priority).
The gates at `prompt_budget.py:451`/`:455` count `retrieved`; `_fit_verifier_code` follows the same
rule.

**D5. Manifest.** RAG blocks go through the same manifest loop: role `retrieved_excerpt`, kind
`source_code`, side, revision, `shown_ranges = source.ranges` (a RAG block is only ever dropped
whole), `tokens = counter.count(rendered excerpt)` (never `RetrievedBlock.tokens`, which uses the
stage's arithmetic), `repository_id = source.repository_id`, `selection_reason = source.reason`,
`index_commit_sha`, `edge_kind`. `tokens_by_role` then carries `retrieved_excerpt` without a contract
change (AC11).

**D6. Verifier.** The verifier's `retrieved` holds only the RAG blocks its candidates cite;
`_cited_excerpts` counts them and the verifier prompt shows them (ADR §9 "Manifest verifier повторно
показывает те же блоки", CVS §5.2). No RAG block is listed in a manifest whose prompt does not show it.

**D7. Guard (live, not dormant).** For code roles (DIFF, SOURCE_EXCERPT, AST_EXCERPT,
RETRIEVED_EXCERPT, or kind `source_code`) with a revision, the non-LEFT branch fails when `head_sha` is
missing or differs (`prompt_budget.py:379`); the same rule replaces the fail-open in
`review_output.py:291` and `reflection_pass.py:859`. `repository_id`: when a snapshot is present and a
code block carries one, it must equal `snapshot.repository_id`; `RETRIEVED_EXCERPT` must carry one.
This changes the live (legacy and snapshot) guard: the full suite must pass. Orientation found no
caller with a revisioned code block and no `head_sha` (eval_live and the bootstrap CLI copy the DIFF
revision from a missing `head_sha`, so the block has none).

**D8. Checkpoint (tech lead, V2).** The candidate checkpoint is the stored manifest of F1. Tests show
that a retry before the accepted answer re-runs retrieval (a newer index publication → a new
`index_commit_sha` in that attempt's manifest), and that the verifier of the accepted attempt shows the
same RAG blocks with the same `index_commit_sha`.

## Out of scope

`review.system.v5`, its `answers.py`/`SNAPSHOT_REVIEW_PROMPT_VERSIONS` entry, pins, golden and the
`RETRIEVED_PROMPT_VERSIONS` entry are PR 3b (stacked on F2). No migration. No change to the legacy
prompt bytes (`tests/fixtures/prompt_builder/sample.user.txt`, `tests/fixtures/prompt_budget/*.user.txt`
stay identical). F1 files are edited only where a task names them; F1's PR #328 body lists its
coordination.

## Verification

- Gates: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy . --exclude '^docs/reports/'`,
  `uv run pytest`; integration with `TEST_DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/dmc_144p3`
  and `TEST_RABBITMQ_URL=amqp://app:app@127.0.0.1:5672/dmc_144p3`, 0 skipped.
- While dormant, a test monkeypatches `RETRIEVED_PROMPT_VERSIONS` to `{("review.system", "v4")}`
  (as `tests/test_review_context_blocks.py:426-445` does) to drive the RAG path end to end.
- Full suite before the PR (D7 changes live behaviour).
