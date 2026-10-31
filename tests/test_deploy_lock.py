"""The VPS deploy lock ``staging-deploy`` (AC5 of #308): one job at a time, in order, none dropped.

Its members are the staging deploy and promotion jobs of ci-cd.yml and the Rollback staging
workflow (and Deploy prod once it exists). With the default ``queue: single`` GitHub cancels the
pending member when the next one joins the group, so a merge would replace a pending rollback or
prod deploy. ``queue: max`` keeps up to 100 pending members in FIFO order; it is never mixed inside
the group, and next to ``cancel-in-progress: true`` it is a validation error. Text contracts on the
workflow files, like the other workflow tests.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
# A `group:` line naming the lock, bare or quoted in an expression; `staging-deploy-x` is not it.
LOCK_GROUP = re.compile(r"^(?P<indent> *)group: .*(?<![\w-])staging-deploy(?![\w-])", re.MULTILINE)


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _lock_members() -> list[tuple[str, int, list[str]]]:
    """(file, line, keys next to the group line) for every concurrency group naming the lock."""
    members = []
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for match in LOCK_GROUP.finditer(text):
            index = text.count("\n", 0, match.start())
            indent = len(match.group("indent"))
            siblings = []
            for line in lines[index + 1 :]:
                key = line.lstrip(" ")
                if key.startswith("#"):
                    continue
                if not key or len(line) - len(key) != indent:
                    break
                siblings.append(key)
            members.append((path.name, index + 1, siblings))
    return members


def test_every_staging_deploy_member_queues_instead_of_replacing() -> None:
    members = _lock_members()
    files = [name for name, _, _ in members]
    # Not vacuous: deploy-staging and promote-staging in ci-cd.yml, the whole Rollback staging run.
    assert files.count("ci-cd.yml") == 2, members
    assert files.count("rollback.yml") == 1, members

    unqueued = [
        (name, line, siblings)
        for name, line, siblings in members
        if not {"cancel-in-progress: false", "queue: max"} <= set(siblings)
    ]
    assert unqueued == [], "each staging-deploy member needs cancel-in-progress: false, queue: max"


def test_the_lock_is_never_named_in_the_short_concurrency_form() -> None:
    # `concurrency: staging-deploy` cannot carry `queue: max`, so it would default to replacing.
    short = re.compile(r"^ *concurrency: .*(?<![\w-])staging-deploy(?![\w-])", re.MULTILINE)
    for path in _workflow_files():
        assert short.search(path.read_text(encoding="utf-8")) is None, path.name
