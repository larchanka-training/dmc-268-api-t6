<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/implement-issue/examples/11-unreadable-parts/README.md -->

# 11 — the criteria name parts the Scope does not number readably: stop

**Source:** synthetic, in the shape of the live api#245 (2026-10-31): the Scope bullets are bold
(`- **Ч.1 (P2, …). Title.** text`), the criteria are all `Ч.N:`-prefixed. The text is shortened.
api#298 has the same shape; api#397 writes its bullets «Часть N — …». The issue is OPEN, In Progress,
with no blockers, no sub-issues and no pull request.

## Input

```sh
preflight.py api#245
```

`build_parts` reads only `Ч.N` / `Part N` / `Teil N` at the very start of a Scope bullet, so no
part is parsed here: `parts` is empty, and the three criteria are reported in `warnings` as naming
parts that are not in the Scope.

## Expected

- `verdict: stop`, exit code 1, `selected: null`. Not a `go` for the whole task: that would
  implement every part in one pull request with an empty list of criteria.
- One reason: the criteria name parts (1, 2, 3) but the Scope has no part bullets the script can
  read — write them as `- Ч.N (…): …` (the create-issue format) or ask the user.
- The skill reports the reason and ends. `--part K` on this issue still exits 2 («has no parts»);
  an issue whose Scope bullets are readable, or whose criteria carry no prefix, is not affected.
