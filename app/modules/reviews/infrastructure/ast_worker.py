"""Parser worker process: one JSON request per line in, one JSON result per line out.

The parent starts it with an empty environment, so no credential of the service reaches
it. It reads sources from its pipe only, opens no network connection and never executes
repository content. Address space is capped after the grammars are loaded; the parent
enforces the time limit, because a running parse cannot be interrupted from Python.
"""

from __future__ import annotations

import base64
import json
import resource
import sys
import time

from app.modules.reviews.application.ast_symbols import (
    MAX_PARSE_CPU_SECONDS_PER_FILE,
    MAX_PARSER_MEMORY_BYTES,
    AstFailureReason,
    AstLanguage,
    facts_to_json,
)
from app.modules.reviews.infrastructure.tree_sitter_facts import (
    ParseTimeout,
    _grammar,
    extract_facts,
)


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


def _handle(line: str) -> dict[str, object]:
    request = json.loads(line)
    started = time.process_time()
    result: dict[str, object] = {"key": request["key"]}
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
    for language in AstLanguage:
        _grammar(language)
    _limit_memory()
    for line in sys.stdin:
        sys.stdout.write(json.dumps(_handle(line), separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
