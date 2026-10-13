# PR #132 integration with GitLab publication (#113)

## Base and contracts

Integrate main `2251b5936822827fc75b38cfc6b352c6a5e89a6b` (merged #137)
with historical verified PR head `b5e0cec92c24a8e702a90e75de5a10350f89cdaa`.
Preserve GitLab partial publication and note rendering, run-step notifications,
GitHub review receipts, immutable GitHub installation origins, cancellation durability,
mandatory #121 receipt retry outcomes and #126 rollback revision admission.
Network calls remain outside database transactions. No dependency changes.

## Implementation

1. Rebase the isolated branch on actual main; inspect publication, lifecycle,
   worker wiring, listeners and documentation conflicts semantically.
2. Run inherited focused regressions first against dedicated PostgreSQL/RabbitMQ.
   For each integration failure, retain an observable regression and make the
   smallest compatible change. Preserve existing GitHub origin fences on provider calls.
3. Record current-base integration evidence and intentional source changes.

## Completion conditions

Focused regressions pass with no skips; repository gates and fresh independent
Standards/Spec reviews pass; publication uses the exact verified head and current
remote lease. Current-head human approval and writer shutdown coordination remain
merge prerequisites. Historical round-five evidence applies only to its recorded head.
