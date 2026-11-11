"""Relative Markdown links (#270, Part 2): ``.github/scripts/check_md_links.py`` run in a git
repository.

The script reads the tracked ``*.md`` files under ``docs/`` and ``.agents/``, ``AGENTS.md`` and
``test-prs-dataset/README.md``, and lists every relative link whose file is not tracked or whose
heading anchor does not exist, as ``path:line: target: reason``. External URLs are not checked.
The anchors below are the ids GitHub renders for these headings (taken from the rendered pages of
this repository and of ui). The headings of ``TARGET`` from ``_private_`` on follow the GFM spec
instead, no page here has one: a code span keeps its underscores, ``_note_`` is emphasis,
escaped ``\\_`` and ``\\[`` are text, an entity is its character, inline HTML and an image
add no text, an autolink adds its URL, closing ``#`` are not text, and a heading inside a fence
is code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github/scripts/check_md_links.py"

# An undefined reference ([context][rag]) stays literal text on GitHub.
EPIC = (
    "### Граф задач: эпик [api#139](https://github.com/larchanka-training/dmc-268-api-t6/issues/139)"
    " [context][rag] Graph RAG: граф символов репозитория как контекст ревью"
)

TARGET = f"""# B

## 8. Пользовательская документация

### `summary 30d`: `_terminal_heads` с JIT и без

## 3. Layout & boundaries

## «In Progress с открытым блокером»

# 1. `copy-run-link` — [ui] Добавить кнопку «Скопировать ссылку» на страницу прогона

{EPIC}

## Dup

## Dup

<a id="gitlab-orphan-stage"></a>

## `_private_` helper

## A _note_ here

## \\_Not emphasis\\_

## Literal \\[link](x)

## Fish &amp; chips

## Install <kbd>uv</kbd>

## See <https://example.com/x>

## Logo ![alt text](img.png) here

## Closed heading ##

<a name="legacy-anchor"></a>

```md
## Fenced heading
```
"""

TREE = {
    "AGENTS.md": "# Agents\n",
    "docs/b.md": TARGET,
    "docs/sub/c.md": "# C\n",
    "docs/my file.md": "# M\n",
    "docs/img.png": "png\n",
    "app/x.py": "x = 1\n",
}


def _git_env(root: Path) -> dict[str, str]:
    """No inherited ``GIT_*`` key (a hook's ``GIT_INDEX_FILE``), no user or system config, and no
    repository above ``root``."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env |= {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(root.parent),
    }
    return env


def _repo(root: Path, files: dict[str, str]) -> Path:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=_git_env(root))
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=_git_env(root))
    return root


def _check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=root,
        env=_git_env(root),
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def _page(link: str) -> str:
    return f"# A\n\n{link}\n"


def test_resolving_links_pass_with_a_count(tmp_path: Path) -> None:
    # The fourth is the image in a heading of docs/b.md.
    page = "# A\n\nSee [b](b.md), [agents](../AGENTS.md) and [c](sub/c.md#c).\n"

    result = _check(_repo(tmp_path, {**TREE, "docs/a.md": page}))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["All 4 relative links in 5 Markdown files resolve."]


