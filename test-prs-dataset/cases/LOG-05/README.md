# LOG-05 oracle

`JobStore` is the authoritative pending-job store for this synthetic dispatch
service. A publisher returns a receipt: `accepted=False` means the batch was
not accepted and the jobs must remain ready for retry. The pre-image returns
that receipt field. The patch returns the truth value of the receipt object,
which is true even when its `accepted` field is false. `DispatchService.flush`
then marks the whole batch dispatched and removes it from the only pending
store although the publisher accepted none of it. Normal successful delivery
is unchanged.

Ground truth is one `correctness` / `critical` finding on the changed return
expression at line 10, yielding `blocking`. The fixture shows loss of the full
pending batch after a normal publisher refusal. Since the same dispatch path
handles every ready batch, repeated refusals can erase the service's backlog;
there is no alternate retry source in this fixture. This is systemic job loss,
not merely a misleading status flag. The source is synthetic, and neither the
pre-image nor patch contains answer-hint comments.

Run the dependency-free oracle from the repository root. It exercises the
public `flush` result, the pending store, and accepted deliveries for both
publisher outcomes before and after applying the patch to a temporary copy:

```sh
uv run python - <<'PY'
import runpy
import shutil
import subprocess
import tempfile
from pathlib import Path

case = Path("test-prs-dataset/cases/LOG-05").resolve()
ids = ("job-101", "job-102", "job-103")


def observe(source, accepted):
    module = runpy.run_path(str(source))
    store = module["JobStore"]()
    for job_id in ids:
        store.add_ready(job_id)
    delivered = []

    def publish(batch):
        if accepted:
            delivered.extend(batch)
        return module["PublishReceipt"](accepted)

    reported = module["DispatchService"](store).flush(publish)
    return reported, store.ready, tuple(delivered)


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    shutil.copytree(case / "base", root, dirs_exist_ok=True)
    source = root / "queue/durable_jobs.py"
    assert observe(source, False) == (False, ids, ())
    assert observe(source, True) == (True, (), ids)
    subprocess.run(["git", "apply", str(case / "diff.patch")], cwd=root, check=True)
    assert observe(source, False) == (True, (), ())
    assert observe(source, True) == (True, (), ids)
    print("rejected batch retained before patch; all jobs lost after patch")
PY
```
