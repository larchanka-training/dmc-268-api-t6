# SEC-04 oracle and provenance

This case is the `src/flask/sessions.py` hunk from [Pallets Flask PR #5632](https://github.com/pallets/flask/pull/5632). The pre-image is the complete, byte-identical file at [base `7522c4bcdb10449dc919e0ffbdebb92fe66822b5`](https://github.com/pallets/flask/blob/7522c4bcdb10449dc919e0ffbdebb92fe66822b5/src/flask/sessions.py); the patch reproduces the complete file diff to [head `e13373f838ab34027c5a80e16a6cb8262d41eab7`](https://github.com/pallets/flask/blob/e13373f838ab34027c5a80e16a6cb8262d41eab7/src/flask/sessions.py). The PR was merged as [`a20bcff8dc4e263312cfd74c11e432bfe8c194c1`](https://github.com/pallets/flask/commit/a20bcff8dc4e263312cfd74c11e432bfe8c194c1). This is one real source-file hunk, not the PR's unrelated docs, config, or test changes.

Flask's [BSD-3-Clause license at the base revision](https://github.com/pallets/flask/blob/7522c4bcdb10449dc919e0ffbdebb92fe66822b5/LICENSE.txt) is retained in `base/.upstream/LICENSE.txt`. The unchanged source is stored under `base/.upstream/` so the repository's mypy run does not try to type-check Flask without its dependencies. A case-local `.ignore` excludes only this upstream source file from this repository's Ruff settings. The validator applies the original patch paths with `git apply --directory=.upstream --check`.

The added line 322 appends old fallback keys *after* the current key. The [Flask advisory GHSA-4grg-w6v8-c28g](https://github.com/pallets/flask/security/advisories/GHSA-4grg-w6v8-c28g) confirms that `itsdangerous` signs with the last key in this list, so deployments using `SECRET_KEY_FALLBACKS` keep signing new sessions with an old key. The advisory rates this **Low**: sessions remain signed and there is no data integrity loss. Ground truth is one `security` / `low` finding on line 322, yielding `attention`.

From the repository root, this dependency-free oracle executes the patched upstream method with a serializer stub that records the key order:

```sh
uv run python - <<'PY'
import ast
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

case = Path("test-prs-dataset/cases/SEC-04").resolve()
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    shutil.copytree(case / "base", root, dirs_exist_ok=True)
    subprocess.run(
        ["git", "apply", "--directory=.upstream", str(case / "diff.patch")],
        cwd=root,
        check=True,
    )
    source = (root / ".upstream/src/flask/sessions.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SecureCookieSessionInterface")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "get_signing_serializer")
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[]))
    namespace = {"URLSafeTimedSerializer": lambda keys, **kwargs: keys}
    exec(compile(module, "sessions.py", "exec"), namespace)
    interface = SimpleNamespace(key_derivation="hmac", digest_method=None, salt="cookie-session", serializer=None)
    app = SimpleNamespace(secret_key="current", config={"SECRET_KEY_FALLBACKS": ["old"]})
    keys = namespace["get_signing_serializer"](interface, app)
    assert keys == ["current", "old"]
    print(keys)
PY
```

The expected output is `['current', 'old']`. The stub proves the exact changed method passes the old key last; the advisory establishes how the real signer uses that ordering.
