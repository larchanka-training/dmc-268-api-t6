# Round 8: policy and context request-origin fences

## Result and scope

The policy/context stage added by merged #136 now uses the claimed Run's existing
attempt checkpoint for every provider read. The checkpoint checks the attempt
deadline, cancellation, and immutable installation origin through short database
sessions. The worker passes it into a fresh stage per attempt; policy and revision
sources receive it explicitly. Cached source locators and revision trees do not
supply authority.

Changed source files:

- `app/worker.py`
- `app/modules/reviews/infrastructure/github_policy_source.py`
- `app/modules/reviews/infrastructure/github_revision_source.py`
- `app/modules/reviews/application/review_context.py`
- `app/modules/reviews/application/review_context_stage.py`

Policy tree reads check immediately before token acquisition and before the actual
GET. Revision-tree reads check immediately after an awaited cache miss before token
acquisition, and again before GET. The initial pre-cache check and the top-level
`ReviewContextStage.begin/run` checks are **defense in depth**: the recorded tests
do not establish a separate zero-cache-access guarantee for these earlier checks.
Delegated blob, PR and comparison reads forward the required checkpoint to the
existing VCS adapter. Missing origin input produces `RunCancelled`.

Optional context enrichment re-raises `RunCancelled`, mandatory `PolicyError`, and
checkpoint `deadline_exceeded`. Ordinary optional failures remain omissions or
fallbacks. Policy errors, SHA-addressed reads, truncated-tree handling, byte limits,
ignore exclusions, parser limits and cache behavior retain their existing contracts.
No new dependency, schema, queue or public API is introduced. CPU parsing retains its
existing budgets and cleanup; no parser polling was added.

Atomic policy persistence is recorded separately in the [policy proof](113-review-round8-policy.md).
Existing context-payload fencing, independently committed paid usage and append-only
run-action audit remain. This correction changes no dependency or migration.

## Test-first evidence

The added tree/blob regressions first failed because cancellation, deadline and
mandatory policy errors were swallowed: **6 failed, 1 passed** (the optional source
failure control passed). The stage-level regressions first failed because cancellation
and deadline became optional fallbacks: **2 failed, 1 passed** (optional failure
control passed).

The real PostgreSQL composed-stage regression first failed on retirement during a
policy token response. It observed **4 HTTP requests where only the first token POST
was permitted**, including root and nested tree work after retirement. This was a
request-boundary failure, not a missing-constructor-argument assertion.

After implementation:

- Focused context, stage, policy source and revision source suites: **71 passed**.
- New PostgreSQL composed-stage suite: **21 passed, zero skipped** in 47.00 seconds.
- Three previously failing real worker/broker main-composition cases: **3 passed,
  13 deselected, zero skipped** in 15.21 seconds, preserving their original expected
  LLM/output codes and fixtures.
- Focused Ruff check and format check: clean (10 Python files).
- Focused strict mypy: clean (10 Python files).

The PostgreSQL matrix covers active control, deletion, and same-account fresh OAuth
rebind at seven boundaries: policy token, root tree before nested tree, rules blob
before AGENTS blob, PR token, comparison token, context-tree token, and changed-file
blob before imported-file retrieval. It asserts no subsequent request after retirement
and no replacement-installation credential request. Each HTTP handler independently
acquires installation, Repository and Run locks with `FOR UPDATE NOWAIT`, proving
provider I/O runs outside the database transaction. Active controls load the imported
source, so cancellation assertions cannot pass on a path that never attempts enrichment.

## Cache-await checkpoint correction

An awaited Redis tree-cache miss rechecks the Run immediately before token
acquisition. The initial pre-cache checkpoint is defense in depth and cannot
authorize work after the cache await returns. The post-cache-await and per-token/
per-request checks carry the request-boundary guarantees recorded below; no separate
zero-cache-call guarantee is claimed for pre-cache or top-level stage checks. The
correction added one checkpoint without changing cache hits, misses or complete/
incomplete tree handling.

The regression first failed in both unit variants: cancellation and deadline were
propagated only after the token recorder had received installation 17, while zero
token acquisitions were required. Real PostgreSQL with the production
`RedisTreeCache`, real Run lifecycle checkpoint and real GitHub token provider
confirmed **2 failed, 1 active control passed**. Repository availability was revoked
or the deadline expired during the awaited cache miss; the failure was an actual
access-token POST after that boundary, rather than a constructor/signature failure.

After the correction, the full revision-source suite has **4 passed**, retaining its
complete-tree cache-hit and incomplete-tree cache-miss controls. The full context
origin PostgreSQL suite has **24 passed, zero skipped** in 29.13 seconds, including
repository-unavailable and deadline cache-await boundaries with zero token/HTTP
requests. Focused Ruff check, format check and strict mypy are clean on the three
changed Python files. Combined gates on the corrected source are recorded in
the [final proof](113-review-round8-final.md); the interrupted preliminary full
run is excluded from passing-gate evidence.

## Separate #151 public composition proof

The counts above are historical round-eight executions, not current-head gate
results. The [#151 verification](151-verification.md) separately records the public
`compose_worker_process(...).handle_run.execute(...)` regression with real PostgreSQL
and the production context factory. Its active control reaches imported context and
publication; retirement during the base-policy root-tree response blocks subsequent
HTTP requests. Replacing only the checkpoint forwarded into the stage with a no-op
fails the request-count assertion; restored wiring passes. This adds composition
coverage without claiming an independent zero-cache-access guarantee.

## Reproduce

Configure the existing local PostgreSQL and RabbitMQ test environment through
`TEST_DATABASE_URL` and `TEST_RABBITMQ_URL`; no endpoint or credentials belong in this
proof.

```sh
uv run pytest tests/test_review_context.py tests/test_review_context_stage.py tests/test_github_policy_source.py tests/test_github_revision_source.py -q
uv run pytest tests/test_run_policy_context_origin_postgres.py -q
uv run pytest tests/test_worker_queue_postgres.py -k 'main_composition_reviews or main_composition_without_llm' -q
```

The [final proof](113-review-round8-final.md) records combined repository gates and
independent reviews. These focused results do not substitute for those executions.
