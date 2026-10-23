# #169 current implementation evidence

## Source context

Main integration base: `a50be1a2b2536c742743d7224aee4b20d422cc81`.
Feature HEAD before final working changes: `8a04e49221368d5bbe3ab0634c518719cb9af51b`.
Earlier full-suite checkpoints predate later implementation and are not final evidence.
Detailed historical iterations and private runtime artifacts are intentionally excluded
from this current acceptance summary.

## Measured evidence

| Check | Actual outcome and limit |
| --- | --- |
| Installed worker component composition | 325 passed with real PostgreSQL/RabbitMQ and MockTransport; publication used EMPTY findings. This predates current-main integration. |
| Current-main affected unit/component selection | 341 passed, nine integration cases deselected, two dependency warnings; includes four Redis startup/cancellation cleanup controls. |
| Offline proof-launcher regression | 16 passed; artifact/Node syntax and static checks passed after the final proxy correction. Offline checks do not prove publication. |
| Final local stand attempt | Runtime exit 1; 27 passed and one failed assertion; three of seven groups passed. |
| Independent final stand audit | Exit 0; owned process groups, ports, schema/SQL clients and broker topology/connections empty; all 146 then-frozen source files exact. |

The three successful stand groups cover trust authority, tree/policy revisions and
incomplete sources before model use. Known-CA public CLI activation A–E, unknown-CA
rejection with zero provider HTTP, target AGENTS bytes, target/start sizes deriving
75/25 languages, distinct merge-base/source-head bytes and keyset continuation passed.
Incomplete listing and size cases stopped before model HTTP or new SQL cache writes.

The GitLab-only worker group stopped at `public durable receipt replay`:
`replay_batch.error_count != 0`. The saved dispatch log reports
`CanonicalMergeRequestInvalidResponse` (internal category). The final canonical
GET `/api/v4/projects/17/merge_requests/12` returned 200; the specific cause is unresolved.
The remaining configured-GitHub/retry/redelivery, retirement/sibling and generation-two
groups were unreached. Application wire contained 80 GitLab calls: 79 HTTP 200 and one
intentional incomplete-size HEAD 503; model, GitHub and discussion/publication calls: zero.

This stand used real local PostgreSQL/RabbitMQ/public app processes and verified TLS
with Postman/Node stand-ins. It exercised prepared durable receipts, not HTTP webhook
ingress or live GitLab/model acceptance. Successful-run persisted policy/cache provenance,
nonempty inline/summary, confirmed pointer redelivery and later publication outcomes
remain unproved. The stand failure is not relabelled a successful complete proof.

## Current validation and remaining acceptance

Documentation validation: 44 existing contract/config checks passed (two dependency
warnings); YAML remains descriptions-only and GitLab env defaults remain disabled.
Whole Ruff, format (579 files), mypy (576 files) and diff checks passed.
Final documentation review and ONE full current-source pytest plus required static gates
remain pending. After those gates, publish an honest draft with the replay/publication
blockers, rather than initiating another automatic fixture-debug cycle.

#171 remains OPEN and external to this intake scope. Actual dependency merge/rebase,
selective authorization, real MR/run/discussion/redelivery evidence and current-head
human approval are required before live acceptance. Default-off configuration remains;
no deployment, merge, issue closure or all-AC-complete claim follows from these results.
