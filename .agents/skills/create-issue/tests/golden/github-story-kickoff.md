<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/golden/github-story-kickoff.md -->

## User Story

**AS** an operator
**I WANT** per-node metrics
**SO THAT** I can spot a single bad node

## Background

The dashboard aggregates across nodes, so one bad node
is invisible until it takes the average down.

## Scope

- Add GET /metrics/nodes
- Extend the collector

## Acceptance Criteria

- [ ] Endpoint returns per-node rows
- [ ] Covered by a test

## Dependencies

None

## DoD Checklist

- [ ] Gates green (AGENTS.md → Commands)
- [ ] Docs updated
- [ ] PR approved, all review threads resolved
- [ ] Issue status updated on board 12

## References

- https://example.invalid/design

## Kickoff — where to start

**Repo:** acme/sandbox — plus acme/shared for the collector client

**Branch:** fix/<n>-metrics-gate — main is protected, open a PR

**The shape of the problem:** The coverage gate fails because the CI job never runs the collector
module's tests.

**Read first:**

- docs/ci.md#coverage — which job reports coverage

**Before writing code:**

- make test-collector — reproduces 83.3% locally

**Mines:**

- never run make baseline locally — it overwrites the stored baseline
