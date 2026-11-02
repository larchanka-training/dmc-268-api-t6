<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/examples/04-return-with-fail/README.md -->

# 04 — return: a «не выполнено» in the table and a `--fail`

**Source:** synthetic (issue `larchanka-training/dmc-268-api-t6#500`, three AC, two DoD); see `../README.md` for the contract. The card is in AC Check and the executor's table has AC3 «не выполнено» (the author found it
after moving the card; the author does not move a card out of AC Check). The accepter
also checked AC1 and the evidence did not hold.

## Input

```sh
acceptance.py return api#500 --fail 'AC1=версия в ответе не совпадает с тегом релиза' --apply
```

## Expected

- `verdict: return`; `return_items`: AC1 («доказательство не подтвердилось: …») and AC3
  («не выполнено»), in body order; `rework_rule` is present (the run returns items; the text states the rule and does not claim any request).
- Write log: the returned comment (links the table, lists what is left, names the rework rule),
  then `set-status In Progress`. The board goes AC Check → In Progress, read back. The issue is
  not closed and no checkbox is ticked.
