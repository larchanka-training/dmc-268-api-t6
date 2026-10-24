#!/usr/bin/env python3
"""issue-batch.py — create a batch of related issues once, in dependency order, and
resume an interrupted batch without creating anything twice.

A batch is riskier than one issue in one specific way: it can stop halfway. So the
whole batch is validated before the first create; parents and blockers are created
before what depends on them, so a local ref `@key` can be replaced by the issue it
names; every result is written to a state file before the next item starts; and a
rerun of the same command is the normal way to resume — an item already in the state
file is not created again, only its links are ensured, and an item whose create hits
issue-create.sh's exact-title duplicate guard adopts that issue instead of creating a
second one. Each item is created by issue-create.sh as a separate process, so the
single-issue guarantees (attribution guard, validation, read-back, board, links) hold
per item. Details: references/batch.md.

Usage
-----
    issue-batch.py <batch.json> [--dry-run] [--no-board]
    issue-batch.py --compare-shape <golden.json> <candidate.json>

Batch file: {"id": "<slug>", "items": [{"key": "<slug>", ...spec keys...}, ...]};
slugs match [a-z0-9][a-z0-9-]*; `parent` / `blocked_by` may name another item as
"@key".

Output
------
--dry-run: the preview on stdout — a table of the items in creation order, then each
  body under a heading with its key; every item also runs `issue-create.sh --dry-run`
  (local refs left out), so a bad milestone, Status or link target fails the preview.
  Nothing is created and no state is written.
real run: {"id", "items": [{"key", "url", "number", "repo", "created", "adopted",
  "verified", "board", "links"}], "state_file"} on stdout — also when an item stops
  the batch (then it lists the items done before it).
--compare-shape: {"same_shape", "differences"} — item count, and per item in creation
  order: repo, type, the parent / blocked_by graph (local refs as positions, so keys
  may differ), AC per part, done_when per part, kickoff read_first and mines. Titles
  and wording are not compared.
Errors: {"error": "..."} on stderr; the item scripts' stderr is passed through.

Exit
----
0 every item done · 2 usage or invalid batch (nothing ran), or an item exit 2 ·
3 auth · 4 an item exit 4 that is not a duplicate · 5 network/unknown, or a create
whose result could not be read · 6 an issue created but its board step failed ·
7 a native link failed (wins over 6). 2-5 stop the batch: the state file keeps what
was created, rerun the same command. 6 and 7 do not stop it. --compare-shape: 0 same
shape, 1 different, 2 a file is not a batch.

State: ${XDG_CACHE_HOME:-$HOME/.cache}/create-issue/batches/<id>.json —
{key: {"url", "number", "repo"}}, written atomically after each item.
Test seam: ISSUE_BATCH_SCRIPTS_DIR — the directory issue-create.sh and issue-link.sh
are run from (default: this script's directory); the builder always runs from here.
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


class BatchError(Exception):
    """The batch file is not a valid batch."""


def err(msg: str, code: int = 2) -> NoReturn:
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(code)


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
    second create would hit issue-create.sh's duplicate guard and adopt the first
    issue (the same comparison: repo, stripped title); and no body names a local ref:
    "@key" is not an issue number, and in a body it renders as an @mention."""
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


def is_record(value: Any) -> bool:
    # CONTRACT: type(...) is int, not isinstance — JSON true/false load as bool, which
    # isinstance(..., int) accepts as an issue number.
    return (
        isinstance(value, dict)
        and isinstance(value.get("url"), str)
        and type(value.get("number")) is int
        and isinstance(value.get("repo"), str)
    )


def load_state(path: Path, ordered: list[dict[str, Any]]) -> dict[str, Any]:
    """What earlier runs created. Never overwritten when unreadable: it is the only
    record of which issues this batch already made."""
    if not path.exists():
        return {}
    hint = "fix or remove it — it records what this batch already created"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        err(f"cannot read the state file {path}: {exc}")
    except json.JSONDecodeError as exc:
        err(f"the state file {path} is not valid JSON ({exc}) — {hint}")
    if not isinstance(state, dict):
        err(f"the state file {path} is not an object — {hint}")
    for key, value in state.items():
        if not is_record(value):
            err(f'the state file {path}: entry "{key}" needs "url", "number" and "repo" — {hint}')
    for item in ordered:
        rec = state.get(item["key"])
        if rec and rec["repo"].lower() != str(item.get("repo", "")).lower():
            err(
                f'item "{item["key"]}" is recorded as {rec["repo"]}#{rec["number"]} in {path}, '
                f"but the batch puts it in {item.get('repo')} — fix the batch or remove the entry"
            )
    return state


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


# ─── preview ────────────────────────────────────────────────────────────────
def cell(text: Any) -> str:
    return str(text).replace("|", "\\|")


