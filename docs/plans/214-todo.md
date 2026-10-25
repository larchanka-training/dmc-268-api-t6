# Tasks: #214 — worker portal URL

**Implementation, verification and review evidence are complete.** Final branch base:
`f8b13103f908b26e3e210c3f78b125f21bfbdaba`; branch `chore/214-portal-url-wiring`.
The original planning/mutation baseline was
`e6d3077cfcb5584f8af0524ce4a0c8a658b2c5fb` and is recorded separately.
See [214-plan.md](214-plan.md) for the approved design and
[214-verification.md](214-verification.md) for precise results and publication body.

## Prerequisites

- [x] Human approved the written plan before implementation.
- [x] Use an isolated checkout; preserve the original checkout and unrelated plans.
- [x] Refresh ownership and coordinate before overlapping edits. The coordinator
      posted #214 comments on #220/#227/#254/#279/#290 before source/docs changes;
      latest refresh covered new #280/#297 before rebasing shared files.

## Task 1: Protect the composed check-run links

- [x] Two independently named tests execute the real composed HandleReviewRun
      recovery and successful PublishRunReview paths, each with fresh state.
- [x] Assert a captured view, literal UUID, details URL and terminal summary;
      trailing-slash bases normalize and absent portal configuration has no link.
- [x] Keep constructors, URL factory, renderer and transactional trace real.
      Typed fake gateways execute checkpoints and assert closed transactions.
      New tests require no PostgreSQL/RabbitMQ/Redis/network or integration marker.
- [x] Record the original full publisher-deletion experiment at the planning base:
      5685 passed, one unrelated leader timing failure, zero skips; isolated
      restored retry passed. Never label that full experiment green.
- [x] Independently delete each constructor's single URL kwarg on the final Task 2
      code, observe its linked case fail on details_url=None, and restore exactly.
      Both replacements match once; restored tests pass and no mutation remains.
- [x] Adapt only the test fake's completion signature after upstream #171;
      preserve upstream RAG/verifier changes and the #214 policy/tests on latest rebase.
      Both mutations on 4faa fail on missing details_url and restore byte-exact.

## Task 2: Reject malformed portal settings at startup

- [x] Establish RED before implementation: malformed settings raise no error on
      baseline; credential-safe errors and accepted literal bases are covered.
- [x] Add the small worker-local standard-library validator. Reject unsupported
      or absent scheme/host, whitespace/control characters, query/fragment even
      when empty, userinfo, bad brackets/ports, empty ports and backslashes.
- [x] Raise RuntimeError naming PORTAL_URL without echoing its value or a parser
      exception containing credentials; fail before starting worker resources.
- [x] Preserve absent/empty configuration and valid HTTP/HTTPS, localhost, IPv4,
      bracketed IPv6 with/without port, base paths, encoded paths, scheme case
      and trailing-slash behavior. The literal staging base passes.
- [x] Update SECRETS and CICD §2.1 to match the exact operator policy, startup
      failure/healthcheck rollback, and retained worker.env correction behavior.
      No real setting, dependency, deployment wiring or application layer changed.
- [x] Focused portal/settings suites: 56 passed. Unit selection: 56 passed.
      Rebased portal plus new upstream parser checks: 71 passed.

## Task 3: Verify the final change and prepare review evidence

- [x] Relevant use-case/render regressions: 167 passed.
- [x] New tests pass the existing CI unmarked unit selection; no workflow or
      exclusion was added. Remote CI runs after the PR is published.
- [x] Rebase onto pinned current main without conflicts; focused tests and both
      missing-link mutations pass their expected checks.
- [x] Verify all 1165 frozen Linux snapshot hashes and all 6437 collected tests.
- [x] Prior CF50 checkpoint: all four gates exit 0; full pytest 6032 passed,
      zero skipped, two deprecation warnings.
- [x] Run all four gates without exclusions on 4faa source: all exit 0;
      full pytest 6437 passed, zero skips, two existing warnings in 1532.28 s.
- [x] Rebase onto f8: exactly one docs-only commit, 44 paths, none of our eight;
      all non-doc trees and five #214 implementation/operator/test hashes identical.
      Ruff/format/mypy pass again (591 formatted files, 581 typed source files).
- [x] Inspect final diff and whitespace; no temporary mutation, unrelated local
      plan, dependency, transport-contract or schema change is included.
- [x] Prior two-axis review against the actual issue: Standards 0, Spec 0.
- [x] Fresh two-axis review after the composition rebase: Standards 0, Spec 0.
      Exact feature diff unchanged by the final docs-only rebase.
- [x] Prepare precise baseline/TDD/mutation/full-gate evidence and a ready PR body
      using What / Why / How to verify / Refs, without issue-closing keywords.

## Publication and human acceptance

Implementation and verification are complete. No commit, push or PR is claimed
by this checklist. The next stage publishes a ready PR; the user assigns its
reviewer manually. GitHub approving review on the current head, resolved threads,
merge and tech-lead issue acceptance/closure remain outside this work. Live
staging acceptance and durability are not inferred from these functional tests.
