# RES-01 oracle

The pre-image reads a report inside a context manager, which closes the file on return. The patch retains each `TextIO` object in `_open_reports` after `read_report()` returns. An open file descriptor therefore remains for every call until the process exits or another caller explicitly closes those objects. Repeated reads in a long-lived process can exhaust its descriptor limit and prevent subsequent file opens. This is one `performance` / `medium` finding anchored to the added append at line 8, yielding `attention`. The severity reflects a cumulative resource failure; the fixture does not establish a critical, immediate outage.

The following oracle applies the patch to a temporary copy of `base/` and checks the public function's returned text and the observed file state after return:

```sh
uv run python - <<'PY'
import runpy
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

case = Path("test-prs-dataset/cases/RES-01").resolve()
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    shutil.copytree(case / "base", root, dirs_exist_ok=True)
    subprocess.run(["git", "apply", str(case / "diff.patch")], cwd=root, check=True)
    report_path = root / "sample.txt"
    report_path.write_text("daily total: 42\n", encoding="utf-8")
    read_report = runpy.run_path(str(root / "app/report_reader.py"))["read_report"]
    opened = []
    original_open = Path.open

    def observed_open(self, *args, **kwargs):
        stream = original_open(self, *args, **kwargs)
        opened.append(stream)
        return stream

    with patch.object(Path, "open", observed_open):
        assert read_report(report_path) == "daily total: 42\n"
    assert len(opened) == 1 and not opened[0].closed
    opened[0].close()
    print("report read; file remained open after return")
PY
```

The source and data are synthetic. Neither the pre-image nor patch contains answer-hint comments.
