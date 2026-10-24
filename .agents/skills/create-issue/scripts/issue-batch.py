#!/usr/bin/env python3
"""issue-batch.py — create a batch of related issues once, in dependency order, and
resume an interrupted batch without creating anything twice.

A batch is riskier than one issue in one specific way: it can stop halfway. So the
whole batch is validated before the first create — locally, then remotely by
`issue-create.sh --dry-run` for every item still to create; parents and blockers are
created before what depends on them, so a local ref `@key` can be replaced by the
issue it names; every result is written to a state file before the next item starts;
and a rerun of the same command is the normal way to resume — an item already in the
state file is not created again, only its links are ensured. An exact-title duplicate
is adopted only on a rerun, and only an open issue you opened since the batch started
(less CLOCK_SKEW_S: one an earlier run created and could not record); any other
duplicate stops the batch. Each item is created by issue-create.sh as a separate
process, so the single-issue guarantees (attribution guard, validation, read-back,
board, links) hold per item. Details: references/batch.md.

Usage
-----
    issue-batch.py <batch.json> [--dry-run] [--no-board]
    issue-batch.py --compare-shape <golden.json> <candidate.json>

Batch file: {"id": "<slug>", "items": [{"key": "<slug>", ...spec keys...}, ...]};
slugs match [a-z0-9][a-z0-9-]*; `parent` / `blocked_by` may name another item as
"@key". Python 3.10+, stdlib only.

Output
------
--dry-run: the preview on stdout — a table of the items in creation order, the items
  already created (key, title, url), what a run would do with each exact-title
  duplicate (adopt it or stop), then each body to create under a heading with its
  key; every item not yet created runs `issue-create.sh --dry-run` (local refs left
  out), so a bad milestone, Status or link target fails the preview. Nothing is
  created, no state is written.
real run: {"id", "items": [{"key", "url", "number", "repo", "created", "adopted",
  "verified", "board", "links"}], "state_file"} on stdout — also when an item stops
  the batch (then it lists the items done before it).
--compare-shape: {"same_shape", "differences"} — item count, and per item in creation
  order: repo, the parent / blocked_by graph (local refs as positions, so keys may
  differ), the number of parts, whether each part has AC and a done_when, kickoff
  read_first and mines. Type, titles, wording and the AC count are not compared.
Errors: {"error": "..."} on stderr; the item scripts' stderr is passed through.

Exit
----
0 every item done · 2 usage or invalid batch (nothing ran), a state entry that does
not match its item, or an item exit 2 · 3 auth · 4 an item exit 4, or a duplicate
that is not adopted · 5 network/unknown, or a create whose result could not be read
or recorded · 6 an issue — created now or recorded earlier — whose board step failed
· 7 a native link failed (wins over 6). 2-5 stop the batch: the state file keeps what
was created, rerun the same command. 6 and 7 do not stop it. --compare-shape: 0 same
shape, 1 different, 2 a file is not a batch.

State: ${XDG_CACHE_HOME:-$HOME/.cache}/create-issue/batches/<id>.json —
{"started_at": ISO, "items": {key: {"url", "number", "repo", "summary", "board",
"links"}}}; started_at is written before the first create, each entry atomically
right after its item.
Test seam: ISSUE_BATCH_SCRIPTS_DIR — the directory issue-create.sh and issue-link.sh
are run from (default: this script's directory; a warning says when it is set); the
builder always runs from here.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/scripts/issue-batch.py

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

HERE = Path(__file__).resolve().parent
BUILDER = HERE / "build_issue_body.py"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
USAGE = (
    "usage: issue-batch.py <batch.json> [--dry-run] [--no-board] | "
    "issue-batch.py --compare-shape <golden.json> <candidate.json>"
)
# The exit codes an item may stop the batch with; anything else is reported as 5.
STOP_CODES = (2, 3, 4, 5)
BOARD_REPAIR = "gh project item-add 12 --owner larchanka-training --url {url}"
# started_at is this machine's clock, GitHub's createdAt its own: an issue created up to
# this many seconds before started_at still counts as created since the batch started.
CLOCK_SKEW_S = 120


class BatchError(Exception):
    """The batch file is not a valid batch."""


def err(msg: str, code: int = 2) -> NoReturn:
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(code)


def warn(msg: str) -> None:
    print(json.dumps({"warning": msg}, ensure_ascii=False), file=sys.stderr)


def load(path: str, what: str) -> Any:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        err(f"cannot read the {what} file: {exc}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        err(f"the {what} file is not valid JSON: {exc}")


# ─── structure, local refs and order ────────────────────────────────────────
def local_parent(item: dict[str, Any]) -> str | None:
    """The key an item's parent names as "@key", or None."""
    parent = item.get("parent")
    return parent[1:] if isinstance(parent, str) and parent.startswith("@") else None