@pytest.mark.parametrize(
    "link",
    [
        "[root](/docs/b.md)",
        "[dot](./b.md)",
        "[dir](sub/)",
        "[dir](sub)",
        "![img](img.png)",
        '[titled](b.md "B")',
        "[angle](<b.md>)",
        "[ref]: b.md",
        "[self](#a)",
        "[line](../app/x.py#L1)",
        "[h](b.md#8-пользовательская-документация)",
        "[h](b.md#8-%D0%BF%D0%BE%D0%BB%D1%8C%D0%B7%D0%BE%D0%B2%D0%B0%D1%82%D0%B5%D0%BB%D1%8C%D1%81%D0%BA%D0%B0%D1%8F-%D0%B4%D0%BE%D0%BA%D1%83%D0%BC%D0%B5%D0%BD%D1%82%D0%B0%D1%86%D0%B8%D1%8F)",
        "[h](b.md#summary-30d-_terminal_heads-с-jit-и-без)",
        "[h](b.md#3-layout--boundaries)",
        "[h](b.md#in-progress-с-открытым-блокером)",
        "[h](b.md#1-copy-run-link--ui-добавить-кнопку-скопировать-ссылку-на-страницу-прогона)",
        "[h](b.md#граф-задач-эпик-api139-contextrag-graph-rag-граф-символов-репозитория-как-контекст-ревью)",
        "[h](b.md#dup)",
        "[h](b.md#dup-1)",
        "[h](b.md#gitlab-orphan-stage)",
        "[h](b.md#_private_-helper)",
        "[h](b.md#a-note-here)",
        "[h](b.md#_not-emphasis_)",
        "[h](b.md#literal-linkx)",
        "\\[not a link](gone.md)",
        "\\\\\\[still text](gone.md)",
        "[h](b.md#fish--chips)",
        "[h](b.md#install-uv)",
        "[h](b.md#see-httpsexamplecomx)",
        "[h](b.md#logo--here)",
        "[h](b.md#closed-heading)",
        "[h](b.md#legacy-anchor)",
        "[query](b.md?plain=1)",
        "[space](my%20file.md)",
        "[root](/)",
        "[up](../)",
    ],
    ids=[
        "root-relative",
        "dot",
        "directory-slash",
        "directory",
        "image",
        "title",
        "angle-brackets",
        "reference-definition",
        "same-file-anchor",
        "line-anchor-on-code",
        "cyrillic-anchor",
        "percent-encoded-anchor",
        "code-span-heading",
        "ampersand-heading",
        "guillemets-heading",
        "dash-and-brackets-heading",
        "link-and-brackets-in-heading",
        "first-duplicate",
        "second-duplicate",
        "html-anchor",
        "code-span-keeps-underscores",
        "underscore-emphasis",
        "escaped-underscores",
        "escaped-bracket-in-heading",
        "escaped-bracket-is-text",
        "escaped-backslash-then-escaped-bracket",
        "entity",
        "inline-html",
        "autolink",
        "image-in-heading",
        "closing-hashes",
        "html-name-anchor",
        "query",
        "percent-encoded-path",
        "root-directory",
        "parent-directory",
    ],
)
def test_a_link_to_a_tracked_file_or_heading_passes(tmp_path: Path, link: str) -> None:
    result = _check(_repo(tmp_path, {**TREE, "docs/a.md": _page(link)}))

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("link", "line"),
    [
        ("[g](gone.md)", "docs/a.md:3: gone.md: no such file"),
        ("[g](/gone.md)", "docs/a.md:3: /gone.md: no such file"),
        ("[g](sub/gone/)", "docs/a.md:3: sub/gone/: no such file"),
        ("![g](gone.png)", "docs/a.md:3: gone.png: no such file"),
        ("[r]: gone.md", "docs/a.md:3: gone.md: no such file"),
        ("[g](../app/gone.py#L1)", "docs/a.md:3: ../app/gone.py#L1: no such file"),
        ("[g](b.md#nope)", "docs/a.md:3: b.md#nope: no such anchor"),
        ("[g](#nope)", "docs/a.md:3: #nope: no such anchor"),
        ("[g](b.md#dup-2)", "docs/a.md:3: b.md#dup-2: no such anchor"),
        ("[g](b.md#Dup)", "docs/a.md:3: b.md#Dup: no such anchor"),
        ("[g](b.md#fenced-heading)", "docs/a.md:3: b.md#fenced-heading: no such anchor"),
        ("[g](gone.md?plain=1)", "docs/a.md:3: gone.md?plain=1: no such file"),
        ("\\\\[g](gone.md)", "docs/a.md:3: gone.md: no such file"),
    ],
    ids=[
        "file",
        "root-relative-file",
        "directory",
        "image",
        "reference-definition",
        "code-file",
        "anchor",
        "same-file-anchor",
        "third-duplicate",
        "anchor-case",
        "heading-in-a-fence",
        "query",
        "link-after-an-escaped-backslash",
    ],
)
def test_a_broken_link_fails_with_its_file_and_line(tmp_path: Path, link: str, line: str) -> None:
    result = _check(_repo(tmp_path, {**TREE, "docs/a.md": _page(link)}))

    assert result.returncode == 1, result.stdout + result.stderr
    assert line in result.stdout.splitlines()


