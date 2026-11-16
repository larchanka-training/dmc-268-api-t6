"""Parser worker process: one JSON request per line in, one JSON result per line out.

The parent starts it with a minimal environment (``PYTHONPATH``, ``PYTHONDONTWRITEBYTECODE``),
so no credential of the service reaches it. It reads sources from its pipe only, opens no
network connection, never executes repository content and writes no core dump. Address space
is capped after the grammars are loaded. A running parse cannot be interrupted from Python,
so the parent enforces the time limit by wall clock; the OS also caps the CPU time of each
request, a backstop should the parent fail to stop it.
"""

from __future__ import annotations

import base64
import json
import math
import resource
import sys
import time

from app.modules.reviews.application.ast_symbols import (
    MAX_PARSE_CPU_SECONDS_PER_FILE,
    MAX_PARSER_MEMORY_BYTES,
    AstFailureReason,
    AstLanguage,
    SuggestionRejectionReason,
    facts_to_json,
)
from app.modules.reviews.infrastructure.suggestion_ast import validate_suggestion_tree
from app.modules.reviews.infrastructure.tree_sitter_facts import (
    ParseTimeout,
    _grammar,
    extract_facts,
)


def _disable_core_dumps() -> None:
    """Never write a core: it would hold the repository sources the worker was given.

    SIGXCPU from the CPU limit dumps core by default, and so does a parser crash. The hard
    limit goes to zero too, since the worker never needs a core.
    """
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _limit_memory() -> None:
    """Cap the address space above what the loaded grammars already use.

    The current size is read from ``/proc``, which only Linux has. Elsewhere (a developer
    machine) the cap is skipped: the parent still stops a worker that does not answer.
    """
    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            current = int(statm.read().split()[0]) * resource.getpagesize()
    except OSError:
        return
    limit = current + MAX_PARSER_MEMORY_BYTES
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


# Whole CPU seconds a request may spend over its file budget before the OS stops the worker.
# The parent waits at most 18 s of wall time for one answer (the file budget, its wall slack
# and the startup allowance of a fresh worker). A single-threaded worker spends no more CPU
# than wall time, so with at least 21 CPU seconds per request the parent's kill comes first.
_CPU_SLACK_SECONDS = 20


def _cpu_soft_limit(used: float) -> int:
    """Soft ``RLIMIT_CPU`` for the next request after ``used`` CPU seconds of the process."""
    return math.ceil(used) + math.ceil(MAX_PARSE_CPU_SECONDS_PER_FILE) + _CPU_SLACK_SECONDS


def _limit_cpu() -> None:
    """Let the next request spend its CPU budget plus the slack, counted from now.

    ``RLIMIT_CPU`` counts the CPU time of the whole process, and one worker serves many
    requests, so the soft limit moves before every request. The hard limit is kept: a
    process cannot raise it again once lowered, and a finite one caps the soft limit. Over
    the soft limit the kernel sends SIGXCPU, which ends the worker; the parent handles that
    like any worker that died.
    """
    _, hard = resource.getrlimit(resource.RLIMIT_CPU)
    soft = _cpu_soft_limit(time.process_time())
    if hard != resource.RLIM_INFINITY:
        soft = min(soft, hard)
    resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))


def _handle(line: str) -> dict[str, object]:
    request = json.loads(line)
    started = time.process_time()
    result: dict[str, object] = {"key": request["key"]}

    if request.get("action") == "validate_suggestion":
        try:
            val_res = validate_suggestion_tree(
                AstLanguage(request["language"]),
                base64.b64decode(request["source"]),
                request["start_line"],
                request["line"],
                request["suggestion"],
            )
            result["is_valid"] = val_res.is_valid
            if val_res.reason is not None:
                result["reason"] = val_res.reason.value
            if val_res.detail is not None:
                result["detail"] = val_res.detail
        except MemoryError:
            result["is_valid"] = True
            result["reason"] = SuggestionRejectionReason.WORKER_ERROR.value
            result["detail"] = "Parser memory limit"
        except Exception as exc:
            result["is_valid"] = True
            result["reason"] = SuggestionRejectionReason.WORKER_ERROR.value
            result["detail"] = str(exc)
        result["cpu"] = time.process_time() - started
        return result

    try:
        facts = extract_facts(
            AstLanguage(request["language"]),
            base64.b64decode(request["source"]),
            cpu_seconds=MAX_PARSE_CPU_SECONDS_PER_FILE,
        )
    except ParseTimeout:
        result["error"] = AstFailureReason.AST_TIMEOUT.value
    except MemoryError:
        result["error"] = AstFailureReason.AST_RESOURCE_LIMIT.value
    else:
        if time.process_time() - started > MAX_PARSE_CPU_SECONDS_PER_FILE:
            result["error"] = AstFailureReason.AST_TIMEOUT.value
        else:
            result["facts"] = facts_to_json(facts)
    result["cpu"] = time.process_time() - started
    return result


def main() -> None:
    _disable_core_dumps()
    for language in AstLanguage:
        _grammar(language)
    _limit_memory()
    for line in sys.stdin:
        _limit_cpu()
        sys.stdout.write(json.dumps(_handle(line), separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
