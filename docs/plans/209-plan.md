# Implementation Plan: installation-token cache wiring and local fallback (#209)

## Overview and approval boundary

Protect the real review-worker and webhook-worker composition against silently selecting
memory when Redis is configured or constructing Redis when its URL is absent. Make a clean
Redis miss fall back to a still-valid process-local token, avoiding repeated GitHub issuance
when Redis reads work but writes fail. The user approved this saved plan on 2026-10-24;
implementation and mutation experiments may proceed through its sequential review gates.

Spec: https://github.com/larchanka-training/dmc-268-api-t6/issues/209.
Base inspected on 2026-10-24: `22c621abc80abdd1b3daaa95bde73b6aaf81c32e` (main;
managed checkout has detached HEAD). Initial `git status --short` was empty.

## Baseline evidence before implementation

- `app/worker.py:626` builds the real GitHub adapters and passes its Redis argument to
  `installation_token_cache`; `_run_worker:915-918`, reached through `run_worker`, only
  constructs Redis with `settings.redis_url`, then passes it to `github_adapters:935`.
- `app/webhook_worker.py:173-196` reads config, constructs optional Redis and builds the
  provider inside `run_forever`; its first `compose_worker` call occurs after these steps.
- `RedisInstallationAccessTokenCache.get:67-68` returns `None` on a clean Redis miss.
  The exception path already uses `_local.get`; `set` already records the token locally
  before attempting Redis. Local expiration already enforces the 60-second safety skew.
- Existing tests exercise cache/factory behavior and config parsing but do not reach the
  relevant production composition with a configured HTTP client.
- Baseline command from the issue completed: `uv run pytest
  tests/test_redis_installation_token_cache.py tests/test_github_installation_adapters.py
  tests/test_worker.py tests/test_webhook_worker_notifications.py` — **50 passed, no skips,
  8.22 seconds** (Python 3.13.4). The earlier issue's 148-test count is historical.
  Sandbox blocked the default uv cache and worktree `.venv`; the approved escalated retry
  created `.venv`, installed 83 packages and ran successfully. No source mutation was made.

## Architecture decisions

1. Add `tests/test_worker_token_cache_wiring.py`; keep `app/worker.py`,
   `app/webhook_worker.py` and `tests/test_worker.py` unchanged. No provider-factory
   extraction is needed: existing startup boundaries support deterministic fakes.
2. Review-worker harness invokes **actual `run_worker` and actual `github_adapters`** with
   configured App credentials and a real in-process httpx client. Fake engine/session
   construction, AMQP context and index consumer prevent external connections. A forwarding
   provider-constructor spy may capture constructor arguments and delegate to the real
   provider; it must not replace cache selection. Stop with a private sentinel at
   `compose_worker_process`, after the real adapters/provider/cache exist. Use a fake
   `Redis.from_url` that records its argument and returns a distinct closeable fake Redis.
3. Webhook harness invokes **actual `run_forever`**, using isolated environment config and
   fake resources/publisher. Capture the real token provider at `compose_worker`, then raise
   a sentinel before listener/sweep tasks. Fake `Redis.from_url` as above. This avoids DB,
   broker and background-loop dependencies without bypassing provider/cache construction.
4. Parameterize both entrypoints for configured URL, absent URL and empty environment URL.
   Assert one exact URL construction and Redis cache backed by that returned Redis object
   when configured; assert zero Redis constructions and the memory cache otherwise.
   Assert cleanup of created fake Redis/resources at the same boundary. Do not patch
   `installation_token_cache` or `github_adapters`: doing so would hide the regressions.
5. Extend `tests/test_redis_installation_token_cache.py` with independent read/write failure
   control on its fake and a mutable clock where needed. Test the real provider through
   `httpx.MockTransport`; three sequential calls for one installation must produce exactly
   one HTTP token issuance when every Redis read misses and every write raises. Keep
   separate cold-miss and expired-local cases; local expiry at the safety-skew boundary
   must require a fresh issuance. A Redis hit continues to take precedence over local data.
6. Implement only the clean-miss branch change: return `await self._local.get(id)`.
   Reuse existing local validation and existing error sanitization. No dependency, DB,
   HTTP contract, Postman workspace, configuration or CI change is expected.
7. New assertions report counts, cache types, keys, lifetimes and precomputed booleans.
   Never place token values in assertion expressions/messages, parametrization IDs or
   object reprs used by assertion diffs. For comparisons/secrecy, compute a boolean first
   and assert that boolean with a generic explanation. A failed Redis write should include
   a fake token in its exception text; assert via booleans that logs and recorded exception
   metadata omit it, and assert no traceback is attached. Retain existing secrecy coverage.

## Task order and checkpoints

1. Add the startup wiring regression tests and prove all four mutants are killed.
   The production code already satisfies the wiring contract, so demonstrate RED via
   one-at-a-time controlled mutations, followed by the original code passing.
2. Add provider-level miss/write-failure regression tests first (observe RED), add cold
   and expiry controls, then implement the minimal local-fallback fix (observe GREEN).