def local_blockers(item: dict[str, Any]) -> list[str]:
    """The keys an item's blocked_by names as "@key", in order."""
    blocked = item.get("blocked_by")
    if not isinstance(blocked, list):
        return []
    return [r[1:] for r in blocked if isinstance(r, str) and r.startswith("@")]


def owner(repo: Any) -> str:
    return repo.split("/")[0].lower() if isinstance(repo, str) else ""


def structure(batch: Any) -> list[dict[str, Any]]:
    """Check the batch shape and the local refs; return the items in creation order."""
    if not isinstance(batch, dict):
        raise BatchError("batch must be a JSON object")
    unknown = set(batch) - {"id", "items"}
    if unknown:
        raise BatchError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    if not isinstance(batch.get("id"), str) or not SLUG.fullmatch(batch["id"]):
        raise BatchError(f'"id" must match [a-z0-9][a-z0-9-]*, got {batch.get("id")!r}')
    items = batch.get("items")
    if not isinstance(items, list) or not items:
        raise BatchError('"items" must be a non-empty list of objects')
    position: dict[str, int] = {}
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise BatchError(f"item {n} must be an object")
        if "key" not in item:
            raise BatchError(f'item {n} has no "key"')
        key = item["key"]
        if not isinstance(key, str) or not SLUG.fullmatch(key):
            raise BatchError(f'item {n} "key" must match [a-z0-9][a-z0-9-]*, got {key!r}')
        if key in position:
            raise BatchError(f'duplicate key "{key}" (items {position[key]} and {n})')
        position[key] = n
    by_key = {item["key"]: item for item in items}
    deps: dict[str, list[str]] = {}
    for key, item in by_key.items():
        parent, blockers = local_parent(item), local_blockers(item)
        twice = next((r for i, r in enumerate(blockers) if r in blockers[:i]), None)
        if twice is not None:
            raise BatchError(f'item "{key}": "blocked_by" lists "@{twice}" more than once')
        refs = ([parent] if parent is not None else []) + blockers
        for ref in refs:
            if ref == key:
                raise BatchError(f'item "{key}" refers to itself ("@{ref}")')
            if ref not in by_key:
                raise BatchError(f'item "{key}": unknown local ref "@{ref}"')
        # GitHub: a sub-issue shares its parent's owner (build_issue_body.py checks the
        # same for a parent that already exists).
        if parent is not None and owner(by_key[parent].get("repo")) != owner(item.get("repo")):
            raise BatchError(
                f'item "{key}": parent "@{parent}" is in {by_key[parent].get("repo")}, but a '
                f"sub-issue must share its parent's owner ({owner(item.get('repo'))})"
            )
        deps[key] = refs
    return creation_order(items, deps)


