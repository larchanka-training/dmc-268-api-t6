# PR #132 integration with GitLab publication (#113)

> **Historical — round six.** This document records checkpoint
> `d88725771ef89a94756ea3a06befd37b44b425d4`, based on main
> `2251b5936822827fc75b38cfc6b352c6a5e89a6b`, plus the integration changes
> described in this round. Its plans, counts, gates, reviews and merge prerequisites
> apply to that historical scope; they do not approve or verify current main or
> the current PR head. Current #151 evidence is recorded in
> [151-verification.md](151-verification.md).

## Base and contracts

Integrate main `2251b5936822827fc75b38cfc6b352c6a5e89a6b` (merged #137)
with historical verified PR head `b5e0cec92c24a8e702a90e75de5a10350f89cdaa`.
Preserve GitLab partial publication and note rendering, run-step notifications,
GitHub review receipts, immutable GitHub installation origins, cancellation durability,
mandatory #121 receipt retry outcomes and #126 rollback revision admission.
Network calls remain outside database transactions. No dependency changes.

## Recorded implementation plan

1. Rebase the isolated branch on the then-current main; inspect publication, lifecycle,
   worker wiring, listeners and documentation conflicts semantically.
2. Run inherited focused regressions first against dedicated PostgreSQL/RabbitMQ.
   For each integration failure, retain an observable regression and make the
   smallest compatible change. Preserve existing GitHub origin fences on provider calls.
3. Record round-six base integration evidence and intentional source changes.

## Recorded completion conditions

The plan required focused regressions without skips, all repository gates and
independent Standards/Spec reviews, followed by publication of the exact verified
head using its remote lease. Human approval on that head and writer shutdown
coordination were merge prerequisites at the historical checkpoint. Historical
round-five evidence applies only to its recorded head.
