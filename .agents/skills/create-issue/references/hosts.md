# GitHub mechanics

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/references/hosts.md -->

Read this when you need a `gh` flag you do not remember, the course board, native links,
or an explanation for a preflight, board or link failure. GitHub (`github.com`) is the only
host.

## Flag map (`gh issue create`)

| Need           | Flag                                                          |
| -------------- | ------------------------------------------------------------- |
| Title          | `-t/--title`                                                  |
| Body from file | `-F/--body-file <f>` (`-` = stdin)                            |
| Labels         | `-l` (repeat or comma) — each label must exist                |
| Assignee       | `-a login` (`@me` works)                                      |
| Milestone      | `-m <name>` — must already exist (checked before the create)  |
| Repo override  | `-R [HOST/]OWNER/REPO`                                        |
| Project        | `-p/--project <name>` — adds only; the script uses `item-add` |

`gh issue create` prints the created issue URL on stdout; that URL is the cross-link
handle. `issue-create.sh` assembles all of these from the spec — do not call
`gh issue create` by hand.

### Labels and milestones

Neither is created implicitly. List first, create explicitly:

```bash
gh label list --repo OWNER/REPO --json name --jq '.[].name'
gh label create "<name>" --repo OWNER/REPO --color <hex>
gh api repos/OWNER/REPO/milestones -f title="Sprint 42"   # `gh milestone` does not exist
```

The repo carries `bug`, `enhancement` and `documentation`, which are what `type` maps to.
Do not create `type:*` labels.

## gh 2.46 caveats

- Plain `gh issue view N` (no `--json`) and `gh pr edit` fail with a `projectCards`
  (Projects classic sunset) GraphQL error. Read an issue through REST instead:
  `gh api repos/OWNER/REPO/issues/N` — this is what the script's read-back does.
- To edit a body, use REST too: `gh api -X PATCH repos/OWNER/REPO/issues/N -F body=@file`.
- `gh project item-list` returns **30 items by default**; the course board is larger.
  Always pass `--limit 200`.
- `gh issue list --json …` with explicit fields is unaffected (checked on 2.46), which is
  why the duplicate guard uses it.

## The course board

Issues go on `larchanka-training/projects/12` (`dmc-268-t6`, issues-only):

```bash
gh project item-add 12 --owner larchanka-training --url <issue-url>
```

`issue-create.sh` runs this **after** the create (constants `BOARD_OWNER` /
`BOARD_NUMBER` in the script) unless `--no-board` is passed. It needs the `project` scope
on `gh auth`:

```bash
gh auth status                      # scopes must list `project` (and `repo`)
gh auth refresh -s project          # adds it, interactive
```

Without the scope nothing is created: the board's Status ids are resolved during the
validation, so the script stops with exit **3** and `run gh auth refresh -s project` (or
pass `--no-board`). If `item-add` fails after the create, the script prints its JSON (with
`"board": "failed"`) and exits **6** — run only the `item-add` line above for that URL,
never re-run the create.

Checking placement:

```bash
gh project item-list 12 --owner larchanka-training --limit 200 --format json \
  | python3 -c 'import json,sys; print([i["content"]["url"] for i in json.load(sys.stdin)["items"] if i.get("content")])'
```

`item-add` only adds the issue; the script then sets its Status — next section.

## Native links, milestone and board Status

`gh issue create` / `gh issue edit` (2.46) have no flag for a parent or a blocker, and
`item-add` sets no field. `issue-create.sh` checks all of them before the create (exit 2 on
a bad one) and sets them after it; `issue-link.sh` sets the links, also by hand to repair:

```bash
gh api repos/O/R/issues/13 --jq .id                 # the internal id — not 13
gh api -X POST repos/O/R/issues/5/sub_issues -F sub_issue_id=<id of 13>    # 13 under 5
gh api repos/O/R/issues/13 --jq .parent_issue_url   # read the child back
gh api -X POST repos/O/R/issues/13/dependencies/blocked_by -F issue_id=<id of blocker>
gh api "repos/O/R/issues/13/dependencies/blocked_by?per_page=100"   # read back
```

