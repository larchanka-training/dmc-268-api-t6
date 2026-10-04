<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/golden/github-bug.md -->

**Environment:** staging, Firefox 141

## Steps to reproduce

1. Open a PR with 3 hunks
2. Scroll to the bottom

**Expected:** All three hunks render

**Actual:** The third hunk is missing

**Severity:** High

## Acceptance Criteria

- [ ] All hunks render

## Dependencies

None

## DoD Checklist

- [ ] Gates green (AGENTS.md → Commands)
- [ ] Docs updated
- [ ] PR approved, all review threads resolved
- [ ] Issue status updated on board 12
