<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/examples/04-undisclosed-defect/README.md -->

# 04 — a defect the body does not mention, with an issue in the other repo

**Source:** a live snapshot of ui#72 and api#54, recorded on 2026-11-03 by saving the output of
the two `gh api … --jq` calls `pr_context.py` makes, with no edits.

**The known defects.** The human review of ui#72 left five P3s; ui#83 (`ebc538ad`) fixed them and
lists them in its body («P3 из ui#72»). Three are in the diff of ui#72, and its body mentions none:

- two «Обновить доступ» buttons in the empty state: the one in the card header
  (`src/widgets/repository-list/ui/RepositoryList.tsx:285`, always rendered) and the one in the
  empty state (`:343`);
- the tests read the expected tooltip texts from the constants the component exports
  (`src/widgets/repository-list/ui/RepositoryList.test.tsx:7-13`, asserted at 260, 267 and 275): a
  tautological test (`.agents/skills/tdd/SKILL.md`, «Tautological»);
- with no `onSyncAccess` the widget falls back to the auth store itself (`RepositoryList.tsx:38`,
  `:75-76`), and the page passes none (`src/pages/repositories/RepositoriesPage.tsx:69`, outside the
  diff).

This case was added after example 01, whose defect the pull request body declares: here the
reviewers can only find the defects in the code.

## Input

```sh
pr_context.py ui#72
```

ui#72 is merged, head `02b0758a20821690ede586a70945d7734b4ebf33`. Its body has
`Refs larchanka-training/dmc-268-api-t6#54`, an issue of the other repo, and names no part. api#54
has twelve acceptance criteria prefixed «Часть 1 —» / «Часть 2 —»: not the `Ч.K:` form, so none is
tied to a part.

## Expected

- `verdict: go`, exit code 0, `reasons` empty.
- One issue, api#54, `parts: []`, AC1–AC12 with `part: null` (keys `api#54 AC1`…), the text
  whole (the prefix kept).
- One warning: the pull request is merged. No part warning: no criterion carries a part.

## Expected review

Written before the manual runs. Run in the ui clone; behaviour, as in 01.

- **Standards** or **Spec** reports at least one of the three known defects at its line:
  `RepositoryList.tsx:285` or `:343`, `RepositoryList.test.tsx:7-13` or an assertion that uses the
  imported constants, or `RepositoryList.tsx:75-76`. Any severity: the human review ranked them P3.
- **AC** has twelve rows, one per key. AC3 (the hints in `RepositoryList` describe the real path,
  and a ui test covers it) links `RepositoryList.tsx` and `RepositoryList.test.tsx`. The other
  eleven are `can't tell`: their proof is in the api repo, not in this clone.
