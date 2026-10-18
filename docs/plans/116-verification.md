# Verification: issue #116 / PR #124

## Development checkpoint

- Rebased original PR head `fa7002e974b5b81077b3dc7bfa34e937eb4520fb`
  onto current main `7546e539e1c1f565f65b9a5ca4cdc5dd1fbfb3fa`.
  Rebased head before the final formatting adjustment: `a7eedf7`.
- The only rebase conflict was `docs/WEBHOOK_WORKER.md`: retained main's
  supersession wait, age-limit WARNING and lifecycle wording, then kept only
  whitespace changes for the wake-up paragraph. Comparing every whitespace-delimited
  token with current main confirms the complete file's word content is unchanged.
- Active defaults in `app/bootstrap/llm_gateway.py` and recorded baseline manifest
  both use system v2; conventions defaults use v2. Examples and section references
  use the active versions; explicitly historical v1 files remain intact.
- WARNING description matches `prompt_budget.py`: 256 work units (a candidate
  examined or an alternative produced), one line per ignored rule per budget fit,
  after the prompt exceeds the budget, when file priorities are computed.
- Existing exact equality assertions remain at both public recording seams.
  `tmp_path` supplies temporary datasets; the diagnostic itself is static and has
  no dynamic path. Preservation, staging cleanup and no-provider-call assertions
  remain intact. The transport still forwards `RunProviderCheckpoint`.

## Diagnostic regression proof

Temporarily removed only `; preserve the existing baseline` from one runtime
raise site at a time in `review/scripts/eval_live.py`:

1. `_publish_capture` raise site:
   `uv run pytest tests/test_eval_live.py::test_newly_populated_baseline_is_not_overwritten_during_capture -q`
   — **RED**, 1 failed in 3.55s, at full diagnostic equality.
2. `record_live` early rejection raise site:
   `uv run pytest tests/test_eval_live.py::test_existing_response_bytes_are_preserved_without_provider_calls -q`
   — **RED**, 1 failed in 1.02s, at full diagnostic equality.

Both failures showed the expected full instruction versus the truncated runtime
message. Restored source byte-identically after each mutation and in a `finally`
block. SHA-256 before/after:
`4882639ef879e9afe9eb519a02da4e867e64c6bad9b1b337b5cfd1855accbe96`.
Raw output is retained locally in `/tmp/issue116-development-proof.txt`.

`uv run pytest tests/test_eval_live.py` — **GREEN**, **102 passed in 23.49s**,
zero skipped. This is the restored production code, not the mutation.

## Scope verification

- `git merge-base --is-ancestor origin/main HEAD` passes.
- `git diff --check` passes; no rebase markers remain.
- `git diff origin/main -- review/scripts/eval_live.py review/prompts test-prs-dataset`
  is empty: production recorder behavior, published prompts and baseline bytes
  have no changes.

## Final gates and integration evidence (2026-10-16)

The coordinator ran all checks on rebased HEAD
`a7eedf71ebc27da93c4d6cc2c1374e819ffb17f8` plus the final WEBHOOK_WORKER
inline-code reflow. No Python source, tests, prompt, baseline or dependency files
changed after these checks; only the reporting Markdown artifacts were completed.
The publication report in PR #124 records the final published commit SHA.

| Command | Result |
| --- | --- |
| `uv run ruff check .` | PASS, All checks passed |
| `uv run ruff format --check .` | PASS, 457 files already formatted |
| `uv run mypy .` | PASS, 454 source files |
| `uv run pytest` | PASS, 3787 passed, 0 failed/errors/skipped; 2 upstream deprecation warnings; 807.98s |
| `uv run pytest -m integration -rs` | PASS, 688 passed, 3099 deselected, 0 failed/errors/skipped; 2 upstream deprecation warnings; 605.56s |

Both pytest runs used these explicit isolated-service variables:

```text
TEST_DATABASE_URL=postgresql+psycopg://issue116:issue116@127.0.0.1:62521/issue116
TEST_RABBITMQ_URL=amqp://issue116:issue116@127.0.0.1:62522/issue116
```

