# Tasks: #144 part 3b (`feat/144-review-prompt-v5`, on #332 F2 + #330)

Design: [144-part3b-plan.md](144-part3b-plan.md). One task → one commit, test first.

## Task 0 — plan

- [x] Plan and todo committed.

**Commit:** `docs(plans): plan review.system.v5 with the retrieved section (#144)`

## Task 1 — v5 and its registration, atomically (E1, E2, E3)

- Files: `review/prompts/review.system.v5.md` (copy of v4 + E1 additions),
  `app/modules/reviews/application/policy_snapshot.py` (`PINNED_PROMPT_CHECKSUMS`),
  `app/modules/reviews/application/review_path.py`, `app/modules/reviews/application/rag_index.py`,
  `app/modules/reviews/infrastructure/llm/answers.py` (docstrings), `tests/test_review_prompt_v5.py`
  (new), the golden envelope fixture, and the tests that flip: `tests/test_prompt_seeding.py`,
  `tests/test_review_prompt_pins_postgres.py`, `tests/test_run_prompt_pins_postgres.py` (names/comments),
  `tests/test_review_path.py`, `tests/test_rag_readiness.py`, `tests/test_review_context_blocks.py`
  (the "empty set" assertion keeps v4 as "no section").
- Tests first: `test_review_prompt_v5.py` red without the file; then the flips.
- Done when:
  - [x] v5 file, pin, both version sets; suite green at this commit;
  - [x] measured token delta v5 − v4 (counter/v2) in the report.

**Commit:** `feat(prompts): add review.system.v5 with the retrieved section (#144)`

## Task 2 — end to end on a real v5 binding (E4)

- Files: `tests/test_snapshot_path_e2e_postgres.py`, `tests/test_review_prompt_pins_postgres.py`
  (`_bind_v5` or a version-generic bind), `tests/test_review_context_blocks.py` (the v4-patched tests).
- Tests: no `RETRIEVED_PROMPT_VERSIONS` monkeypatch left; a v5 run shows the section, `context.rag` `used`,
  `retrieved_excerpt` in `tokens_by_role`; a v5 run with RAG off shows an empty section and no
  `context.rag`; v4 runs show no section.
- Done when:
  - [x] patches removed; PG files green with 0 skipped.

**Commit:** `test(reviews): run the retrieved section on a v5 binding (#144)`

## Task 3 — docs

- Files: `review/README.md`, `docs/adr/0001-graph-rag-v1.md` §9, `docs/CONTEXT_AND_VERIFICATION_SPEC.md`
  §3.5, `docs/PIPELINE_SPEC.md` (`context.rag`), `docs/SYSTEM_DESIGN.md` (RAG "until v5" sentences only).
- Done when:
  - [x] the canon names v5 as the version with the retrieved section; no other v3/v4 sweep (#130 G).

**Commit:** `docs: name review.system.v5 as the prompt with the retrieved section (#144)`
