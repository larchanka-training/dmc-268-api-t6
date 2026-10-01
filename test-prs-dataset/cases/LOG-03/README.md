# LOG-03 oracle and provenance

This case is the complete `snap7/server/__init__.py` source-file diff from [python-snap7 PR #806](https://github.com/gijzelaerr/python-snap7/pull/806), which extended the pure-Python server's COTP Connection Confirm to echo the negotiated TPDU size. GitHub's PR API confirms base `d72b66fb4a7e64c7c11ea9caa0ad64d4345d34c9`, head `bd11af442dd0a710b816c408d4ec180a8f5e89ee`, and author gijzelaerr. The committed pre-image is byte-identical to the [base source](https://github.com/gijzelaerr/python-snap7/blob/d72b66fb4a7e64c7c11ea9caa0ad64d4345d34c9/snap7/server/__init__.py) (Git blob `96b4797a5318bed83490ad73642f55be6d2df377`). Applying `diff.patch` yields the byte-identical [head source](https://github.com/gijzelaerr/python-snap7/blob/bd11af442dd0a710b816c408d4ec180a8f5e89ee/snap7/server/__init__.py) (Git blob `a9591517834c1374d5a08576165d9201f730c3da`). No other source files changed in this PR.

The source is [MIT-licensed at the base revision](https://github.com/gijzelaerr/python-snap7/blob/d72b66fb4a7e64c7c11ea9caa0ad64d4345d34c9/LICENSE). The complete license notice is retained in `base/.upstream/LICENSE`, with attribution in `case.json`. Upstream code is kept under `base/.upstream/` so this repository's strict Python checks do not reinterpret third-party code; a case-local Ruff `.ignore` names only that source file. The validator applies the original patch with `--directory=.upstream`.

The PR adds a `0x00` octet at line 2634, labelled `Reserved / CDT`, after the COTP CC type byte. For class-0 COTP CC, the low nibble of the type byte already encodes CDT. The extra octet makes the fixed part seven octets while `pdu_length = 6 + len(pdu_size_param)` still declares six. `_build_cotp_cc()` therefore emits a length indicator of 9 followed by 10 octets, shifting the destination reference and variable parameter. The pre-image emitted LI 6 followed by exactly 6 octets. [Issue #813](https://github.com/gijzelaerr/python-snap7/issues/813) reports that strict clients reject the malformed handshake; this defect was introduced by #806. Ground truth is one `correctness` / `high` finding on added line 2634, yielding `blocking`: it prevents connection establishment for strict clients of this server, but the scope is limited to that client/server combination.

The oracle extracts the exact upstream `ServerISOConnection` class with the Python AST and calls its unchanged public frame-building method. It supplies only the connection state, runs before and after the patch, and checks the declared versus actual COTP length. No PLC, third-party dependency, or network is needed. Run it from the repository root:

```sh
uv run python - <<'PY'
import ast
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

case = Path('test-prs-dataset/cases/LOG-03').resolve()

def response(source: Path) -> bytes:
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ServerISOConnection')
    module = ast.fix_missing_locations(ast.Module(
        body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls],
        type_ignores=[],
    ))
    namespace = {'struct': struct}
    exec(compile(module, str(source), 'exec'), namespace)
    connection = object.__new__(namespace['ServerISOConnection'])
    connection.dst_ref = 15
    connection.src_ref = 1
    connection.tpdu_size = 9
    return connection._build_cotp_cc()

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    shutil.copytree(case / 'base', root, dirs_exist_ok=True)
    source = root / '.upstream/snap7/server/__init__.py'
    before = response(source)
    subprocess.run(['git', 'apply', '--directory=.upstream', str(case / 'diff.patch')], cwd=root, check=True)
    after = response(source)
    assert before.hex() == '06d0000f000100'
    assert after.hex() == '09d000000f000100c00109'
    assert before[0] == len(before) - 1 == 6
    assert after[0] == 9 and len(after) - 1 == 10
    print('COTP CC: LI 6 / actual 6 before; LI 9 / actual 10 after')
PY
```
