"""A worker that arms the real per-request CPU limit without slack, then burns the CPU asked for."""

from __future__ import annotations

import json
import resource
import sys
import time

from app.modules.reviews.infrastructure import ast_worker

# The SIGXCPU death the tests provoke must not leave a core dump.
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
# Without slack a request may spend one to two CPU seconds, which keeps the tests short.
ast_worker._CPU_SLACK_SECONDS = 0

for line in sys.stdin:
    ast_worker._limit_cpu()
    soft, _ = resource.getrlimit(resource.RLIMIT_CPU)
    sys.stderr.write(f"{soft}\n")
    sys.stderr.flush()
    request = json.loads(line)
    deadline = time.process_time() + request["burn"]
    while time.process_time() < deadline:
        pass
    answer = {"key": request["key"], "soft": soft, "cpu": time.process_time()}
    sys.stdout.write(json.dumps(answer) + "\n")
    sys.stdout.flush()
