# RES-03 oracle and provenance

This case is the complete `aiohttp/cookiejar.py` source-file diff from [aiohttp PR #7944](https://github.com/aio-libs/aiohttp/pull/7944), which introduced domain/path matching in `CookieJar.filter_cookies()`. GitHub's PR API confirms base `2670e7b08da179e74a643dca8d795fd23fcd282e`, head `ac55d6466963b7bb2834fe3d19a4f3c2a67e82a3`, and original PR author xiangxli. The committed pre-image is byte-identical to the [base file](https://github.com/aio-libs/aiohttp/blob/2670e7b08da179e74a643dca8d795fd23fcd282e/aiohttp/cookiejar.py) (Git blob `6e26b029026e4fce0e32b6d16670f7cdbb27d580`). Applying `diff.patch` produces the byte-identical [head file](https://github.com/aio-libs/aiohttp/blob/ac55d6466963b7bb2834fe3d19a4f3c2a67e82a3/aiohttp/cookiejar.py) (Git blob `aa63d280079900f72f60c87a9ef1c5af0919bce5`). Its 31 added and 33 removed source lines match the PR changed-files API. Other PR files are outside this case.

The source is licensed under [Apache-2.0 at the base revision](https://github.com/aio-libs/aiohttp/blob/2670e7b08da179e74a643dca8d795fd23fcd282e/LICENSE.txt). The complete license notice is retained at `base/.upstream/LICENSE.txt`, and attribution is recorded in `case.json`. The unchanged upstream source is under `base/.upstream/`; this repository's Ruff settings differ from aiohttp's, so a case-local `.ignore` excludes only that source file. The validator uses `git apply --directory=.upstream --check` with original upstream paths.

The added line 288 accesses `self._cookies[p]` for every candidate domain/path pair. `_cookies` is a `defaultdict(SimpleCookie)`, so each unseen pair creates an empty bucket. Calls for distinct URL paths retain these buckets in a long-lived `CookieJar`, even when the same cookie is returned. [aiohttp issue #11052](https://github.com/aio-libs/aiohttp/issues/11052) reports growth from about 100 MB to 5 GB over weeks, and the maintainers' [fix PR #11054](https://github.com/aio-libs/aiohttp/pull/11054) identifies this lookup as the cause and guards it. Ground truth is one `performance` / `high` finding on the added line 288, yielding `blocking`: documented multi-GB unbounded growth puts a long-lived client process at serious risk of memory exhaustion. The evidence does not establish an immediate, universal outage, so this is not marked `critical`.

The oracle below executes the exact upstream `filter_cookies` method before and after applying the patch. It supplies only the surrounding URL and jar state with standard-library fakes, so no aiohttp installation or network access is needed. Forty distinct request paths return the same seeded cookie; the pre-image retains one bucket, while the patched method retains 85.

```sh
uv run python - <<'PY'
import ast
import contextlib
import itertools
import shutil
import subprocess
import tempfile
import warnings
from collections import defaultdict
from http.cookies import BaseCookie, Morsel, SimpleCookie
from pathlib import Path
from typing import cast

case = Path("test-prs-dataset/cases/RES-03").resolve()

class URL:
    def __init__(self, path="/"):
        self.raw_host = "example.com"
        self.scheme = "https"
        self.path = path

class Jar:
    def __init__(self):
        self._cookies = defaultdict(SimpleCookie)
        self._cookies[("example.com", "")]["session"] = "abc"
        self._cookies[("example.com", "")]["session"]["domain"] = "example.com"
        self._cookies[("example.com", "")]["session"]["path"] = "/"
        self._quote_cookie = True
        self._treat_as_secure_origin = []
        self._unsafe = False
        self._host_only_cookies = set()

    def _do_expiration(self):
        pass

    def __iter__(self):
        for bucket in self._cookies.values():
            yield from bucket.values()

    def _is_domain_match(self, domain, hostname):
        return domain == hostname

    def _is_path_match(self, request_path, cookie_path):
        return request_path.startswith(cookie_path)

def method_from(source):
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CookieJar")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "filter_cookies")
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[]))
    namespace = {
        "URL": URL, "SimpleCookie": SimpleCookie, "BaseCookie": BaseCookie,
        "Morsel": Morsel, "cast": cast, "warnings": warnings,
        "contextlib": contextlib, "itertools": itertools,
        "is_ip_address": lambda _: False,
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["filter_cookies"]

def bucket_count(source):
    jar = Jar()
    filter_cookies = method_from(source)
    for number in range(1, 41):
        filtered = filter_cookies(jar, URL(f"/report/{number}"))
        assert filtered["session"].value == "abc"
    return len(jar._cookies)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    shutil.copytree(case / "base", root, dirs_exist_ok=True)
    source = root / ".upstream/aiohttp/cookiejar.py"
    before = bucket_count(source)
    subprocess.run(["git", "apply", "--directory=.upstream", str(case / "diff.patch")], cwd=root, check=True)
    after = bucket_count(source)
    assert (before, after) == (1, 85)
    print(f"cookie buckets: {before} before, {after} after")
PY
```
