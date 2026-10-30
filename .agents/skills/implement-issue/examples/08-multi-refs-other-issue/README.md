<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/08-multi-refs-other-issue/README.md -->

# 08 — the same pull requests are strays for the other issue

**Source:** synthetic: the fixture of `07-multi-refs` with the issue replaced by a skeleton of
api#130 (the same six parts, OPEN, no blockers). The timeline is the same: the real api#346 and
api#353.

## Input

```sh
preflight.py api#130
```

Both pull requests have `Refs #130` among several issues, but their part mentions («#168 part 6»,
«#168 part 4») are tied to api#168.

## Expected

- `verdict: go`, exit code 0, `selected: {part: 1, reason: first-todo}`.
- Every part is `todo`: neither pull request names a part of api#130.
- One warning lists api#346 and api#353 as pull requests with `Refs` that name no part of this
  issue. Were either OPEN, the run would stop on it («say which part each covers, or pass
  --part»).
- Before 1.1.0 the two pull requests counted for Ч.6 and Ч.4 of api#130 as well, which finished
  parts that nobody had done.
