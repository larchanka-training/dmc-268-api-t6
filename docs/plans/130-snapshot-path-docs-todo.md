# Task list: documentation of the snapshot review path (#130, #168 part 4, PR G)

The design is in [130-snapshot-path-docs-plan.md](130-snapshot-path-docs-plan.md); this list adds no scope.

- Each task is one Conventional commit whose subject ends with `(#130)`. Part-4 tasks add `Refs #168` to the
  body.
- Re-read every anchor before editing; G2 line numbers are from `f8b09818`.
- Docs only. Do not change code, except G2-8 (`.agents` rule text) and G2-9 (one docstring outside eval `_REVIEW_CODE`). Never read any `.env`.
- Gates for every task:
  - `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy . --exclude '^docs/reports/'`.
  - Once per PR: the full `uv run pytest` with an isolated DB and vhost.
- #332 merged; G1 merged as #343; G2 opens on main.

## G1

- **G1-0** `docs(plans): plan the snapshot path docs (#130)`
  - Files: these two files, staged with `git add -f`.
  - Done when only these two files are committed.
- **G1-1** `docs(erd): show the policy snapshot, manifest and run pin tables (#130)`
  - Items: E1, E2.
  - Files: `docs/erd/erd.mmd`, `docs/erd/erd.svg`, `docs/BACKEND_ARCHITECTURE.md`.
  - Regenerate the svg with `npx -y @mermaid-js/mermaid-cli@12.0.0 -i docs/erd/erd.mmd -o docs/erd/erd.svg -b transparent`.
    This needs network; ask before running it.
  - Done when:
    - the svg contains RUN_POLICY_SNAPSHOTS, RUN_POLICY_DOCUMENTS, CONTEXT_MANIFESTS and policy_key;
    - every new column exists in migrations 0029, 0030, 0038, 0039 or 0040.
- **G1-2** `docs(review): catalog review.system.v4 and its envelope (#130)`
  - Items: E3, E4.
  - Files: `review/README.md`, `docs/RULES_MD_TEST_REPORT.md`.
  - Done when no "v4 activation remains in F2" or "ждут v4" text is left without R15.
- **G1-3** `docs(api): describe run actions after the snapshot switch-on (#130)`
  - Item: E5.
  - Done when:
    - `uv run pytest tests/test_openapi_contract.py` is green;
    - `npx --yes @redocly/cli@2.57.0 lint contracts/openapi.yaml` is green;
    - the backticked tool set in both descriptions is unchanged.
- **G1-4** `docs(design): describe both review paths and the parser limits (#130)`
  - Items: S1, S2, S3, S5, and C1's SD :1500 line.
  - Done when SD:891 names base for both paths and the legacy and v2 keys.
- **G1-5** `docs(specs): record the conventions key, parser, retries and reserves as built (#130)`
  - Items: C1, C2, C3, C6, C7.
  - Done when every number cites its code anchor; deep is noted as #331.
- **G1-6** `docs(rules): commit messages, the v4 defaults deviation and pin ownership (#130)`
  - Items: R1, R2, R4.

## G2

Before G2-1, the coordinator posts the §5 lines in #339, #340 and #334 (#342 FYI).

- **G2-0** `docs(plans): refresh the G2 plan on main (#130)`
  - Done when only the two plan files are in the commit.
- **G2-1** `docs(pipeline): verifier trace order, prompt override and pr_base_changed (#130)`
  - Items: P1, P2, P4, P5, P7.
  - Done when:
    - the order of both tables matches `run_repository.py:866-985`;
    - no "цель #107" remains on `llm.verify`;
    - no text says GitLab yields `pr_base_changed`.
- **G2-2** `docs(pipeline): no_changes, reserves, verifier codes and the silent legacy skip (#130)`
  - Body: `Refs #168`.
  - Items: P10-P13.
  - Done when:
    - PS :648 equals `check_runs.py:37`;
    - `grep -rn execute_review_legacy_run_without_snapshot docs/*.md` is empty.
- **G2-3** `docs(specs): CVS §8.3 tables that exist and the attribution allowlist (#130)`
  - Items: C8, C10.
- **G2-4** `docs(rules): RFS §7.2 after F2, the §5.3 counter and policy_path_invalid in §9 (#130)`
  - Body: `Refs #168`.
  - Items: R3, R6, R7, T5.
  - Done when:
    - §9 has a `policy_path_invalid` row;
    - neither «этим изменением не выполняется» nor «передана в #168» is left.
- **G2-5** `docs(design): SD §11 and the reviews model row list the existing tables (#130)`
  - Body: `Refs #168`.
  - Items: S4, B1.
  - Done when BA :448 names all 13 classes.
- **G2-6** `docs(test-plan): map the #130 and #168 tests and the v4 state (#130)`
  - Body: `Refs #168`.
  - Items: T1-T4.
  - Done when:
    - every mapped file exists under `tests/`;
    - the FakeRedis sentence names both in-test classes.
- **G2-7** `docs(cicd): REDIS_URL at the workers, rollback row 0040 and the F1 floor (#130)`
  - Body: `Refs #168`.
  - Items: O1-O3.
  - Done when the 0040 row sits above 0039 in `alembic history` order.
- **G2-8** `docs(agents): name the merged test-file layout in backend.md §5 (#130)`
  - Item: A1.
  - Done when both lines agree and AGENTS.md is untouched.
- **G2-9** `docs(reviews): the parser worker gets a minimal environment (#130)`
  - Item: X2.
  - Done when no `_REVIEW_CODE` file is in the diff and `uv run ruff format --check .` is green.
