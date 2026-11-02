<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/examples/05-finding-outside-ac/README.md -->

# 05 — a finding outside the AC: accept, plus a follow-up with a parent

**Source:** synthetic (issue `larchanka-training/dmc-268-api-t6#500`, three AC, two DoD); see `../README.md` for the contract. Same state as case 03, and a comment after the table: the accepter noticed that `/health`
has no Prometheus metrics. That is new scope outside every AC.

## Input

```sh
acceptance.py accept api#500 --followup api#510 --apply
```

`followup-spec.json` is the `create-issue` batch the skill hands over (a sub-issue of this issue:
`parent: "#500"`, an ordinary title, no «Хвосты приёмки»); it was filed as api#510. The test only
reads it as JSON (`parent`, the title) and never runs the create-issue scripts.

## Expected

Same as case 03, plus: the comment after the table is listed in `after_table`, and the closing
comment ends with `Follow-up вне AC (новый скоуп): #510`. The script reads the follow-up's native
parent (fixture `parent-…-510`) and `followups` shows `parent` = this issue; a follow-up with another
parent is a stop (case 08). An unmet AC would never reach this path
(case 07).