def creation_order(items: list[dict[str, Any]], deps: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Parents and blockers first; among the items that can go next, file order."""
    placed: set[str] = set()
    ordered: list[dict[str, Any]] = []
    remaining = list(items)
    while remaining:
        ready = next((i for i in remaining if all(d in placed for d in deps[i["key"]])), None)
        if ready is None:
            raise BatchError("dependency cycle: " + " → ".join(find_cycle(remaining, deps)))
        ordered.append(ready)
        placed.add(ready["key"])
        remaining.remove(ready)
    return ordered


def find_cycle(remaining: list[dict[str, Any]], deps: dict[str, list[str]]) -> list[str]:
    """Every item left waits for another item left, so following the first such
    dependency from any of them must come back to a key already on the path."""
    left = {i["key"] for i in remaining}
    path = [remaining[0]["key"]]
    while True:
        nxt = next(d for d in deps[path[-1]] if d in left)
        if nxt in path:
            return [*path[path.index(nxt) :], nxt]
        path.append(nxt)


# ─── specs ──────────────────────────────────────────────────────────────────
def full_ref(ref: str, repo: str) -> str:
    """A short ref "#N" becomes "OWNER/REPO#N" of the item; "@key" and full refs stay."""
    return f"{repo}{ref}" if ref.startswith("#") else ref


def spec_of(item: dict[str, Any], resolve: dict[str, str] | None) -> dict[str, Any]:
    """The item as a spec for build_issue_body.py / issue-create.sh: without `key`,
    each "@key" replaced by the issue in `resolve` — or, with `resolve` None, left out
    (the validation and the preview run before those issues exist)."""
    spec = {k: v for k, v in item.items() if k != "key"}
    parent, blocked = spec.get("parent"), spec.get("blocked_by")
    if isinstance(parent, str) and parent.startswith("@"):
        if resolve is None:
            del spec["parent"]
        else:
            spec["parent"] = resolve[parent[1:]]
    if isinstance(blocked, list):
        refs = []
        for ref in blocked:
            if isinstance(ref, str) and ref.startswith("@"):
                if resolve is not None:
                    refs.append(resolve[ref[1:]])
            else:
                refs.append(ref)
        if refs or not blocked:
            spec["blocked_by"] = refs
        else:
            del spec["blocked_by"]
    return spec


def link_args(spec: dict[str, Any]) -> list[str]:
    args = ["--parent", spec["parent"]] if "parent" in spec else []
    for ref in spec.get("blocked_by", []):
        args += ["--blocked-by", ref]
    return args


def build_body(key: str, spec: dict[str, Any]) -> str:
    """The body, from the builder itself: a spec it refuses stops the batch here."""
    r = subprocess.run(
        [sys.executable, str(BUILDER), "-"],
        input=json.dumps(spec, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        err(f'item "{key}": {last_error(r.stderr)}')
    return r.stdout


def validate(batch: dict[str, Any]) -> None:
    """Every item is a spec the builder accepts; no two share a title in one repo — the
    second create would hit issue-create.sh's duplicate guard on the first one (the
    same comparison: repo, stripped title); and no body names a local ref: "@key" is
    not an issue number, and in a body it renders as an @mention."""
    keys = [item["key"] for item in batch["items"]]
    titles: dict[tuple[str, str], str] = {}
    for item in batch["items"]:
        body = build_body(item["key"], spec_of(item, None))
        title = (item["repo"].lower(), item["summary"].strip())
        if title in titles:
            err(
                f'items "{titles[title]}" and "{item["key"]}" share the title '
                f"{item['summary'].strip()!r} in {item['repo']} — give each its own title"
            )
        titles[title] = item["key"]
        for key in keys:
            if re.search(rf"(?<![A-Za-z0-9_.@-])@{re.escape(key)}(?![A-Za-z0-9_-])", body):
                err(
                    f'item "{item["key"]}": the body mentions "@{key}" — a local ref is not an '
                    "issue number and renders as an @mention; describe the dependency in words, "
                    "the native link carries the number"
                )


# ─── the item scripts ───────────────────────────────────────────────────────
def last_error(stderr: str) -> str:
    """The last {"error": ...} line of a script's stderr, else its last line."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    for line in reversed(lines):
        with contextlib.suppress(ValueError, TypeError, KeyError):
            return str(json.loads(line)["error"])
    return lines[-1] if lines else "no error text"


