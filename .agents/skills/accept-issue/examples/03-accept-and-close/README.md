<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/examples/03-accept-and-close/README.md -->

# 03 — accept: tick, closing comment, close, read-back

**Source:** synthetic (issue `larchanka-training/dmc-268-api-t6#500`, three AC, two DoD); see `../README.md` for the contract. Both PRs are merged, the card is in AC Check, the executor's table (marker comment 3001)
has every row done or moved; AC3 is already ticked by the tech lead.

## Input

```sh
acceptance.py accept api#500 --apply
```

## Expected

- `verdict: accept`, `tick: [AC1, AC2, DoD1, DoD2]` (AC3 is already ticked and is left alone).
- The closing comment names every AC and DoD and links the table; the moved AC3 is listed once, as its body item reads («перенесён в #285 решением техлида»), with no second «перенесён».
- Write log, in order: `patch-body` (the ticked body, nothing else changed), `comment`, `close`
  (`state_reason: completed`). No board write: Done is set by the workflow «Item closed».
- `readback` comes from `after-issue-p1` / `after-acceptance-p1`: `CLOSED`, `last_closer: manual`,
  5 of 5 ticked, 0 unchecked.
