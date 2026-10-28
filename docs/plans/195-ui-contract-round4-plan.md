# UI #195 round 4: API contract counterpart

## Goal and state

Match the exact published UI #195 commit `46b2807492a52e100bcfb48052b1e640cbd50beb` for Bitbucket connection response slugs. Its shared `connectionFields` require 1–128 characters for `workspaceSlug` and `repositorySlug`, inherited by `bitbucketConnectResponse`, `bitbucketConnectionItem`, and nested `bitbucketConnectionList`. API #340 is Draft at `f60202bae2d31dda23e5d0c70f429ac6d3030bfe`; its `ConnectResponse` and `ConnectionItem` currently declare bare `str` fields. The API pin worktree already has uncommitted fixture/pin and token-pattern parity work. Preserve those edits.

## Task 1: Reproduce the three mirror failures

Regenerate `tests/fixtures/ui_zod_contracts.json` from a clean checkout of UI commit `46b2807`, and set `UI_CONTRACT_COMMIT` to that full SHA. Run `test_component_schemas_mirror_the_ui_zod_contract` for `bitbucketConnectResponse`, `bitbucketConnectionItem`, and `bitbucketConnectionList`; capture the three RED mismatches before editing DTOs. The mirror compares `minLength` and `maxLength` recursively, so the list case covers nested items. Verify fixture provenance and the generated schema names. Do not alter `DiscoveredRepository`: its UI schema remains separately unbounded.

## Task 2: Match response DTO and OpenAPI bounds

Set `Field(min_length=1, max_length=128)` for `workspace_slug` and `repository_slug` on both `ConnectResponse` and `ConnectionItem` in `app/modules/integrations/bitbucket/api/connections.py`. The list response inherits `ConnectionItem`. Add focused boundary evidence for 128 accepted and 129 rejected on both fields and both DTOs, plus OpenAPI `minLength: 1` / `maxLength: 128` assertions. The three mirror cases must turn GREEN. Keep the request models' existing bounds and the discovery response model unchanged.

## Task 3: Verify and publish a stacked API patch

Run focused contract/API tests, then `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, and `uv run pytest`. Review the final diff for fixture provenance, the token-pattern parity test, response bounds, and accidental unrelated changes. Publish a new Draft PR targeting the current #340 head rather than pushing to #340. The ownership coordination comment is already posted: https://github.com/larchanka-training/dmc-268-api-t6/pull/340#issuecomment-6476646581. Recheck #340's head and stacked PR mergeability before publishing; if #340 moved, rebase/update the stack and rerun affected gates. Attach the new PR and report its link to the coordinator. The final release gate still requires the integrated API #340 and authenticated staging/browser acceptance.

## Dependency and risk

Task 1 precedes Task 2; Task 2 precedes Task 3. The exact UI SHA is immutable for this pin. The API #340 head may move while this patch is prepared, and the older stacked #356 is dirty after #340's squash; use the isolated API pin worktree as the source for the new stack. The current API worktree has concurrent uncommitted contract and test edits; preserve them and attribute them in final review.