def test_every_broken_link_is_listed(tmp_path: Path) -> None:
    files = {
        **TREE,
        "AGENTS.md": "# Agents\n\n[a](docs/gone.md)\n",
        "docs/a.md": "# A\n\n[x](gone.md) and [y](b.md#nope)\n\n[z](b.md#dup)\n[w](sub/gone.md)\n",
    }

    result = _check(_repo(tmp_path, files))

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "AGENTS.md:3: docs/gone.md: no such file",
        "docs/a.md:3: gone.md: no such file",
        "docs/a.md:3: b.md#nope: no such anchor",
        "docs/a.md:6: sub/gone.md: no such file",
        "4 broken relative links.",
    ]


@pytest.mark.parametrize(
    "link",
    [
        "[e](https://example.com/gone.md)",
        "[e](http://example.com/gone.md#nope)",
        "[e](mailto:someone@example.com)",
        "[e](//example.com/gone.md)",
    ],
    ids=["https", "http-with-anchor", "mailto", "protocol-relative"],
)
def test_external_urls_are_not_checked(tmp_path: Path, link: str) -> None:
    result = _check(_repo(tmp_path, {**TREE, "docs/a.md": _page(link)}))

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "text",
    [
        "# A\n\n```md\n[g](gone.md)\n```\n",
        "# A\n\n~~~\n[g](gone.md)\n~~~\n",
        "# A\n\n1. Step\n\n    ```\n    [g](gone.md)\n    ```\n",
        "# A\n\nWrite `[g](gone.md)` like this.\n",
    ],
    ids=["backtick-fence", "tilde-fence", "fence-in-a-list", "code-span"],
)
def test_links_inside_code_are_not_checked(tmp_path: Path, text: str) -> None:
    result = _check(_repo(tmp_path, {**TREE, "docs/a.md": text}))

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("docs/deep/x.md", "docs/deep/x.md:3: gone.md: no such file"),
        (".agents/skills/s/SKILL.md", ".agents/skills/s/SKILL.md:3: gone.md: no such file"),
        ("test-prs-dataset/README.md", "test-prs-dataset/README.md:3: gone.md: no such file"),
    ],
    ids=["docs", "agents", "dataset-readme"],
)
def test_files_in_scope_are_checked(tmp_path: Path, path: str, line: str) -> None:
    result = _check(_repo(tmp_path, {**TREE, path: _page("[g](gone.md)")}))

    assert result.returncode == 1, result.stdout + result.stderr
    assert line in result.stdout.splitlines()


@pytest.mark.parametrize(
    "path",
    ["README.md", "src/notes.md", "test-prs-dataset/cases/README.md", "docs/notes.txt"],
    ids=["root-readme", "other-directory", "dataset-subdirectory", "not-markdown"],
)
def test_files_out_of_scope_are_not_read(tmp_path: Path, path: str) -> None:
    result = _check(_repo(tmp_path, {**TREE, path: _page("[g](gone.md)")}))

    assert result.returncode == 0, result.stdout + result.stderr


def test_untracked_files_are_neither_read_nor_link_targets(tmp_path: Path) -> None:
    root = _repo(tmp_path, {**TREE, "docs/a.md": _page("[p](plans/p.md)")})
    (root / "docs/plans").mkdir()
    (root / "docs/plans/p.md").write_text(_page("[g](gone.md)"), encoding="utf-8")

    result = _check(root)

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "docs/a.md:3: plans/p.md: no such file",
        "1 broken relative link.",
    ]


def test_outside_a_git_repository_it_stops_with_exit_2(tmp_path: Path) -> None:
    result = _check(tmp_path)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "not a git work tree" in result.stderr
