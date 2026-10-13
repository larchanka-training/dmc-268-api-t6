# Plan #115: AuthScope and installation token cache

## Goal and boundaries

For [#115](https://github.com/larchanka-training/dmc-268-api-t6/issues/115), update
the two relevant bullets in [SYSTEM_DESIGN](../SYSTEM_DESIGN.md) and the AuthScope
rule in [backend rules](../../.agents/rules/backend.md). The issue names §6.9;
the target bullets are in **§7.3**. This is the documentation part of
[#106](https://github.com/larchanka-training/dmc-268-api-t6/issues/106), item 3.
Runtime OpenAPI belongs to a separate task. Runtime behavior, dependencies,
migrations and test code are outside this change, as are neighboring documentation
work and the scope of #107. Check overlapping PR diffs and publish the required
scope notices before editing shared files.

## Evidence from the code

| Contract | Files and symbols |
| --- | --- |
| Production cache creation | [app/webhook_worker.py](../../app/webhook_worker.py) → `run_forever`; [app/worker.py](../../app/worker.py) → `github_adapters`, called by `run_worker` |
| Production worker composition | `github_adapters` creates one cache shared by its GitHub adapters only when the GitHub App is configured and an HTTP client is available; otherwise it returns `None` with a warning. Production uses `compose_worker_process` / `WorkerProcess`. |
| Third cache creation site | `review_worker` in `app/worker.py` is the obsolete prototype listed in SYSTEM_DESIGN §16 and called by [tests/test_worker.py](../../tests/test_worker.py). It creates a cache when no `vcs_provider` is injected and the GitHub App is configured. |
| Process-local cache, 60-second skew | [github_installation_tree_provider.py](../../app/modules/integrations/webhooks/infrastructure/github_installation_tree_provider.py) → `InMemoryInstallationAccessTokenCache.get`, `_INSTALLATION_TOKEN_SAFETY_SKEW_SECONDS` |
| Run/cancel and authorization helper | [run_repository.py](../../app/modules/reviews/infrastructure/run_repository.py) → `SqlAlchemyRunRepository`, `SqlAlchemyCancelRunUnitOfWork`, `authorized_run` |
| Rerun | [rerun_store.py](../../app/modules/reviews/infrastructure/rerun_store.py) → `SqlAlchemyRerunStore`, `SqlAlchemyRerunUnitOfWork`; scoped rerun shares the Run visibility predicate |
| PR list | [pull_request_queries.py](../../app/modules/reviews/infrastructure/pull_request_queries.py) → `SqlAlchemyPullRequestQueries` |
| Repository settings | [repository_settings.py](../../app/modules/repositories/infrastructure/repository_settings.py) → `SqlAlchemyRepositorySettingsStore`, `SqlAlchemyRepositorySettingsUnitOfWork` |
| Delegating factory | [reviews_api.py](../../app/bootstrap/reviews_api.py) → `ReviewsApiResources.run_repository`, delegates to the protected repository constructor |
| Explicit internal bypass | `app/worker.py` → `process_review_run`, `github_adapters`; [run_processing_unit_of_work.py](../../app/modules/reviews/infrastructure/run_processing_unit_of_work.py) → `SqlAlchemyRunProcessingUnitOfWork.repository` |

All seven adapter constructors reject `scope is None and not allow_unscoped` with
`ValueError`. The same fail-closed rule covers authorization helpers and factories:
`authorized_run` checks directly on invocation, and `ReviewsApiResources.run_repository`
enforces the rule by delegation. Missing scope never implies permission to bypass it.
Existing checks include [test_sqlalchemy_run_repository.py](../../tests/test_sqlalchemy_run_repository.py)
and [test_rerun_scope.py](../../tests/test_rerun_scope.py). A documentation-only change
needs no synthetic runtime tests or RED step; verify its claims against the code.

## Mirrors

According to [.agents/README.md](../../.agents/README.md), backend rules are per-repo;
no UI copy is required. Shared skills, agents, git-workflow and PR templates stay
unchanged. `.claude/skills` and `.claude/agents` are symlinks; `CLAUDE.md` imports
AGENTS. AGENTS mirrors backend §1, §2 and the transaction rule, but not the AuthScope
paragraph in §3. Do not edit AGENTS when the mirrored rules stay unchanged; if they
change, synchronize both sides of the SYNC marker.

## Sequence and verification

1. Update the two §7.3 bullets with matching adapter examples, the helper/factory
   rule and explicit `allow_unscoped=True` for trusted internal callers. Distinguish
   production cache composition from the prototype. Use relative file links and
   symbols without line numbers; preserve process-local semantics, the 60-second
   skew, Redis/#62 and scoped rerun's shared authorization predicate.
2. Align backend §3 and verify the mirror rationale. Checkpoint: both #115 acceptance
   criteria are supported by code and the diff stays within the agreed scope.
3. Check all `scope: AuthScope | None` signatures and cache creation sites with `rg`,
   resolve links and inspect the diff. Run all four uv gates listed in
   [115-todo.md](115-todo.md); record actual results, integration skips and warnings.

The tasks are sequential. Obtain plan approval before implementation, then run
independent Standards/Spec review and recheck the documentation against the current
main base. Publish using `What / Why / How to verify / Refs`, with Refs #115/#106,
no closing keywords and no Development-panel link. Rebase before requesting review
and again before merge. Current-head approval and resolved review threads are merge
requirements; tech-lead acceptance, issue closure, tracker #106 and board 12 updates
are subsequent Definition-of-Done steps, not implementation evidence.