def call(script: str, args: list[str]) -> tuple[int, Any, str]:
    """Run one item script as its own process; its stderr (warnings, repair commands)
    is passed through. Returns (exit code, parsed stdout or None, error text)."""
    path = Path(os.environ.get("ISSUE_BATCH_SCRIPTS_DIR") or HERE) / script
    try:
        r = subprocess.run([str(path), *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        return 2, None, f"cannot run {path}: {exc}"
    sys.stderr.write(r.stderr)
    try:
        parsed = json.loads(r.stdout) if r.stdout.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return r.returncode, parsed, last_error(r.stderr)


# ─── state ──────────────────────────────────────────────────────────────────
def state_file(batch_id: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "create-issue" / "batches" / f"{batch_id}.json"


def now_utc() -> str:
    """Whole seconds: GitHub's createdAt has none, so an issue created in the second
    the batch started still counts as created at or after it."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def when(text: Any) -> datetime | None:
    """An ISO 8601 time with a zone, else None. "Z" is replaced first: fromisoformat
    reads it only from Python 3.11 on."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def earliest(started_at: Any) -> datetime | None:
    """The earliest createdAt an issue of this batch can carry: started_at less the
    margin for this machine's clock running ahead of GitHub's."""
    start = when(started_at)
    return None if start is None else start - timedelta(seconds=CLOCK_SKEW_S)


def is_record(value: Any) -> bool:
    # CONTRACT: type(...) is int, not isinstance — JSON true/false load as bool, which
    # isinstance(..., int) accepts as an issue number.
    return (
        isinstance(value, dict)
        and isinstance(value.get("url"), str)
        and type(value.get("number")) is int
        and isinstance(value.get("repo"), str)
    )


def is_entry(value: Any) -> bool:
    """A state entry: the issue, the title it was filed under, the board and links
    results an earlier run got for it."""
    return (
        is_record(value)
        and isinstance(value.get("summary"), str)
        and (value.get("board") is None or isinstance(value["board"], str))
        and (value.get("links") is None or isinstance(value["links"], dict))
    )


def load_state(path: Path, ordered: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    """What earlier runs created, and whether this is a rerun: a state file exists only
    once a run got as far as its first create. Never overwritten when unreadable: it
    is the only record of which issues this batch already made."""
    if not path.exists():
        return {"started_at": None, "items": {}}, False
    hint = "fix or remove it — it records what this batch already created"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        err(f"cannot read the state file {path}: {exc}")
    except json.JSONDecodeError as exc:
        err(f"the state file {path} is not valid JSON ({exc}) — {hint}")
    if (
        not isinstance(state, dict)
        or set(state) != {"started_at", "items"}
        or not isinstance(state["items"], dict)
    ):
        err(f'the state file {path} is not {{"started_at", "items": {{...}}}} — {hint}')
    if when(state["started_at"]) is None:
        err(f'the state file {path}: "started_at" is not an ISO 8601 time with a zone — {hint}')
    for key, value in state["items"].items():
        if not is_entry(value):
            err(
                f'the state file {path}: entry "{key}" needs "url", "number", "repo" and '
                f'"summary" — {hint}'
            )
    for item in ordered:
        key, rec = item["key"], state["items"].get(item["key"])
        if rec is None:
            continue
        where = f'item "{key}" is recorded as {rec["repo"]}#{rec["number"]} in {path}'
        fix = "fix the batch or remove the entry (references/batch.md § 5)"
        if rec["repo"].lower() != item["repo"].lower():
            err(f"{where}, but the batch puts it in {item['repo']} — {fix}")
        if rec["summary"].strip() != item["summary"].strip():
            err(
                f"{where} with the title {rec['summary']!r}, but the batch gives it the title "
                f"{item['summary']!r} — {fix}"
            )
    return state, True


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically: a crash leaves the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ─── duplicates ─────────────────────────────────────────────────────────────
def refusal(dup: dict[str, Any], started_at: str | None, user: Any) -> str | None:
    """Why an exact-title duplicate is not adopted, or None when it is. Only a rerun
    adopts (`started_at` is that of an earlier run, None on a first run), and only an
    open issue you opened at or after it (less CLOCK_SKEW_S): the one an earlier run
    created and could not record. Any other is somebody's real issue — or yours from
    before the batch."""
    if started_at is None:
        return "this is the first run of the batch, so the batch did not create it"
    if str(dup.get("state") or "").upper() != "OPEN":
        return f"it is {str(dup.get('state') or 'not open').lower()}"
    author = dup.get("author")
    if not (isinstance(author, str) and isinstance(user, str) and user):
        return "its author or yours is unknown"
    if author.lower() != user.lower():
        return f"it was opened by {author}, not by you ({user})"
    start, created = earliest(started_at), when(dup.get("createdAt"))
    if start is None or created is None or created < start:
        skew = f"more than {CLOCK_SKEW_S} s"
        return f"it was created {skew} before this batch started ({started_at})"
    return None


def maybe_own(dup: dict[str, Any], started_at: str | None, user: Any) -> bool:
    """A refused duplicate that may be the batch's own issue all the same: on a rerun,
    one you opened — closed since, or created while this machine's clock ran ahead by
    more than CLOCK_SKEW_S. Only a person can tell, so the stop says how to record it."""
    author = dup.get("author")
    return (
        started_at is not None
        and isinstance(author, str)
        and isinstance(user, str)
        and bool(user)
        and author.lower() == user.lower()
    )


RENAME = "give the item another title or settle that issue, then rerun"


def how_to_record(key: str, state_file: str) -> str:
    """The way out for a duplicate maybe_own() flags: first recording it, then RENAME."""
    return (
        f"if it is this batch's own issue, add it to {state_file} (`items.{key}`: url, "
        f"number, repo, summary) and rerun; or {RENAME}"
    )


def named(dup: dict[str, Any]) -> str:
    return (
        f"an issue with exactly this title already exists: {dup.get('url')} "
        f"({dup.get('state')}, by {dup.get('author')}, created {dup.get('createdAt')})"
    )


def unknown_outcome(repo: str) -> str:
    return (
        f"the issue may exist: check the newest issues of {repo} first — a rerun adopts an "
        "open issue with this title that you opened since the batch started"
    )


# ─── the remote validation ──────────────────────────────────────────────────
def to_create(ordered: list[dict[str, Any]], recorded: dict[str, Any]) -> list[dict[str, Any]]:
    """The items no run has created or adopted yet — the ones the remote validation
    checks, in the preview and in the run alike."""
    return [item for item in ordered if item["key"] not in recorded]


def dry_runs(items: list[dict[str, Any]], no_board: bool, step: str) -> dict[str, Any]:
    """`issue-create.sh --dry-run` per item, local refs left out: milestone, Status
    and external link targets are checked, and the exact-title search runs, while
    nothing of these items exists. The first failure ends the batch here."""
    flags = ["--dry-run", *(["--no-board"] if no_board else [])]
    outs: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as work:
        for item in items:
            key = item["key"]
            spec_path = Path(work) / f"{key}.json"
            spec_path.write_text(json.dumps(spec_of(item, None), ensure_ascii=False), "utf-8")
            code, out, msg = call("issue-create.sh", [str(spec_path), *flags])
            if code != 0 or not isinstance(out, dict) or not isinstance(out.get("body"), str):
                err(
                    f'item "{key}": {step} failed (issue-create.sh --dry-run, exit {code}): {msg}',
                    code if code in STOP_CODES else 5,
                )
            outs[key] = out
    return outs


# ─── preview ────────────────────────────────────────────────────────────────
def cell(text: Any) -> str:
    return str(text).replace("|", "\\|")


def preview(
    batch: dict[str, Any],
    ordered: list[dict[str, Any]],
    state: dict[str, Any],
    rerun: bool,
    no_board: bool,
) -> int:
    recorded, started = state["items"], state["started_at"] if rerun else None
    outs = dry_runs(to_create(ordered, recorded), no_board, "the preview")
    n = len(ordered)
    rows = [
        f"Batch `{batch['id']}`: {n} issue{'' if n == 1 else 's'}, in creation order.",
        "",
        "| # | Key | Repo | Type | Title | Parent | Blocked by | Milestone | Status |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    bodies, done, dups = [], [], []
    for pos, item in enumerate(ordered, 1):
        key, repo, out = item["key"], item["repo"], outs.get(item["key"], {})
        parent = full_ref(item["parent"], repo) if "parent" in item else "—"
        blocked = ", ".join(full_ref(r, repo) for r in item.get("blocked_by", [])) or "—"
        row = [
            str(pos),
            f"`{key}`",
            repo,
            item.get("type", "Story"),
            cell(item["summary"]),
            parent,
            blocked,
            cell(item.get("milestone") or "—"),
            cell(out.get("status") or "—"),
        ]
        rows.append("| " + " | ".join(row) + " |")
        if key in recorded:  # not validated again: the issue exists, its url is its body
            done.append(f"- `{key}` — {recorded[key]['summary']} — {recorded[key]['url']}")
            continue
        bodies.append(f"# {pos}. `{key}` — {item['summary']}\n\n{out['body']}")
        dup = out.get("duplicate")
        if isinstance(dup, dict):
            num, url = dup.get("number"), dup.get("url")
            if refusal(dup, started, out.get("user")) is None:
                dups.append(f"- `{key}`: would adopt #{num} — {url}")
            else:
                found = f"{dup.get('state')}, {dup.get('author')}"
                dups.append(f"- `{key}`: would stop: duplicate #{num} ({found}) — {url}")
    if done:
        rows += ["", "Already created, only their links are ensured:", *done]
    if dups:
        rows += ["", "An issue with exactly the same title exists:", *dups]
    sys.stdout.write("\n".join(rows) + "\n" + "".join("\n" + body for body in bodies))
    return 0


# ─── the real run ───────────────────────────────────────────────────────────
def run(
    batch: dict[str, Any],
    ordered: list[dict[str, Any]],
    state: dict[str, Any],
    rerun: bool,
    path: Path,
    no_board: bool,
) -> int:
    recorded: dict[str, Any] = state["items"]
    results: list[dict[str, Any]] = []
    worst = 0  # 0, 6 or 7 — 7 wins

    def report() -> None:
        print(
            json.dumps(
                {"id": batch["id"], "items": results, "state_file": str(path)}, ensure_ascii=False
            )
        )

    def stop(
        key: str, code: int, what: str, fix: str = "fix the cause and rerun the same command"
    ) -> NoReturn:
        report()
        err(
            f'item "{key}" stopped the batch: {what} — what was created so far is recorded in '
            f"{path}; {fix} (recorded items are not created again)",
            code if code in STOP_CODES else 5,
        )

    def record(key: str, entry: dict[str, Any], new: bool) -> None:
        recorded[key] = entry
        try:
            save_state(path, state)
        except OSError as exc:
            if new:
                lost = f"{entry['url']} exists but could not be recorded ({exc})"
                stop(key, 5, f"{lost}; {unknown_outcome(entry['repo'])}")
            stop(key, 5, f"its entry could not be updated ({exc})")

    def ensure_links(key: str, entry: dict[str, Any], spec: dict[str, Any]) -> Any:
        """Links of an issue this run did not create; issue-link.sh is idempotent."""
        nonlocal worst
        args = link_args(spec)
        if not args:  # issue-link.sh refuses a call with nothing to link
            return None
        code, out, msg = call("issue-link.sh", [f"{entry['repo']}#{entry['number']}", *args])
        if code not in (0, 7):
            stop(key, code, f"issue-link.sh could not ensure its links (exit {code}): {msg}")
        worst = max(worst, code)
        return out.get("links") if isinstance(out, dict) else None

    def result(key: str, entry: dict[str, Any], **fields: Any) -> None:
        row = {k: entry[k] for k in ("url", "number", "repo")}
        results.append({"key": key, **row, "created": False, "adopted": False, **fields})

    # Everything still to create is validated remotely before the first create, so a
    # bad milestone, Status, link target or a duplicate stops a batch that made nothing.
    todo = to_create(ordered, recorded)
    started = state["started_at"] if rerun else None
    outs = dry_runs(todo, no_board, "the validation before the first create (nothing created)")
    for item in todo:
        key, out = item["key"], outs[item["key"]]
        dup = out.get("duplicate")
        why = refusal(dup, started, out.get("user")) if isinstance(dup, dict) else None
        if isinstance(dup, dict) and why is not None:
            own = maybe_own(dup, started, out.get("user"))
            fix = how_to_record(key, f"the state file {path}") if own else RENAME
            err(f'item "{key}": {named(dup)}; not adopted: {why} — nothing was created; {fix}', 4)
    if todo and not rerun:
        # CONTRACT: started_at is on disk before the first create — a rerun adopts only an
        # issue created at or after it, so it can never be an issue that predates the batch.
        state["started_at"] = now_utc()
        try:
            save_state(path, state)
        except OSError as exc:
            err(f"cannot write the state file {path} ({exc}) — nothing was created", 5)

    flags = ["--no-board"] if no_board else []
    if rerun:  # an issue an earlier run opened and did not record is looked for
        since = earliest(state["started_at"])
        # CONTRACT: rerun ⇒ load_state accepted started_at as an ISO 8601 time with a zone.
        assert since is not None
        flags += [
            "--adopt-since",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since.timestamp())),
        ]
    with tempfile.TemporaryDirectory() as work:
        for item in ordered:
            key = item["key"]
            resolve = {k: f"{v['repo']}#{v['number']}" for k, v in recorded.items()}
            spec = spec_of(item, resolve)
            if key in recorded:  # created or adopted by an earlier run
                entry = recorded[key]
                links = ensure_links(key, entry, spec)
                record(key, {**entry, "links": links}, new=False)
                if entry.get("board") == "failed":
                    worst = max(worst, 6)
                    warn(
                        f'item "{key}": {entry["url"]} is recorded with a failed board step — '
                        f"{BOARD_REPAIR.format(url=entry['url'])}, then set its Status on the board"
                    )
                result(key, entry, verified=None, board=entry.get("board"), links=links)
                continue
            spec_path = Path(work) / f"{key}.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), "utf-8")
            code, out, msg = call("issue-create.sh", [str(spec_path), *flags])
            out = out if isinstance(out, dict) else {}
            rec = {k: out.get(k) for k in ("url", "number", "repo")}
            if code == 4 and out.get("duplicate") is True:
                why = refusal(out, started, out.get("user"))
                if why is not None:
                    what = f"{named(out)}; not adopted: {why}"
                    if maybe_own(out, started, out.get("user")):
                        fix = how_to_record(key, "the state file") + " the same command"
                        stop(key, 4, what, fix)
                    stop(key, 4, what)
                if not is_record(rec):
                    stop(key, 5, f"issue-create.sh reported a duplicate it did not name: {msg}")
                # Adopted: its board step may never have run — `unchecked`, not `added`.
                entry = {**rec, "summary": item["summary"], "board": "unchecked", "links": None}
                record(key, entry, new=True)
                links = ensure_links(key, entry, spec)
                record(key, {**entry, "links": links}, new=False)
                result(key, entry, adopted=True, verified=None, board="unchecked", links=links)
            elif code in (0, 6, 7):
                if not is_record(rec):
                    stop(
                        key,
                        5,
                        f"issue-create.sh exited {code} but printed no url; "
                        + unknown_outcome(item["repo"]),
                    )
                board, links = out.get("board"), out.get("links")
                entry = {**rec, "summary": item["summary"], "board": board, "links": links}
                record(key, entry, new=True)
                worst = max(worst, code)
                verified = out.get("verified")
                result(key, entry, created=True, verified=verified, board=board, links=links)
            else:
                # a 5, or a code that means nothing: the create may have landed
                hint = "" if code in (2, 3, 4) else f"; {unknown_outcome(item['repo'])}"
                stop(key, code, f"issue-create.sh exit {code}: {msg}{hint}")
    report()
    return worst


# ─── compare by shape ───────────────────────────────────────────────────────
def compare_shape(golden_path: str, candidate_path: str) -> int:
    shapes = []
    for what, path in (("golden", golden_path), ("candidate", candidate_path)):
        try:
            shapes.append(shape(load(path, what)))
        except BatchError as exc:
            err(f"{what}: {exc}")
    golden, candidate = shapes
    diffs = [] if len(golden) == len(candidate) else [f"items: {len(golden)} != {len(candidate)}"]
    for pos, (g, c) in enumerate(zip(golden, candidate, strict=False), 1):
        diffs += [f"item {pos}: {f}: {g[f]!r} != {c[f]!r}" for f in g if g[f] != c[f]]
    print(json.dumps({"same_shape": not diffs, "differences": diffs}, ensure_ascii=False))
    return 1 if diffs else 0


def shape(batch: Any) -> list[dict[str, Any]]:
    """What a batch decides, without its wording: per item in creation order, where it
    goes, its links (local refs as positions, so keys may differ), how many parts it
    has and whether each is complete, and whether the kickoff names entry points and
    mines. The type and the number of criteria are the writer's call, not the shape."""
    ordered = structure(batch)
    pos = {item["key"]: n for n, item in enumerate(ordered, 1)}

    def ref(value: str, repo: str) -> str:
        return f"@{pos[value[1:]]}" if value.startswith("@") else full_ref(value, repo).lower()

    out = []
    for item in ordered:
        repo = str(item.get("repo", "")).lower()
        parent, blocked = item.get("parent"), item.get("blocked_by")
        if "parts" in item and not isinstance(item["parts"], list):
            kind = "null" if item["parts"] is None else type(item["parts"]).__name__
            raise BatchError(f'item "{item["key"]}": "parts" must be a list, got {kind}')
        parts = [p if isinstance(p, dict) else {} for p in item.get("parts", [])]
        kickoff = item.get("kickoff")
        kickoff = kickoff if isinstance(kickoff, dict) else {}
        out.append(
            {
                "repo": repo,
                "parent": ref(parent, repo) if isinstance(parent, str) else None,
                "blocked_by": sorted(
                    ref(r, repo)
                    for r in (blocked if isinstance(blocked, list) else [])
                    if isinstance(r, str)
                ),
                "parts": len(parts),
                "parts (has AC)": [
                    isinstance(p.get("acceptance_criteria"), list)
                    and bool(p["acceptance_criteria"])
                    for p in parts
                ],
                "parts (has done_when)": [
                    isinstance(p.get("done_when"), str) and bool(p["done_when"].strip())
                    for p in parts
                ],
                "kickoff read_first": bool(kickoff.get("read_first")),
                "kickoff mines": bool(kickoff.get("mines")),
            }
        )
    return out


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if args[:1] == ["--compare-shape"]:
        if len(args) != 3:
            err(USAGE)
        return compare_shape(args[1], args[2])
    seam = os.environ.get("ISSUE_BATCH_SCRIPTS_DIR")
    if seam:
        warn(f"ISSUE_BATCH_SCRIPTS_DIR is set: the item scripts run from {seam}, not {HERE}")
    dry = "--dry-run" in args
    no_board = "--no-board" in args
    paths = [a for a in args if a not in ("--dry-run", "--no-board")]
    bad = next((a for a in paths if a.startswith("-")), None)
    if bad is not None:
        err(f"unknown argument: {bad} — {USAGE}")
    if len(paths) != 1:
        err(USAGE)

    batch = load(paths[0], "batch")
    try:
        ordered = structure(batch)
    except BatchError as exc:
        err(str(exc))
    if no_board:
        with_status = next((i["key"] for i in ordered if "status" in i), None)
        if with_status is not None:
            err(f'item "{with_status}" sets "status" but --no-board skips the board — drop one')
    validate(batch)
    path = state_file(batch["id"])
    state, rerun = load_state(path, ordered)
    if dry:
        return preview(batch, ordered, state, rerun, no_board)
    return run(batch, ordered, state, rerun, path, no_board)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
