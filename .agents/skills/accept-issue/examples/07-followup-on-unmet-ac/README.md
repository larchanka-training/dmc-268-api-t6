<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/examples/07-followup-on-unmet-ac/README.md -->

# 07 — «заводи follow-up на AC2» for an unmet AC

**Source:** synthetic (issue `larchanka-training/dmc-268-api-t6#500`, three AC, two DoD); see `../README.md` for the contract. AC2 is «не выполнено». The user says (`prompt.txt`):

> заводи follow-up на AC2 из #500

The request contradicts «Доработка — в том же issue» (CONTRIBUTING L79) and the follow-up rule
(L82): a follow-up is only for a finding outside the AC. The skill files nothing in either mode.
The rows are symmetric for DoD: the same holds for an unmet DoD. `prompt.txt` is read by nobody in
the test; it is the prompt for the manual Claude Code and Codex runs (T9).

## Step 1 — author mode

```sh
acceptance.py table api#500 rows.json
```

Dry run: `verdict: not_done`, `rework_rule` present, the card would go On Review → In Progress; no
write log, no create-issue spec.

## Step 2 — accepter mode

```sh
acceptance.py accept api#500
```

The table is in the issue, the card is in AC Check: `verdict: return`, `return_items: [AC2]`,
`rework_rule` present, `comment: null`, no write log.

## Step 3 — accepter mode, the follow-up flag on an unmet table

```sh
acceptance.py accept api#500 --followup api#510 --apply
```

`verdict: stop`: «follow-up на невыполненный AC или DoD не заводится — доработка в том же issue».
No write, even with `--apply`.

## Expected reply (judged by hand in Claude Code and Codex)

`rework_rule` is the script's neutral statement of the rule. The sentence that the request differs
from the rule is the skill's, in its reply, because here the user did ask.

In both modes the reply (Russian) must: say that it did not file a follow-up and why, citing the
rule «Доработка — в том же issue» (CONTRIBUTING L79); say that the request differs from the rule;
name the way forward — a code AC gets a new PR with `Refs #500` in this issue, a non-code AC an
action plus an evidence comment, then a new table and AC Check (author), or a `return` (accepter);
and offer a follow-up only for a finding outside the AC. It must not create an issue, a spec or a
sub-issue.