Board Status: `gh project view 12 --owner larchanka-training --format json` (`.id`), then
`gh project field-list 12 --owner larchanka-training --format json --limit 100` (field
`Status`, option by exact name) — ids are resolved by name, never hard-coded; then
`gh project item-edit --id <item> --project-id <project> --field-id <field>
--single-select-option-id <option>`, read back with `gh api graphql` (`node(id: <item>)` →
`ProjectV2Item.fieldValueByName(name: "Status")` → `name`). A spec `status` together with
`--no-board` is refused (exit 2): there is no board item to set it on.

The validation's target lookups are `issue-link.sh OWNER/REPO --check [--parent REF]
[--blocked-by REF]...` — for an issue not created yet: each target exists, is an issue, a
parent shares the owner; it writes nothing, prints `{"repo","targets":[{"kind","ref","id"}]}`
and exits 2 / 3 / 5 as a link run does, which `issue-create.sh` passes through.

Mines:

- **id ≠ number.** Both link endpoints take the issue's `.id`; the number is a
  valid-looking integer and the wrong value. `-F`, not `-f`, so the id goes as an integer.
- **The sub-issue POST answers with the parent**, not the child: read the child back.
- **One parent.** Attaching a second is a 422; the script never replaces a parent (no
  `replace_parent`) — change it on GitHub if that is intended. The parent must share the
  child's owner; a blocker may live in any repository, so the read-back matches
  `repository_url` **and** `number`, never the number alone.
- **No native "related".** GitHub has parent/sub-issue and blocked-by/blocking only;
  "Related, not blocking" stays text in Dependencies.
- **Milestones are per repository.** "Sprint 3" in ui and in api are two objects with one
  title. The script lists `repos/O/R/milestones?state=open&per_page=100` and wants an exact
  title; it never creates one (`gh api repos/O/R/milestones -f title=…`, above).
- **"Auto-add sub-issues to project"** (a board 12 workflow) puts a sub-issue on the board
  when its parent is there — even with `--no-board`.
- **Status race.** The "Item added to project" automation can set Todo just after the
  script's `item-edit` — even after a first read already shows the new value. So a Status
  other than Todo counts only when a second read, `ISSUE_STATUS_RETRY_DELAY` seconds later
  (default 3), still shows it; a mismatch on either read gets one re-edit (after the
  pause), then exit 6 with the `item-edit` line. Todo, what the automation sets, is read once.
- **Rate limits.** A 403 whose text says rate limit, secondary rate limit or abuse is exit
  5 (transient — wait, then rerun), not 3; any other 401 / 403 is auth.
- **Exit 7** means the issue exists and a link did not land. Run only the `repair:` command
  from stderr — `issue-link.sh OWNER/REPO#N --parent … --blocked-by …`; it skips what is in
  place. Quote a short ref in a shell (`--parent '#5'`): unquoted, `#` starts a comment.

## Why preflight probes in that order

A failing `gh api user` cannot tell a dead network from a rejected token, and the two send
the user to fix opposite things. So reachability is probed first, unauthenticated:
`curl -sS -m 5 -o /dev/null -w '%{http_code}' https://github.com/`, and the rule is
`curl_exit != 0 || http_code == 000`. Not an allowlist of exit codes — exit 56 means the
TCP connection succeeded and was then reset, which an allowlist of "resolve / connect /
timeout" failures would read as _reachable_. A 3xx counts as reachable, which is why the
probe does not follow redirects. TLS failures (curl 35/60/77/91) get their own message:
a certificate or proxy problem, not auth.

## Write permission

Preflight checks `gh api repos/OWNER/REPO --jq .has_issues` — the repository is visible to
this account and its issue tracker is on. It deliberately does **not** check
`permissions.push`: filing an issue is not pushing, and on a public repository any
authenticated user may open one.

## Remote aliases

The SSH-over-443 endpoint `ssh.github.com` is a DNS alias of `github.com`; preflight maps
it. A remote on any other host is refused with exit 2 — pass `--repo OWNER/REPO` to file
into a GitHub repository from a clone that points elsewhere.
