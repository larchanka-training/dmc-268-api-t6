"""Run the tree-sitter worker and keep a bad file from failing the others (spec §3.4)."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

from app.modules.reviews.application.ast_symbols import (
    MAX_PARSE_CPU_SECONDS_PER_ATTEMPT,
    MAX_PARSE_CPU_SECONDS_PER_FILE,
    MAX_SOURCE_BYTES,
    AstFacts,
    AstFailureReason,
    ParseRequest,
    SuggestionRejectionReason,
    SuggestionValidationRequest,
    SuggestionValidationResult,
    facts_from_json,
)

_WORKER_MODULE = "app.modules.reviews.infrastructure.ast_worker"
_PROJECT_ROOT = str(Path(__file__).resolve().parents[4])
# Wall-clock allowance over the CPU budget of a file: process start and pipe latency.
_WALL_SLACK_SECONDS = 2.0
_STARTUP_SECONDS = 15.0
_STREAM_LIMIT = 16 * 1024 * 1024


class SubprocessSourceParser:
    """``SourceParser`` backed by a worker process that lives until ``aclose``.

    The worker is started on the first file and restarted after it is killed or dies, so
    a run pays for one process start in the usual case. ``attempt_cpu_seconds`` is the CPU
    budget of the parser's lifetime; ``None`` leaves only the per-file limit.
    """

    def __init__(
        self,
        *,
        python: str = sys.executable,
        attempt_cpu_seconds: float | None = MAX_PARSE_CPU_SECONDS_PER_ATTEMPT,
    ) -> None:
        self._python = python
        self._attempt_cpu_seconds = attempt_cpu_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._cpu_spent = 0.0

    async def aclose(self) -> None:
        if self._process is not None:
            await self._stop(self._process)
            self._process = None

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        results: dict[str, AstFacts | AstFailureReason] = {}
        for request in requests:
            if len(request.source) > MAX_SOURCE_BYTES:
                results[request.key] = AstFailureReason.SOURCE_TOO_LARGE
                continue
            try:
                request.source.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                results[request.key] = AstFailureReason.SOURCE_DECODE_FAILED
                continue
            results[request.key] = await self._parse(request)
        return results

    async def validate_suggestions(
        self, requests: tuple[SuggestionValidationRequest, ...]
    ) -> dict[str, SuggestionValidationResult]:
        results: dict[str, SuggestionValidationResult] = {}
        for request in requests:
            if len(request.source) > MAX_SOURCE_BYTES:
                results[request.key] = SuggestionValidationResult(
                    is_valid=True,
                    reason=SuggestionRejectionReason.FILE_TOO_LARGE,
                    detail="File exceeds MAX_SOURCE_BYTES",
                )
                continue
            try:
                request.source.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                results[request.key] = SuggestionValidationResult(
                    is_valid=True,
                    reason=SuggestionRejectionReason.NON_UTF8,
                    detail="Source cannot be decoded as utf-8",
                )
                continue
            results[request.key] = await self._validate_suggestion(request)
        return results

    async def _validate_suggestion(
        self, request: SuggestionValidationRequest
    ) -> SuggestionValidationResult:
        budget = self._attempt_cpu_seconds
        if budget is not None and self._cpu_spent >= budget:
            return SuggestionValidationResult(
                is_valid=True,
                reason=SuggestionRejectionReason.AST_TIMEOUT,
                detail="Attempt parser CPU budget exhausted",
            )
        first = self._process is None or self._process.returncode is not None
        if first:
            self._process = await self._start()
        process = self._process
        assert process is not None and process.stdin is not None and process.stdout is not None
        payload = {
            "action": "validate_suggestion",
            "key": request.key,
            "language": request.language.value,
            "source": base64.b64encode(request.source).decode("ascii"),
            "start_line": request.start_line,
            "line": request.line,
            "suggestion": request.suggestion,
        }
        wait = MAX_PARSE_CPU_SECONDS_PER_FILE + _WALL_SLACK_SECONDS
        try:
            process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            await process.stdin.drain()
            line = await asyncio.wait_for(
                process.stdout.readline(), wait + (_STARTUP_SECONDS if first else 0.0)
            )
        except TimeoutError:
            await self.aclose()
            self._cpu_spent += MAX_PARSE_CPU_SECONDS_PER_FILE
            return SuggestionValidationResult(
                is_valid=True,
                reason=SuggestionRejectionReason.AST_TIMEOUT,
                detail="Parser subprocess timeout",
            )
        except (BrokenPipeError, ConnectionResetError):
            line = b""
        if not line:
            await self.aclose()
            return SuggestionValidationResult(
                is_valid=True,
                reason=SuggestionRejectionReason.WORKER_ERROR,
                detail="Parser subprocess died",
            )
        answer = json.loads(line)
        self._cpu_spent += float(answer.get("cpu", 0.0))
        reason_raw = answer.get("reason")
        reason = SuggestionRejectionReason(reason_raw) if reason_raw is not None else None
        return SuggestionValidationResult(
            is_valid=bool(answer.get("is_valid", True)),
            reason=reason,
            detail=answer.get("detail"),
        )

    async def _parse(self, request: ParseRequest) -> AstFacts | AstFailureReason:
        budget = self._attempt_cpu_seconds
        if budget is not None and self._cpu_spent >= budget:
            return AstFailureReason.AST_TIMEOUT
        first = self._process is None or self._process.returncode is not None
        if first:
            self._process = await self._start()
        process = self._process
        assert process is not None and process.stdin is not None and process.stdout is not None
        payload = {
            "key": request.key,
            "language": request.language.value,
            "source": base64.b64encode(request.source).decode("ascii"),
        }
        wait = MAX_PARSE_CPU_SECONDS_PER_FILE + _WALL_SLACK_SECONDS
        try:
            process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            await process.stdin.drain()
            line = await asyncio.wait_for(
                process.stdout.readline(), wait + (_STARTUP_SECONDS if first else 0.0)
            )
        except TimeoutError:
            await self.aclose()
            self._cpu_spent += MAX_PARSE_CPU_SECONDS_PER_FILE
            return AstFailureReason.AST_TIMEOUT
        except (BrokenPipeError, ConnectionResetError):
            line = b""
        if not line:
            # The worker died on this file: the memory cap, the OS CPU cap or a parser crash.
            await self.aclose()
            return AstFailureReason.AST_RESOURCE_LIMIT
        answer = json.loads(line)
        self._cpu_spent += float(answer.get("cpu", 0.0))
        if "facts" in answer:
            return facts_from_json(answer["facts"])
        return AstFailureReason(answer["error"])

    async def _start(self) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            self._python,
            "-m",
            _WORKER_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=_PROJECT_ROOT,
            # No inherited environment: the worker never sees a token or a database URL.
            env={"PYTHONPATH": _PROJECT_ROOT, "PYTHONDONTWRITEBYTECODE": "1"},
            limit=_STREAM_LIMIT,
        )

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.kill()
        await process.wait()