def preview(
    batch: dict[str, Any], ordered: list[dict[str, Any]], state: dict[str, Any], no_board: bool
) -> int:
    flags = ["--dry-run", *(["--no-board"] if no_board else [])]
    n = len(ordered)
    rows = [
        f"Batch `{batch['id']}`: {n} issue{'' if n == 1 else 's'}, in creation order.",
        "",
        "| # | Key | Repo | Type | Title | Parent | Blocked by | Milestone | Status |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    bodies = []
    with tempfile.TemporaryDirectory() as work:
        for pos, item in enumerate(ordered, 1):
            key, repo = item["key"], item["repo"]
            spec_path = Path(work) / f"{key}.json"
            spec_path.write_text(json.dumps(spec_of(item, None), ensure_ascii=False), "utf-8")
            code, out, msg = call("issue-create.sh", [str(spec_path), *flags])
            if code != 0 or not isinstance(out, dict) or not isinstance(out.get("body"), str):
                err(
                    f'item "{key}": the preview failed (issue-create.sh --dry-run, exit {code}): '
                    f"{msg}",
                    code if code in STOP_CODES else 5,
                )
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
            bodies.append(f"# {pos}. `{key}` — {item['summary']}\n\n{out['body']}")
    done = [f"`{i['key']}` → {state[i['key']]['url']}" for i in ordered if i["key"] in state]
    if done:
        rows += ["", "Already created, only their links are ensured: " + ", ".join(done)]
    sys.stdout.write("\n".join(rows) + "\n" + "".join("\n" + body for body in bodies))
    return 0


# ─── the real run ───────────────────────────────────────────────────────────
def run(
    batch: dict[str, Any],
    ordered: list[dict[str, Any]],
    state: dict[str, Any],
    path: Path,
    no_board: bool,
) -> int:
    results: list[dict[str, Any]] = []
    worst = 0  # 0, 6 or 7 — 7 wins

    def report() -> None:
        print(
            json.dumps(
                {"id": batch["id"], "items": results, "state_file": str(path)}, ensure_ascii=False
            )
        )

    def stop(key: str, code: int, what: str) -> NoReturn:
        report()
        err(
            f'item "{key}" stopped the batch: {what} — what was created so far is recorded in '
            f"{path}; fix the cause and rerun the same command (recorded items are not "
            "created again)",
            code if code in STOP_CODES else 5,
        )

    def record(key: str, rec: dict[str, Any]) -> None:
        state[key] = rec
        try:
            save_state(path, state)
        except OSError as exc:
            stop(
                key,
                5,
                f"{rec['url']} exists but could not be recorded ({exc}); a rerun "
                "adopts it through the duplicate guard",
            )

    def ensure_links(key: str, rec: dict[str, Any], spec: dict[str, Any]) -> Any:
        """Links of an issue this run did not create; issue-link.sh is idempotent."""
        nonlocal worst
        args = link_args(spec)
        if not args:  # issue-link.sh refuses a call with nothing to link
            return None
        code, out, msg = call("issue-link.sh", [f"{rec['repo']}#{rec['number']}", *args])
        if code not in (0, 7):
            stop(key, code, f"issue-link.sh could not ensure its links (exit {code}): {msg}")
        worst = max(worst, code)
        return out.get("links") if isinstance(out, dict) else None

    flags = ["--no-board"] if no_board else []
    with tempfile.TemporaryDirectory() as work:
        for item in ordered:
            key = item["key"]
            resolve = {k: f"{v['repo']}#{v['number']}" for k, v in state.items()}
            spec = spec_of(item, resolve)
            row: dict[str, Any] = {"created": False, "adopted": False, "verified": None}
            row.update(board=None, links=None)
            if key in state:  # created or adopted by an earlier run
                row["links"] = ensure_links(key, state[key], spec)
                results.append({"key": key, **state[key], **row})
                continue
            spec_path = Path(work) / f"{key}.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), "utf-8")
            code, out, msg = call("issue-create.sh", [str(spec_path), *flags])
            out = out if isinstance(out, dict) else {}
            rec = {k: out.get(k) for k in ("url", "number", "repo")}
            if code == 4 and out.get("duplicate") is True:
                # The exact-title duplicate guard named an existing issue: adopt it.
                if not is_record(rec):
                    stop(key, 5, f"issue-create.sh reported a duplicate it did not name: {msg}")
                record(key, rec)
                row.update(adopted=True, links=ensure_links(key, rec, spec))
            elif code in (0, 6, 7):
                if not is_record(rec):
                    stop(
                        key,
                        5,
                        f"issue-create.sh exited {code} but printed no url — look at "
                        f"{item['repo']} by hand; a rerun adopts an issue with this title",
                    )
                record(key, rec)
                row.update(created=True, verified=out.get("verified"), board=out.get("board"))
                row["links"] = out.get("links")
                worst = max(worst, code)
            else:
                stop(key, code, f"issue-create.sh exit {code}: {msg}")
            results.append({"key": key, **rec, **row})
    report()
    return worst


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
    goes, its type, its links (local refs as positions, so keys may differ), its parts
    and whether the kickoff names entry points and mines."""
    ordered = structure(batch)
    pos = {item["key"]: n for n, item in enumerate(ordered, 1)}

    def ref(value: str, repo: str) -> str:
        return f"@{pos[value[1:]]}" if value.startswith("@") else full_ref(value, repo).lower()

    out = []
    for item in ordered:
        repo = str(item.get("repo", "")).lower()
        parent, blocked = item.get("parent"), item.get("blocked_by")
        parts = [p if isinstance(p, dict) else {} for p in item.get("parts") or []]
        kickoff = item.get("kickoff")
        kickoff = kickoff if isinstance(kickoff, dict) else {}
        ac = [p.get("acceptance_criteria") for p in parts]
        out.append(
            {
                "repo": repo,
                "type": item.get("type", "Story"),
                "parent": ref(parent, repo) if isinstance(parent, str) else None,
                "blocked_by": sorted(
                    ref(r, repo)
                    for r in (blocked if isinstance(blocked, list) else [])
                    if isinstance(r, str)
                ),
                "parts (AC per part)": [len(a) if isinstance(a, list) else 0 for a in ac],
                "parts (done_when)": [
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
    state = load_state(path, ordered)
    if dry:
        return preview(batch, ordered, state, no_board)
    return run(batch, ordered, state, path, no_board)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
