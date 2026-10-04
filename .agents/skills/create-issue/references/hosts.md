# GitHub mechanics

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/references/hosts.md -->

Read this when you need a `gh` flag you do not remember, the course board, or an
explanation for a preflight or board failure. GitHub (`github.com`) is the only host.

## Flag map (`gh issue create`)

| Need           | Flag                                                          |
| -------------- | ------------------------------------------------------------- |
| Title          | `-t/--title`                                                  |
| Body from file | `-F/--body-file <f>` (`-` = stdin)                            |
| Labels         | `-l` (repeat or comma) — each label must exist                |
| Assignee       | `-a login` (`@me` works)                                      |
| Milestone      | `-m <name>` — must already exist                              |
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

Without the scope the issue is still created: the script prints its JSON (with
`"board": "failed"`), a message containing `run gh auth refresh -s project`, and exits
**6**. Refresh the scope and run only the `item-add` line above for that URL — never
re-run the create.

Checking placement:

```bash
gh project item-list 12 --owner larchanka-training --limit 200 --format json \
  | python3 -c 'import json,sys; print([i["content"]["url"] for i in json.load(sys.stdin)["items"] if i.get("content")])'
```

`item-add` only adds the issue; it does not set the board's Status field. Status
changes are the board's own workflow and out of scope here.

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