The disposable services were PostgreSQL 17.11 (`issue116-pg`, separate database
and user `issue116`) and RabbitMQ 4.3.6 (`issue116-rabbit`, separate user and vhost
`issue116`). PostgreSQL and RabbitMQ were real; GitHub and LLM were mocked. No
live provider run is claimed. Local raw logs: `/tmp/issue116-pytest.log` and
`/tmp/issue116-integration.log`.

Cleanup completed successfully with `docker rm -fv issue116-pg issue116-rabbit`.
The subsequent verification-label-filtered `docker ps -a` was empty. Only these
dedicated containers and their anonymous volumes were removed; other local
services were left untouched.

The separate independent Standards and Spec reviews each reported zero findings
(see `116-review.md`). This is not a GitHub approving review. Current-head
GitHub approval and final tech-lead AC acceptance remain pending; issue #116
must remain open and PR #124 unmerged until the responsible humans complete
their steps. Issue acceptance checkboxes and board statuses are unchanged.

## Review round 2: correction and local documentation proof

External review on published head `b42195dae68bb89908d33c0ab820204b2dc73322`
found that the third original short line, `after removing and re-adding`, remained
at line 670. The first independent Spec review missed it. Our previous statement
that AC1's WEBHOOK_WORKER formatting was complete was an overclaim: token equality
proved content preservation, but did not prove completion of the requested layout.
The original evidence above is historical and does not establish this new fix.

P2 now reflows that line through the paragraph end at width 92, keeping inline-code
spans atomic. All three original standalone short lines from `f0f3c7e` were checked:
`tech lead approved`, `risks of the budget"). A revived`, and
`after removing and re-adding` are all integrated into longer lines now. The forced
71-character line before `installation_repositories.added` remains unchanged.
The complete affected paragraph (current lines 650–679) was inspected line by line.
The unchanged surrounding line 669 has 93 characters; the rewritten tail is at most
92. Tail lines 672 and 676 end early because the next complete inline-code span
cannot fit; line 679 is the paragraph's final line.

Local ad hoc checks passed:

- Every whitespace-delimited token in the entire WEBHOOK_WORKER file matches
  `origin/main` (`7546e539e1c1f565f65b9a5ca4cdc5dd1fbfb3fa`).
- Every inline-code span's content matches pre-edit `b42195d`, normalizing embedded
  whitespace. Every span in the complete reviewed paragraph now stays on one line,
  including `GET /repos` and the final-attempt WARNING.
- The rewritten tail matches greedy 92-column wrapping with code spans treated as
  atomic tokens. No Markdown formatting tests were added.
- `git diff --check` and current-main ancestry pass. Python, tests, review scripts,
  published prompts, baseline, `pyproject.toml` and `uv.lock` are byte-identical to
  both `b42195d` and the originally tested `a7eedf7`.

P3-1 now explains the actual version choice: `seed_prompt_versions` activates the
newest known version per key in `prompt_versions` by setting `is_active`. The manual
gateway CLI defaults use the same v2 files.
Historical v1 prompt files remain immutable. This wording was checked against
`seed_prompts.py:95–142` and CLI defaults in
`llm_gateway.py:85–86,334–335`; the paragraph is wrapped to its surrounding width.
Coordination with overlapping PR #133 preceded edits:
[comment](https://github.com/larchanka-training/dmc-268-api-t6/pull/133#issuecomment-6225203953).

Local line-by-line output: `/tmp/issue116-round2-doc-proof.txt`. Fresh separate
independent reviews completed with Standards 0 hard violations/0 smells and Spec 0
findings; see `116-review.md`. No fresh local pytest/gate run or new-head CI success
is claimed at this publication checkpoint. Current-head CI with all four gates,
full pytest and the explicit integration zero-skip guard is required after push.
Its exact SHA, run URL and actual counts are recorded in the existing
[PR AC/DoD report](https://github.com/larchanka-training/dmc-268-api-t6/pull/124#issuecomment-6217853912)
and [issue report](https://github.com/larchanka-training/dmc-268-api-t6/issues/116#issuecomment-6217854511)
once CI completes, without another metadata-only push. The reviewer's thread
resolution, approving review and final tech-lead acceptance remain pending.
