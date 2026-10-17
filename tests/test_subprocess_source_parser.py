"""Parser supervisor: limits, and one bad file never fails the rest of the batch."""

from __future__ import annotations

import asyncio

import pytest

from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstFailureReason,
    AstLanguage,
    ParseRequest,
)
from app.modules.reviews.infrastructure import subprocess_source_parser
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser


def _parse(*requests: ParseRequest) -> dict[str, AstFacts | AstFailureReason]:
    async def run() -> dict[str, AstFacts | AstFailureReason]:
        parser = SubprocessSourceParser()
        try:
            return await parser.parse_many(requests)
        finally:
            await parser.aclose()

    return asyncio.run(run())


def _python(key: str, source: bytes = b"def f():\n    return 1\n") -> ParseRequest:
    return ParseRequest(key, AstLanguage.PYTHON, source)


def test_real_worker_parses_every_language_in_one_batch() -> None:
    results = _parse(
        _python("a.py"),
        ParseRequest("a.go", AstLanguage.GO, b"package a\nfunc F() {}\n"),
        ParseRequest("a.ts", AstLanguage.TYPESCRIPT, b"export function g(): void {}\n"),
        ParseRequest("a.jsx", AstLanguage.JAVASCRIPT, b"export const C = () => <div/>;\n"),
        ParseRequest("a.tsx", AstLanguage.TSX, b"export const D = (): JSX.Element => <div/>;\n"),
    )

    names = {
        key: [symbol.name for symbol in value.symbols]
        for key, value in results.items()
        if isinstance(value, AstFacts)
    }
    assert names == {"a.py": ["f"], "a.go": ["F"], "a.ts": ["g"], "a.jsx": ["C"], "a.tsx": ["D"]}


def test_oversized_and_undecodable_sources_never_reach_the_worker() -> None:
    results = _parse(
        _python("limit.py", b"x = 1\n" + b"#" * 262_138),
        _python("big.py", b"#" * 262_145),
        _python("bad.py", b"\xff\xfe"),
        _python("ok.py"),
    )

    assert isinstance(results["limit.py"], AstFacts)
    assert results["big.py"] is AstFailureReason.SOURCE_TOO_LARGE
    assert results["bad.py"] is AstFailureReason.SOURCE_DECODE_FAILED
    assert isinstance(results["ok.py"], AstFacts)


def test_worker_gets_no_environment_of_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "secret")
    captured: dict[str, object] = {}
    original = asyncio.create_subprocess_exec

    async def spy(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        captured.update(kwargs)
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    assert isinstance(_parse(_python("a.py"))["a.py"], AstFacts)
    env = captured["env"]
    assert isinstance(env, dict)
    assert set(env) == {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}


def test_hung_worker_is_killed_and_later_files_are_still_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess_source_parser, "_WORKER_MODULE", "tests.ast_workers.hanging")
    monkeypatch.setattr(subprocess_source_parser, "_WALL_SLACK_SECONDS", -0.7)
    monkeypatch.setattr(subprocess_source_parser, "_STARTUP_SECONDS", 10.0)

    results = _parse(_python("first.py"), _python("hangs.py"), _python("after.py"))

    assert isinstance(results["first.py"], AstFacts)
    assert results["hangs.py"] is AstFailureReason.AST_TIMEOUT
    # A fresh worker answers the first request it receives.
    assert isinstance(results["after.py"], AstFacts)


def test_worker_that_dies_marks_the_file_as_a_resource_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess_source_parser, "_WORKER_MODULE", "tests.ast_workers.dying")

    results = _parse(_python("a.py"), _python("b.py"))

    assert results == {
        "a.py": AstFailureReason.AST_RESOURCE_LIMIT,
        "b.py": AstFailureReason.AST_RESOURCE_LIMIT,
    }


def test_cpu_budget_of_the_attempt_stops_further_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess_source_parser, "_WORKER_MODULE", "tests.ast_workers.greedy")

    results = _parse(_python("a.py"), _python("b.py"), _python("c.py"))

    # Two files spend six of the five allowed CPU seconds; the third is not parsed.
    assert results == {
        "a.py": AstFailureReason.AST_FAILED,
        "b.py": AstFailureReason.AST_FAILED,
        "c.py": AstFailureReason.AST_TIMEOUT,
    }


def test_no_attempt_budget_leaves_only_the_per_file_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess_source_parser, "_WORKER_MODULE", "tests.ast_workers.greedy")

    async def run() -> dict[str, AstFacts | AstFailureReason]:
        parser = SubprocessSourceParser(attempt_cpu_seconds=None)
        try:
            return await parser.parse_many((_python("a.py"), _python("b.py"), _python("c.py")))
        finally:
            await parser.aclose()

    # Nine CPU seconds in total: without the attempt budget every file is still parsed.
    assert asyncio.run(run()) == {
        "a.py": AstFailureReason.AST_FAILED,
        "b.py": AstFailureReason.AST_FAILED,
        "c.py": AstFailureReason.AST_FAILED,
    }
