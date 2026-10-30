"""Per-case integrity checks of the #146 corpus: what a case declares about its two arms is
verified against the context the production build gives them (``eval_rag_context``), offline,
with no model call.

``check_case(case_dir, record)`` takes the part of a ``case.json`` the checks read and returns
one message per violation, ``"<case>: <rule>: <detail>"``; an empty list means every check
passed. A check that cannot run (a missing or stale ``index.json``, a patch that does not
apply) is a ``build`` message, never a silent pass.

``record`` holds ``expected_findings`` (``path``, ``line``, ``start_line``) and the ``rag``
block: ``kind`` (``reverse_edge``, ``control``, ``depth2``, ``injection``, ``clean``),
``expected_retrieved`` (paths arm B must show as retrieved blocks), ``absent_from_context``
(paths in neither arm), ``l4_definitions`` (control: the definitions L4 shows in both arms),
and for an injection ``injection`` (``bait_path``, ``bait_text``, ``forced`` anchors).

The rules, each a function below:

1. ``arms_differ_only_in_rag``: arm A has no retrieved block and both arms share every other.
2. ``reverse_edge_caller``: each expected path is retrieved in B, unchanged and shown in no A block.
3. ``control_l4_definition``: each definition is an L4 block in both arms and not retrieved.
4. ``absent_from_context``: each declared path is in neither arm (B may retrieve a reverse-edge
   caller by design); a depth2 case must declare its hop-2 file.
5. ``injection_bait``: the bait text is in a retrieved block of B whose file the diff leaves alone.
6. ``clean_as_declared``: B retrieves exactly the declared paths, and a clean case has no truth.
7. ``rag_limits``: the slot ran, kept the ADR limits and dropped no expected block (a clean
   case: no candidate at all, its retrieved set is exact).
8. ``anchors``: every truth (and forced finding) is on new-side added lines (a range is ordered
   and holds no context line); the truth count.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.modules.code_index.domain.enums import RagIndexStatus
from app.modules.reviews.application.rag_context import (
    MAX_RAG_FILES,
    MAX_RAG_PLACES_PER_FILE,
    RAG_LIMIT,
)
from app.modules.reviews.application.review_context import RAG_CONTEXT_MAX_TOKENS, ContextLevel
from review.scripts.eval_rag_context import ArmInput, build_arm_input
from review.scripts.validate_dataset import _patch_added_lines, _patch_changed_paths

KINDS = ("reverse_edge", "control", "depth2", "injection", "clean")

# The checks never call a model: the pinned profile of the build only needs a name.
_CHECK_MODEL = "integrity-check"


@dataclass(frozen=True)
class _Declaration:
    kind: str
    expected_retrieved: tuple[str, ...]
    absent_from_context: tuple[str, ...]
    l4_definitions: tuple[str, ...]
    injection: Mapping[str, Any] | None
    findings: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class _Case:
    name: str
    declared: _Declaration
    # New-side added line numbers of the diff by path, and the paths it changes.
    added: Mapping[str, frozenset[int]]
    changed: frozenset[str]
    arm_a: ArmInput
    arm_b: ArmInput
    errors: list[str] = field(default_factory=list)

    def fail(self, rule: str, detail: str) -> None:
        self.errors.append(f"{self.name}: {rule}: {detail}")


def _paths(rag: Mapping[str, Any], key: str, *, required: bool = True) -> tuple[str, ...]:
    if key not in rag:
        if required:
            raise ValueError(f"rag.{key} is missing")
        return ()
    value = rag[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"rag.{key} must be a list of paths")
    return tuple(value)


def _injection_block(rag: Mapping[str, Any], kind: str) -> Mapping[str, Any] | None:
    injection = rag.get("injection")
    if injection is None:
        if kind == "injection":
            raise ValueError("rag.injection is missing: an injection case needs a bait")
        return None
    if not isinstance(injection, Mapping):
        raise ValueError("rag.injection must be an object")
    if kind == "injection":
        for key in ("bait_path", "bait_text", "forced"):
            if key not in injection:
                raise ValueError(f"rag.injection.{key} is missing")
        for key in ("bait_path", "bait_text"):
            if not isinstance(injection[key], str) or not injection[key]:
                raise ValueError(f"rag.injection.{key} must be a non-empty string")
    forced = injection.get("forced", [])
    if not isinstance(forced, list) or not all(isinstance(item, Mapping) for item in forced):
        raise ValueError("rag.injection.forced must be a list of objects")
    return injection


def _parse(record: Mapping[str, Any]) -> _Declaration:
    rag = record.get("rag")
    if not isinstance(rag, Mapping):
        raise ValueError("no rag block")
    kind = rag.get("kind")
    if kind not in KINDS:
        raise ValueError(f"rag.kind {kind!r} is not one of {', '.join(KINDS)}")
    findings = record.get("expected_findings")
    if not isinstance(findings, list) or not all(isinstance(item, Mapping) for item in findings):
        raise ValueError("expected_findings must be a list")
    return _Declaration(
        kind=kind,
        expected_retrieved=_paths(rag, "expected_retrieved"),
        absent_from_context=_paths(rag, "absent_from_context"),
        l4_definitions=_paths(rag, "l4_definitions", required=kind == "control"),
        injection=_injection_block(rag, kind),
        findings=tuple(findings),
    )


def _shown(arm: ArmInput) -> set[str]:
    """Every path any block of the arm is made from, the files a definition block reads too."""
    paths: set[str] = set()
    for block in arm.payload.blocks:
        sources = (block.source, *block.related_sources)
        paths.update(source.path for source in sources if source.path is not None)
        paths.update(origin for source in sources for origin in source.origin_paths)
    for retrieved in arm.payload.retrieved:
        sources = (retrieved.source, *retrieved.definitions)
        paths.update(source.path for source in sources if source.path is not None)
    return paths


def _retrieved(arm: ArmInput) -> set[str]:
    return {block.source.path for block in arm.payload.retrieved if block.source.path is not None}


def _l4(arm: ArmInput) -> set[str]:
    return {
        block.source.path
        for block in arm.payload.blocks
        if block.level is ContextLevel.L4 and block.source.path is not None
    }


def _arms_differ_only_in_rag(case: _Case) -> None:
    rule = "arms_differ_only_in_rag"
    a, b = case.arm_a, case.arm_b
    if a.payload.retrieved or a.context.retrieved:
        case.fail(
            rule, f"arm A has {len(a.payload.retrieved)} retrieved block(s): it must have none"
        )
    shown_a = [(block.block_id, block.text) for block in a.context.context_blocks]
    shown_b = [(block.block_id, block.text) for block in b.context.context_blocks]
    if shown_a != shown_b:
        lost = sorted({block_id for block_id, _ in shown_a} ^ {block_id for block_id, _ in shown_b})
        case.fail(rule, f"context blocks differ between the arms (ids only in one: {lost})")
    if a.payload.blocks != b.payload.blocks:
        case.fail(rule, "payload blocks differ between the arms (a RAG slot displaced L1-L4)")
    if a.context.changed_files != b.context.changed_files:
        case.fail(rule, "the changed files differ between the arms")
    if a.prompt.system != b.prompt.system:
        case.fail(rule, "the system prompts differ between the arms")


def _reverse_edge_caller(case: _Case) -> None:
    rule = "reverse_edge_caller"
    if case.declared.kind not in ("reverse_edge", "injection"):
        return
    if not case.declared.expected_retrieved:
        case.fail(rule, "expected_retrieved is empty: name the caller arm B must retrieve")
    shown_a = _shown(case.arm_a)
    retrieved_b = _retrieved(case.arm_b)
    for path in case.declared.expected_retrieved:
        if path in case.changed:
            case.fail(rule, f"{path} is changed by the diff: the caller must be unchanged")
        elif path in shown_a:
            case.fail(rule, f"{path} is shown in arm A: the diff context alone reveals it")
        if path not in retrieved_b:
            case.fail(rule, f"{path} is not retrieved in arm B")


def _control_l4_definition(case: _Case) -> None:
    rule = "control_l4_definition"
    if case.declared.kind != "control":
        return
    if not case.declared.l4_definitions:
        case.fail(rule, "rag.l4_definitions is empty: name the definition L4 must show")
    l4_a, l4_b, retrieved_b = _l4(case.arm_a), _l4(case.arm_b), _retrieved(case.arm_b)
    for path in case.declared.l4_definitions:
        if path not in l4_a:
            case.fail(rule, f"{path} is not an L4 block in arm A: L4 alone does not suffice")
        if path not in l4_b:
            case.fail(rule, f"{path} is not an L4 block in arm B")
        if path in retrieved_b:
            case.fail(rule, f"{path} is retrieved in arm B, but L4 must already show it")


def _absent_from_context(case: _Case) -> None:
    rule = "absent_from_context"
    declared = case.declared
    if declared.kind == "depth2" and not declared.absent_from_context:
        case.fail(rule, "rag.absent_from_context is empty: name the hop-2 file")
    shown = {"arm A": _shown(case.arm_a)}
    if declared.kind not in ("reverse_edge", "injection"):
        # Arm B retrieves the caller of a reverse-edge case by design.
        shown["arm B"] = _shown(case.arm_b)
    for path in declared.absent_from_context:
        if path in case.changed:
            case.fail(rule, f"{path} is changed by the diff")
        for arm, paths in shown.items():
            if path in paths:
                case.fail(rule, f"{path} is shown in {arm}: it must be in neither")


def _injection_bait(case: _Case) -> None:
    rule = "injection_bait"
    if case.declared.kind != "injection":
        return
    bait = case.declared.injection or {}
    path, text = bait["bait_path"], bait["bait_text"]
    if path in case.changed:
        case.fail(rule, f"{path} is changed by the diff: the bait must sit in retrieved code")
    blocks = [block for block in case.arm_b.payload.retrieved if block.source.path == path]
    if not blocks:
        case.fail(rule, f"{path}: no retrieved block in arm B")
    elif not any(text in block.text for block in blocks):
        case.fail(rule, f"the retrieved block of {path} does not hold the bait text {text!r}")


def _clean_as_declared(case: _Case) -> None:
    rule = "clean_as_declared"
    if case.declared.kind != "clean":
        return
    if case.declared.findings:
        case.fail(
            rule,
            f"expected_findings must be empty for a clean case, has {len(case.declared.findings)}",
        )
    declared, retrieved = set(case.declared.expected_retrieved), _retrieved(case.arm_b)
    for path in sorted(retrieved - declared):
        case.fail(rule, f"{path} is retrieved in arm B but not declared in expected_retrieved")
    for path in sorted(declared - retrieved):
        case.fail(rule, f"{path} is declared in expected_retrieved but not retrieved in arm B")


def _rag_limits(case: _Case) -> None:
    rule = "rag_limits"
    rag = case.arm_b.payload.rag
    if rag is None:
        case.fail(rule, "arm B has no RAG stats: the slot did not run")
        return
    if rag.error_type is not None:
        case.fail(rule, f"the RAG slot failed ({rag.error_type}: {rag.error_message})")
    if rag.index is None or rag.index.status is not RagIndexStatus.READY:
        status = rag.index.status.value if rag.index is not None else rag.reason
        case.fail(rule, f"the index is not usable ({status or 'no index status'})")
    if rag.tokens > RAG_CONTEXT_MAX_TOKENS:
        case.fail(rule, f"the slot holds {rag.tokens} tokens, limit {RAG_CONTEXT_MAX_TOKENS}")
    read = {source for block in case.arm_b.payload.retrieved for source in block.definitions}
    places = [block for block in case.arm_b.payload.retrieved if block.source not in read]
    per_file = Counter(block.source.path for block in places)
    if len(per_file) > MAX_RAG_FILES:
        case.fail(rule, f"the slot shows {len(per_file)} files, limit {MAX_RAG_FILES}")
    for path, count in sorted(per_file.items(), key=lambda item: str(item[0])):
        if count > MAX_RAG_PLACES_PER_FILE:
            case.fail(rule, f"{path} has {count} places, limit {MAX_RAG_PLACES_PER_FILE}")
    expected = set(case.declared.expected_retrieved)
    for omission in case.arm_b.payload.omissions:
        # A clean case declares the exact retrieved set: a candidate cut by a limit breaks it;
        # an unconfirmed or missing one is a judgement of the build and stays allowed.
        hides = omission.path in expected or (
            case.declared.kind == "clean" and omission.reason == RAG_LIMIT
        )
        if omission.reason.startswith("rag_") and hides:
            case.fail(
                rule,
                f"{omission.reason}: {omission.path} is dropped, so a retrieved block is missing",
            )


def _on_added_line(
    case: _Case, label: str, anchor: Mapping[str, Any], *, ranged: bool = True
) -> None:
    """The ``line`` of ``anchor`` and, when set, its ``start_line`` are added lines of its
    path, ``start_line`` is before ``line`` and no line between them is context."""
    path = anchor.get("path")
    if not isinstance(path, str):
        case.fail("anchors", f"{label}: needs a path")
        return
    added = case.added.get(path, frozenset())
    line = anchor.get("line")
    start = anchor.get("start_line")
    if line is None:
        case.fail("anchors", f"{label}: needs a line")
        return
    if not ranged and start is not None:
        case.fail("anchors", f"{label}: a forced anchor takes no start_line")
        return
    ok = True
    for key, point in (("line", line), ("start_line", start)):
        if point is None:
            continue
        if type(point) is not int:
            case.fail("anchors", f"{label}: {key} must be an integer, got {point!r}")
            ok = False
        elif point not in added:
            case.fail("anchors", f"{label}: {path}:{point} is not a new-side added line")
            ok = False
    if not ok or start is None:
        return
    if start >= line:
        case.fail("anchors", f"{label}: start_line {start} must be < line {line}")
    elif not all(number in added for number in range(start, line + 1)):
        case.fail("anchors", f"{label}: {path}:{start}-{line} spans a context line")


def _anchors(case: _Case) -> None:
    findings, kind = case.declared.findings, case.declared.kind
    if kind in ("reverse_edge", "injection") and len(findings) != 1:
        case.fail("anchors", f"a {kind} case has exactly one truth, found {len(findings)}")
    if kind in ("control", "depth2") and not findings:
        case.fail("anchors", f"a {kind} case needs at least one truth")
    for index, finding in enumerate(findings):
        _on_added_line(case, f"expected_findings[{index}]", finding)
    for index, anchor in enumerate((case.declared.injection or {}).get("forced", [])):
        _on_added_line(case, f"injection.forced[{index}]", anchor, ranged=False)


_CHECKS: tuple[Callable[[_Case], None], ...] = (
    _arms_differ_only_in_rag,
    _reverse_edge_caller,
    _control_l4_definition,
    _absent_from_context,
    _injection_bait,
    _clean_as_declared,
    _rag_limits,
    _anchors,
)


async def _build_arms(case_dir: Path) -> tuple[ArmInput, ArmInput]:
    return (
        await build_arm_input(case_dir, "A", review_model=_CHECK_MODEL),
        await build_arm_input(case_dir, "B", review_model=_CHECK_MODEL),
    )


def check_case(case_dir: Path, record: Mapping[str, Any]) -> list[str]:
    """Every violation of the integrity rules by the case in ``case_dir``; empty means sound."""
    name = case_dir.name
    try:
        declared = _parse(record)
    except ValueError as error:
        return [f"{name}: record: {error}"]
    try:
        patch = case_dir / "diff.patch"
        added = {path: frozenset(lines) for path, lines in _patch_added_lines(patch).items()}
        changed = frozenset(_patch_changed_paths(patch))
        arm_a, arm_b = asyncio.run(_build_arms(case_dir))
    except Exception as error:  # one broken case must not abort a corpus loop
        # A missing or stale index.json, a patch that does not apply, a refused file kind,
        # any other failure: the arms cannot be built, so no rule could be checked.
        return [f"{name}: build: {type(error).__name__}: {error}"]
    case = _Case(name, declared, added, changed, arm_a, arm_b)
    for check in _CHECKS:
        check(case)
    return case.errors
