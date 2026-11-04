<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/examples/03-part-ac/README.md -->

# 03 — a part's pull request: only that part's criteria

**Source:** a live snapshot of api#358 and api#273, recorded on 2026-11-03 by saving the output of
the two `gh api … --jq` calls `pr_context.py` makes, with no edits.

## Input

```sh
pr_context.py api#358
```

api#358 is merged, head `21f397492a7bed3cf40a23f09235dba7dab6df19`. The first line of its
`## What` is «Part 2 of #273: the sprint start ritual. Part 1, …, is merged in #357.» and its
body has `Refs #273`. api#273 has «Ч.1» and «Ч.2» in its Scope and four acceptance criteria: three
prefixed `Ч.1:`, one prefixed `Ч.2:`, none without a prefix.

## Expected

- `verdict: go`, exit code 0, `reasons` empty.
- One issue, api#273, `parts: [2]`. Its criteria are AC4 alone (key `api#273 AC4`), `part: 2`,
  the prefix cut off; AC1–AC3 (Part 1) are not listed. The id keeps the issue's numbering.
- One warning: the pull request is merged.

## Expected review

Run in the api clone; behaviour, as in 01.

- **AC** has one row, AC4: `covered`, with `docs/CONTRIBUTING.md:255` (`### Старт спринта`, the
  steps table at 259–265) and the published graph link (`docs/CONTRIBUTING.md:275`, also in the
  pull request body). A documentation criterion needs no test. No row for AC1–AC3.
- **Spec** reads api#273 and reports nothing that Part 1 should have done as missing here.
