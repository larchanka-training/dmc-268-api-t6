# Tasks: #144 part 3a (`feat/144-rag-retrieved-section`, stacked on #328)

Design: [144-part3a-plan.md](144-part3a-plan.md). One task → one commit, test first. Line numbers are
at `a9269bbd`.

## Task 0 — plan

- [x] Plan and todo committed.

**Commit:** `docs(plans): plan the retrieved rag section of the snapshot prompt (#144)`

## Task 1 — RAG text invariant and snapshot ids (D2, D3)

- Files: `app/modules/reviews/application/review_context.py` (slot fill `:660-700`,
  `_retrieved_block` `:268-281`, `snapshot_block` `:133-147` or a sibling),
  `app/modules/reviews/application/execute_review.py` (`:262-286`).
- Tests first: a place whose text lines differ from its range lines is dropped with the omission and
  the rest of the slot is kept; a snapshot-path RAG id for a 200-character path is ≤ 128 characters and
  has the `RAG:{rev12}:{digest16}` shape, LEFT and RIGHT blocks of one path differ, and the payload /
  `context.build` trace keep the stage id (mirror F1's L4 test in `tests/test_review_context_blocks.py`).
- Done when:
  - [x] invariant drop + omission tested;
  - [x] bounded snapshot RAG id tested, stage id unchanged.

**Commit:** `feat(reviews): bound the snapshot ids and text of rag blocks (#144)`

## Task 2 — the retrieved section (D1)

- Files: `prompt_builder.py` (`ReviewContext` `:170-190`: a flag for the section, **suggested**
  `retrieved_section: bool = False`; `render_input` `:246-280`; a render next to
  `render_context_excerpts` `:670-707`), `execute_review.py` (set the flag from
  `has_retrieved_section`). `RetrievedBlock` must satisfy the excerpt render and manifest (e.g.
  `truncated` False) without mixing types in `context_blocks`.
- Tests first: flag + blocks → section after `<context_excerpts>` with one `<excerpt level="RAG" …>`
  per block; flag + no blocks → empty section; no flag → no section and legacy/v4 bytes unchanged;
  fix the `RETRIEVED` fixture (`tests/test_review_context_blocks.py:309-318`: two text lines for
  ranges 3–4, a matching id) and flip the "not in prompt" assertion (`:445`).
- Done when:
  - [x] three render cases tested; legacy goldens byte-identical.

**Commit:** `feat(reviews): render the retrieved section of the snapshot prompt (#144)`

## Task 3 — fit (D4)

- Files: `prompt_budget.py` (`fit_review_context` `:435-500`, `_drop_context_blocks` `:502-530`,
  `_EXCERPT_DROP_ORDER` `:499`), `review_context.py` (the place→definitions relation on
  `RetrievedBlock`, **suggested** a group key or the definition ids), `reflection_pass.py`
  (`_fit_verifier_code` `:237`).
- Tests first: L3 and L2 go before RAG, RAG before L4; a place leaves with its definitions; a
  definition shared with a kept place stays; a context with only `retrieved` is fitted; files are
  trimmed only after every excerpt is gone; the verifier fit follows the same order.
- Done when:
  - [x] drop order, groups and shared definitions tested for review and verifier fits.

**Commit:** `feat(reviews): fit retrieved blocks between L2 and L4 (#144)`

## Task 4 — manifest (D5)

- Files: `prompt_builder.py` (`build_manifest` `:368-536`).
- Tests first: a RAG block becomes a `ManifestBlock` with role `retrieved_excerpt`, kind
  `source_code`, side, revision, `shown_ranges`, tokens of the rendered excerpt by the pinned
  counter, `repository_id`, `selection_reason`, `index_commit_sha`, `edge_kind`; `tokens_by_role`
  of the stored row has `retrieved_excerpt` and its share of `input_tokens`
  (`tests/test_context_manifests_postgres.py`).
- Done when:
  - [x] manifest fields and the stored `tokens_by_role` tested.

**Commit:** `feat(reviews): record retrieved blocks in the call manifest (#144)`

## Task 5 — verifier re-shows cited RAG blocks (D6)

- Files: `reflection_pass.py` (`_cited_excerpts` `:213`, `_verifier_code_context` `:183`, manifest
  build `:616-646`).
- Tests first: a candidate citing a RAG block → the verifier prompt shows it and its manifest lists
  it; uncited RAG blocks are in neither; the verifier guard passes.
- Done when:
  - [x] cited / uncited RAG blocks tested in the verifier prompt and manifest.

**Commit:** `feat(reviews): show the verifier the rag blocks its candidates cite (#144)`

## Task 6 — guard (D7)

- Files: `prompt_budget.py` (`:275-431`, the non-LEFT branch `:374-384`), `review_output.py`
  (`:280-296`), `reflection_pass.py` (`:849-860`); fixtures in `tests/test_gateway_review_model.py`
  (`_snapshot()` repository id vs `REPOSITORY_ID`) and `tests/test_execute_review.py:2805`.
- Tests first: RIGHT and side-less code blocks with a revision fail when `head_sha` is missing or
  differs (guard, attribution, verifier); a code block of another repository fails with
  `policy_context_violation`; a `retrieved_excerpt` without `repository_id` fails; a foreign path is
  still refused.
- Done when:
  - [x] negative tests for the three checks; full suite green (live change).

**Commit:** `fix(reviews): refuse code blocks of another revision or repository (#144)`

## Task 7 — end to end and checkpoint (D8)

- Files: `tests/test_snapshot_path_e2e_postgres.py` (or a sibling), test helpers only.
- Tests first: with `RETRIEVED_PROMPT_VERSIONS` patched to v4 and a seeded ready index, a v4 run shows
  the section, its manifest row has `retrieved_excerpt` in `tokens_by_role`, and `context.rag` is
  `used`; retry after a newer publication uses the new `index_commit_sha`; the verifier of the
  accepted attempt shows the same RAG blocks and `index_commit_sha`.
- Done when:
  - [ ] e2e and checkpoint tests green with 0 skipped.

**Commit:** `test(reviews): pin the retrieved section through the worker composition (#144)`

## Task 8 — docs

- Files: `docs/CONTEXT_AND_VERIFICATION_SPEC.md` (§3.5, §5.1, §5.2, §7.4), `docs/adr/0001-graph-rag-v1.md`
  §9 (as built, without the v5 text), `docs/PIPELINE_SPEC.md` (`context.manifest` roles).
- Done when:
  - [ ] canon matches the code; no v5 wording.

**Commit:** `docs: describe the retrieved section, its fit and its manifest (#144)`
