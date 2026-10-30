# Accepter mode — check, close or return

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/references/accepter.md -->

The tech lead, or the participant they named in a comment in the issue (`docs/CONTRIBUTING.md`,
«Кто закрывает»), accepts. The author, a merge and a card move never close the issue. The script
shows the viewer and warns when the viewer wrote the table or is an assignee; whether that person
may accept is decided by the human, not by the script.

## Conditions the script checks

Stop: the issue is CLOSED; the body has no AC or DoD; a PR with `Refs` is OPEN; no table (no marker
comment and no `--table-comment`); `--followup` with an unmet table or whose native parent is not
this issue; a box that cannot be ticked safely; (for `accept`) a returned comment newer than the table — the executor has not posted a new one (a table posted
by hand after the return is passed with `--table-comment ID`). Warnings (shown to the user, never a stop): the card
is not in AC Check; the viewer wrote the table / is an assignee; a merged PR has open threads or CI
other than SUCCESS; the table author is not an assignee; (for `return`) the table is older than an earlier return.

What stays with you: which of the referencing PRs belong to the task (the PRs list shows each
`refs`), and whether the comments after the table (`after_table`) leave a remark without an answer.

## Check the table selectively

Read every row, open some of the evidence: a PR page, a test file, a command output, a linked
comment. A row «выполнено» must show what the AC says, not only mention it. For a non-code AC the
linked comment must say what was done and how to check it. A moved AC (`перенесён в #M`) is fine
only with the tech lead's note in the body item; the script returns it otherwise. The script cannot
tell who wrote that note (the executor can edit the body too), so it warns for every moved row it
accepts: check that the tech lead added the note (the issue's edit history shows it).

If the table was written by hand or by another session, pass `--table-comment ID`. It is read
tolerantly (rows by `AC<n>`/`DoD<n>` or by item text); a gap makes the verdict `return`, never a
pass. The rule is in `examples/README.md` § How a table comment is read.

## Outcomes

| Outcome                     | Command                                             | Result                                                                              |
| --------------------------- | --------------------------------------------------- | ----------------------------------------------------------------------------------- |
| All rows hold               | `accept REF` → yes → `accept REF --apply`           | ticked boxes, comment naming every AC and DoD, issue closed as completed, read-back |
| «Не выполнено» in the table | `return REF` → yes → `--apply`                      | «что осталось» comment, card AC Check → In Progress, issue open                     |
| Evidence did not hold       | `return REF --fail AC2="причина"` → yes → `--apply` | the same, with your reason on that row                                              |
| Finding outside the AC      | file it first, then `accept REF --followup REF2`    | accept proceeds; the closing comment names the follow-up                            |
| Remark without an answer    | do not close; ask or answer, then repeat            |                                                                                     |

`--fail` may repeat; its reason replaces the table's reason for that id. `return` with nothing to
return is a stop. After a return the executor reworks in the same issue and posts a new table; run
`accept` again on the new one.

## Follow-up for a finding outside the AC

Only for new scope found during the check, outside every AC: one `create-issue` spec,
`parent` = the accepted issue (native sub-issue), an ordinary title. Never for an unmet AC or DoD
(Rule 1). Never «Хвосты приёмки #N»; open ones are not reopened or merged. The follow-up's number
goes to `--followup`; the script reads its native parent and stops when it is not this issue.

## After the close

`readback` must say `state: CLOSED`, `last_closer: manual`, `unchecked: 0`. If the run exits 3
after the writes, the error ends with `(written: …)` and names what was written; do not repeat blindly: read the issue (it may
be closed already) and tell the user. Done on the board is set by the workflow «Item closed».

## An issue closed the wrong way

Closed by a merge, a commit or the Development panel: the script stops (`last close was by merge`).
The tech lead reopens it, returns the card to In Progress or On Review and writes what is left.
The script never reopens.
