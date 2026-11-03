<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/pr-review/examples/01-known-defect/README.md -->

# 01 — a known defect: found and tied to a line

**Source:** a live snapshot of ui#130 and ui#111, recorded on 2026-11-03 by saving the output of
the two `gh api … --jq` calls `pr_context.py` makes, with no edits.

**The known defect.** `RunControls.tsx:41` (a line the diff adds) builds Ant Design's internal
disabled-button class, `${getPrefixCls('btn')}-disabled`, on a screen. `.agents/rules/frontend.md`
§3a (rules 1, 3 and 5) and `AGENTS.md` («Do not restyle a base component on a screen») keep the look
of a base component inside `src/shared/ui`. ui#139 (`4917c70a`, under ui#131) later moved it into
`useDisabledButtonClass` in `src/shared/ui/lib`, with a guard in `antd.button.test.tsx`.

**Not the target.** The same head drops the focus to the page when a cancel ends the run (§3b
rule 3); ui#139 fixed that too. The pull request body declares it under «Known, not changed here»
as present on `main` before the change, so the brief ranks it `info` at most. The first version of
this case aimed at it; neither manual run reported it, not even as `info`.

**A limit of this case.** The same section of the body also names the line-41 defect («relies on
antd's internal class … a guard belongs in `src/shared/ui`»), and the brief passes the body to the
reviewers. So this case shows that a finding is tied to its line; example 04 shows a defect found
in the code alone.

## Input

```sh
pr_context.py ui#130
```

ui#130 is merged, head `21b8d99b895956a7c0dcd2ee809b401552aad806`. Its body has `Refs #111` and
names no part. ui#111 has no parts in its Scope and three acceptance criteria.

## Expected

- `verdict: go`, exit code 0, `reasons` empty.
- One issue, ui#111, `parts: []`, AC1–AC3 with `part: null` (keys `ui#111 AC1`…), in the
  issue's order.
- One warning: the pull request is merged.

## Expected review

Run in the ui clone. This part is behaviour, not compared byte by byte: the manual runs in Claude
Code and Codex are attached to the pull request that added the skill.

- The reviewers' first line names `head 21b8d99b895956a7c0dcd2ee809b401552aad806`.
- **Standards** reports the known defect at `src/widgets/run-inspector/ui/RunControls.tsx:41` (the
  `ConfigContext` read at `:24` may be named with it) and cites `.agents/rules/frontend.md` §3a or
  the `AGENTS.md` rule against restyling a base component on a screen. Severity medium or higher.
  The focus drop may show up as `info`; it is not required.
- **AC** has one row per criterion, AC1–AC3, each `covered`, `not covered` or `can't tell` with a
  `path:line` at the head or a reason. AC3 (the gates) is `can't tell`: a CI run is not in the
  diff. No row is `covered` without a link.
