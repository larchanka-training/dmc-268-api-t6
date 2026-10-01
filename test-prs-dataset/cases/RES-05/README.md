# RES-05 oracle

`SettlementBatchWorker` keeps the two most recent batch payloads for diagnostics in a bounded `deque`. It has an 8 MiB retained-payload budget and latches into a stopped state if that budget is exceeded; a stopped worker rejects all later batches before calling its writer. The patch replaces the bounded deque with an unbounded list. Distinct 3 MiB batches now accumulate: the third batch raises `WorkerStopped`, and the fourth is rejected immediately. In the fixture's single long-lived settlement worker, this permanently halts settlement processing until the worker is replaced.

Ground truth is one `performance` / `critical` finding on the added list initialization at line 15, yielding `blocking`. The severity is based on the reproducible, sustained loss of the worker's core service after ordinary repeated batches under its explicit memory ceiling. It does not claim that every deployment of a similar cache change is critical. The source is synthetic, and neither source nor patch contains answer-hint comments.

This dependency-free oracle runs the exact pre-image and patched code. It creates a fresh 3 MiB byte string for each batch so the retained objects represent actual growth, and its writer records only lengths:

```sh
uv run python - <<'PY'
import runpy
import shutil
import subprocess
import tempfile
from pathlib import Path

case = Path("test-prs-dataset/cases/RES-05").resolve()


def observe(source):
    module = runpy.run_path(str(source))
    written = []
    worker = module["SettlementBatchWorker"](lambda payload: written.append(len(payload)))
    outcomes = []
    for number in range(1, 5):
        try:
            worker.process_batch(bytes([number]) * (3 * 1024 * 1024))
            outcomes.append("ok")
        except module["WorkerStopped"]:
            outcomes.append("stopped")
    return outcomes, written, worker.retained_bytes


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    shutil.copytree(case / "base", root, dirs_exist_ok=True)
    source = root / "services/settlement_worker.py"
    before = observe(source)
    assert before == (["ok", "ok", "ok", "ok"], [3145728] * 4, 6291456)
    subprocess.run(["git", "apply", str(case / "diff.patch")], cwd=root, check=True)
    after = observe(source)
    assert after == (["ok", "ok", "stopped", "stopped"], [3145728] * 2, 9437184)
    print("four batches written before; worker stops after two with patch")
PY
```