3. Run secrecy checks, exact mutation verification, complete gates and independent review;
   update the task list with reproducible evidence. Publish only after the agent-loop
   approval boundary and review requirements have been satisfied.

Dependency graph: base/overlap check -> wiring harness -> mutation proof -> provider
regressions -> minimal cache fix -> full verification -> review -> publishing.
Detailed acceptance criteria and commands are in `209-todo.md`.

Checkpoint after tasks 1-2: focused suite green without service configuration; each mutant
has an observed failure, and restoring only the fallback behavior produces its own RED.
Checkpoint after task 3: all required gates green, new tests executed without skips,
review has zero Standards/Spec findings, publication evidence recorded.

## Mutation verification design

Run each mutation independently against the new test nodes, using a fresh Python process
and a disposable temporary source-tree copy or an in-memory import-source override. Keep
owned tracked entrypoint files in this checkout unchanged; preserve the exact tested
substitution and failed test node in the execution evidence. Do not leave mutants active.
A nonzero exit counts only if the expected contract assertion fails, not for import,
fixture, type, or unrelated startup errors.

| ID | Exact semantic substitution | Required observed failure |
| --- | --- | --- |
| e1 | In `github_adapters`, pass `installation_token_cache(None)` | Configured review worker has memory cache instead of Redis-backed provider cache |
| e2 | In webhook `run_forever`, pass `installation_token_cache(None)` | Configured webhook worker has memory cache instead of Redis-backed provider cache |
| b3 | Webhook startup constructs `Redis.from_url` unconditionally with absent URL (use a fallback URL if needed) | Absent/empty-URL webhook case records a forbidden Redis construction |
| b4 | Review `_run_worker` constructs `Redis.from_url` unconditionally with absent URL | Absent/empty-URL review case records a forbidden Redis construction |
| fallback revert | Restore clean-miss `return None` after implementing the fix | Three-request provider scenario issues three tokens instead of one |

The recording Redis factory must accept the mutant's absent/fallback argument and return
its fake, so b3/b4 fail the construction-count assertion rather than crashing redis-py.
Run the untouched focused suite after every experiment and at the end. A development
report should include the five mutant outcomes and the clean rerun, never token strings.

## Open PR overlap and coordination

Read-only `gh pr list --state open --json number,url,files` on 2026-10-24 found:

| Open PR | Files relevant to this work | Planned action |
| --- | --- | --- |
| [#220](https://github.com/larchanka-training/dmc-268-api-t6/pull/220) | `app/worker.py`, `app/webhook_worker.py` | Read only; no refactor |
| [#227](https://github.com/larchanka-training/dmc-268-api-t6/pull/227) | `app/worker.py` | Read only |
| [#230](https://github.com/larchanka-training/dmc-268-api-t6/pull/230) | `app/worker.py`, `tests/test_worker.py` | New focused test file instead |
| [#254](https://github.com/larchanka-training/dmc-268-api-t6/pull/254) | `app/worker.py` | Read only |

No open PR touches the planned cache source/test file, new wiring test file or plan files.
No external comments were posted. Recheck immediately before implementation and rebase
before requesting review. No coordination comment is required with this file selection.
If a source refactor becomes unavoidable, first present the reason and affected paths;
AGENTS.md requires a comment in every overlapping PR before editing, and sending those
comments requires explicit authorization. Draft for each affected PR:

> Working on #209: I need to adjust `<affected file>` at the installation-token provider
> composition boundary to make Redis/memory selection regression-testable. The change
> preserves the runtime contract and does not modify your feature logic. This overlaps
> your PR; I will rebase on main before review and resolve the composition conflict if
> your PR merges first. Proposed scope: `<exact helper/signature and call sites>`.

Replace the placeholders with concrete scope before requesting authorization or posting.

## Risks, exclusions and open decisions

- Startup tests must reach Redis construction and actual provider/cache construction before
  their sentinel. Type/identity assertions are intentional composition contracts; mutation
  proof guards against a fake harness that accidentally bypasses the code being tested.
- Ambient Redis/LLM/GitLab/heartbeat settings must be controlled, and every client/context
  closed, so tests remain deterministic and service-independent.
- Expired local data must never be resurrected. Use a controlled clock at and beyond the
  safety-skew boundary; Redis-hit and exceptional-read behavior remain covered.
- No cross-process cold-start lock, 401 invalidation, Redis timeout/circuit-breaker work,
  indexer cache migration, deployment or issue closing is included.
- Required CI checks are `Python lint / type / test`, `OpenAPI lint`, and `Webhook container
  smoke`. The Python job runs unrestricted `uv run pytest` with coverage options, and
  `testpaths = ["tests"]`; these new unmarked tests need no service URL and will not skip.
  CI already starts PostgreSQL/RabbitMQ for unrelated tests; Redis is unnecessary here.
- No unresolved product decision. User approved this saved plan on 2026-10-24; task-level
  review remains required before proceeding to each next implementation task.
