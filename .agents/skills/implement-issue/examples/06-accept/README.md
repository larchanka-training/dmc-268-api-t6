<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/06-accept/README.md -->

# 06 — every part is merged: acceptance is next

**Source:** synthetic, from the fixture of `03-next-part` (the real api#266 at 2026-10-29, without
its blocker api#272) plus the two real pull requests of its Part 2, ui#221 and api#405 (merged
2026-10-30), with the body reduced to the first line of `## What` and the `## Refs` lines. The
issue stays OPEN and In Progress here; the live api#266 has been accepted and closed since.

## Input

```sh
preflight.py api#266
```

Ч.1 has ui#213 and api#382 (MERGED), Ч.2 has ui#221 and api#405 (MERGED), all with a `Refs` line
naming api#266 and a part in the first line of `What`. No pull request is open and no comment
carries an accept-issue marker.

## Expected

- `verdict: accept`, exit code 1, `selected: null`.
- One reason: «every part has a merged pull request — acceptance is next». Both parts are `done`;
  no warnings.
- This is not a stop and not a go. The skill does not cut a branch or move the card; it runs the
  `accept-issue` skill in author mode on the same REF (the self-check table, then AC Check).
- An issue without parts gives the same verdict once its only pull request with `Refs` is merged
  and none is open (the reason says «the task has no parts and its pull request with Refs is
  merged»).
