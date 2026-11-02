<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/examples/08-guards-and-stops/README.md -->

# 08 — guards: warnings and stops

**Source:** synthetic (issue `larchanka-training/dmc-268-api-t6#500`, three AC, two DoD); see `../README.md` for the contract. Steps 1, 3, 4, 5 and 7 are `accept` on the state of case 03 (both PRs merged, the card in AC Check, the executor's table complete) with one thing changed each; the stop order is in `../README.md`.

## Steps

1. `acceptance.py accept api#500` — PR #903 is OPEN with `Refs #500`: **stop** (closing condition «Когда»); nothing after it is read.
2. `acceptance.py table api#500 rows.json` — the same open PR in author mode: a **warning** only (`verdict: ac_check`); the self-check follows the last PR of the task.
3. `acceptance.py accept api#500` — the card is in On Review: a **warning** naming the Status; the verdict is still `accept` (dry run).
4. `acceptance.py accept api#500` — the viewer (`executor`) wrote the table and is an assignee: a **warning**, not a stop; the script does not decide who the tech lead is (D3).
5. `acceptance.py accept api#500 --followup api#511 --apply` — api#511's native parent is #321, not #500: **stop**, no write.
6. `acceptance.py table api#500 rows.json` — AC3 is `moved` but the body item has no «перенесён в #285»: **stop**; only the tech lead moves an AC.
7. `acceptance.py accept api#500` — no comment with the marker and no `--table-comment`: **stop**; the message names both ways.
8. `acceptance.py accept api#500` — a returned comment (#8003) is newer than the executor's table: **stop** («таблица старше возврата #8003 …»); `return` only warns.
