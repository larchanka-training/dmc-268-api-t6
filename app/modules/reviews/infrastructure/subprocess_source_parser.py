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
    facts_from_json,
)

_WORKER_MODULE = "app.modules.reviews.infrastructure.ast_worker"
_PROJECT_ROOT = str(Path(__file__).resolve().parents[4])
# Wall-clock allowance over the CPU budget of a file: process start and pipe latency.
_WALL_SLACK_SECONDS = 2.0
_STARTUP_SECONDS = 15.0
_STREAM_LIMIT = 16 * 1024 * 1024


class SubprocessSourceParser:
    """``SourceParser`` backed by one short-lived worker process per batch."""

    def __init__(self, *, python: str = sys.executable) -> None:
        self._python = python

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        results: dict[str, AstFacts | AstFailureReason] = {}
        pending: list[ParseRequest] = []
        for request in requests:
            if len(request.source) > MAX_SOURCE_BYTES:
                results[request.key] = AstFailureReason.SOURCE_TOO_LARGE
                continue
            try:
                request.source.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                results[request.key] = AstFailureReason.SOURCE_DECODE_FAILED
                continue
            pending.append(request)

        cpu_spent = 0.0
        process: asyncio.subprocess.Process | None = None
        try:
            for index, request in enumerate(pending):
                if cpu_spent >= MAX_PARSE_CPU_SECONDS_PER_ATTEMPT:
                    results[request.key] = AstFailureReason.AST_TIMEOUT
                    continue
                first = process is None or process.returncode is not None
                if first:
                    process = await self._start()
                assert (
                    process is not None and process.stdin is not None and process.stdout is not None
                )
                payload = {
                    "key": str(index),
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
                    await self._stop(process)
                    results[request.key] = AstFailureReason.AST_TIMEOUT
                    cpu_spent += MAX_PARSE_CPU_SECONDS_PER_FILE
                    continue
                except (BrokenPipeError, ConnectionResetError):
                    line = b""
                if not line:
                    # The worker died on this file: the memory cap or a parser crash.
                    await self._stop(process)
                    results[request.key] = AstFailureReason.AST_RESOURCE_LIMIT
                    continue
                answer = json.loads(line)
                cpu_spent += float(answer.get("cpu", 0.0))
                if "facts" in answer:
                    results[request.key] = facts_from_json(answer["facts"])
                else:
                    results[request.key] = AstFailureReason(answer["error"])
        finally:
            if process is not None:
                await self._stop(process)
        return results

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
