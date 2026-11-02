<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/examples/06-closed-by-merge/README.md -->

# 06 — the issue was closed by a merge: stop, write nothing

**Source:** synthetic (issue `larchanka-training/dmc-268-api-t6#500`, three AC, two DoD); see `../README.md` for the contract. The issue is CLOSED and its last close was by the merge of PR #901 (the Development trap,
CONTRIBUTING L85).

## Input

```sh
acceptance.py accept api#500 --apply
```

## Expected

`verdict: stop`, exit 1, one stop that says the close was by merge and that the tech lead reopens
the issue; the script never reopens. Nothing else is checked (`rows: []`, `table: null`), no
board read, and no write log even with `--apply`.
