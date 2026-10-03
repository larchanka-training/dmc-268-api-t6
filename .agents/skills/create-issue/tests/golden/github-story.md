<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/golden/github-story.md -->

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
