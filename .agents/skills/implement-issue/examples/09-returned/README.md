<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/09-returned/README.md -->

# 09 — returned at acceptance: stop, rework goes through `--part`

**Source:** synthetic: the fixture of `06-accept` plus two comments, as accept-issue writes them
(the issue number in the links is real, the comment ids are made up): the author's table
(`<!-- accept-issue:table v1 -->`, 2026-10-30T21:40Z) and the accepter's return
(`<!-- accept-issue:returned v1 -->`, 2026-10-30T22:00Z). The last pull request was merged at
21:35Z, before the return.

## Input

```sh
preflight.py api#266
```

## Expected

- `verdict: stop`, exit code 1, `selected: null`, not `accept`.
- One reason: the issue was returned at acceptance, with the link to the return comment, and no
  pull request with `Refs` has been merged since — «rework a part with --part K».
- The skill reports the reason and ends. The user names the part to rework and runs it with
  `--part K`; once a pull request with `Refs` is merged after the comment, the verdict is `accept`
  again. An issue without parts that was returned has no `--part`: that rework is manual for now.
