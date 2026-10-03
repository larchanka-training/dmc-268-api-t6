"""Contract tests for scripts/issue-preflight.sh.

Offline: remote-URL parsing is exercised by sourcing the bash functions
directly, and every probe goes through the ISSUE_FIXTURE_DIR seam, so this
suite needs no network and no tokens. The remote-resolution tests run against
a throwaway local git repository.

The assertion that matters most is the probe-classification group: an
unreachable host and a rejected token must produce different exit codes AND
different words, because telling a user "your token is bad" when the network is
down sends them to fix something that is not broken.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/create-issue/tests/test_preflight.py

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "issue-preflight.sh"
GOOD_AUTH = '{"login": "octocat"}'
TIMEOUT = 20  # seconds; the script makes no real network call under the fixture seam


def call_fn(fn: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Source the script's function definitions without running the main flow."""
    body = SCRIPT.read_text(encoding="utf-8")
    # everything up to the first executable line after the function block
    cut = body.index("# ─── resolve repo")
    prelude = body[:cut].replace("set -uo pipefail", "")
    quoted = " ".join(f"'{a}'" for a in args)
    return subprocess.run(
        ["bash", "-c", f"{prelude}\n{fn} {quoted}"],
        capture_output=True,
        text=True,
    )


# ─── remote URL parsing ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url,host,path",
    [
        ("git@github.com:acme/sandbox.git", "github.com", "acme/sandbox"),
        ("ssh://git@ssh.github.com:443/acme/sandbox.git", "ssh.github.com", "acme/sandbox"),
        ("ssh://git@git.example.com:2222/grp/proj.git", "git.example.com", "grp/proj"),
        ("https://github.com/acme/sandbox.git", "github.com", "acme/sandbox"),
        # the form that a naive ${url#https://*/} mangles
        ("https://oauth2:TOKEN@github.com/acme/sandbox.git", "github.com", "acme/sandbox"),
        ("git://github.com/acme/x.git", "github.com", "acme/x"),
        # a password may itself contain "@": the host is what follows the LAST one
        ("https://u:p@ss@github.com/acme/sandbox.git", "github.com", "acme/sandbox"),
    ],
)
def test_parse_remote(url: str, host: str, path: str) -> None:
    r = call_fn("parse_remote", url)
    assert r.returncode == 0, r.stderr
    got_host, got_path = r.stdout.strip().split("\t")
    assert (got_host, got_path) == (host, path)


def test_parse_remote_rejects_garbage() -> None:
    assert call_fn("parse_remote", "not-a-url").returncode != 0


@pytest.mark.parametrize(
    "url",
    ["https://github.com", "https://github.com/", "ssh://git@github.com", "git@github.com:"],
)
def test_parse_remote_rejects_a_bare_host(url: str) -> None:
    """A host with no path is not a repository."""
    assert call_fn("parse_remote", url).returncode != 0


# ─── host alias table ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "remote,api",
    [
        ("ssh.github.com", "github.com"),  # real DNS alias, not derivable
        ("github.com", "github.com"),
        ("git.example.com", "git.example.com"),
    ],
)
def test_api_host_alias(remote: str, api: str) -> None:
    assert call_fn("api_host_for", remote).stdout.strip() == api


# ─── probe classification, via the fixture seam ─────────────────────────────
def write_fixtures(
    tmp_path: Path,
    *,
    reach_code: str = "0",
    reach_http: str = "200",
    auth_code: str = "0",
    auth_user: str = GOOD_AUTH,
    perms: str = "true",
) -> Path:
    fix = tmp_path / "fix"
    fix.mkdir(exist_ok=True)
    (fix / "reach.code").write_text(reach_code)
    (fix / "reach.http").write_text(reach_http)
    (fix / "auth_user.code").write_text(auth_code)
    (fix / "auth_user.json").write_text(auth_user)
    (fix / "repo_perms.json").write_text(perms)
    return fix


def preflight(
    tmp_path: Path, *extra: str, repo: str | None = "acme/sandbox", **fixtures: str
) -> subprocess.CompletedProcess[str]:
    fix = write_fixtures(tmp_path, **fixtures)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ISSUE_FIXTURE_DIR": str(fix)}
    argv = ["bash", str(SCRIPT)]
    if repo is not None:
        argv += ["--repo", repo]
    # The timeout makes a hanging argument parser fail fast instead of stalling the suite.
    return subprocess.run(
        argv + list(extra), capture_output=True, text=True, env=env, timeout=TIMEOUT
    )


def test_happy_path(tmp_path: Path) -> None:
    r = preflight(tmp_path)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["can_write"] is True and out["user"] == "octocat"
    assert out["repo"] == "acme/sandbox"


def test_output_shape_is_github_only(tmp_path: Path) -> None:
    out = json.loads(preflight(tmp_path).stdout)
    assert set(out) == {"repo", "user", "reachable", "authed", "can_write", "nested_repos"}


def test_unreachable_says_network_not_auth(tmp_path: Path) -> None:
    # the measured signature of a dead network path: TCP connects, then RST
    r = preflight(tmp_path, reach_code="56", reach_http="000")
    assert r.returncode == 5
    msg = json.loads(r.stderr)["error"].lower()
    assert "network" in msg
    # It may mention auth only to rule it out — never to blame it.
    assert "not an auth problem" in msg


def test_tls_failure_is_its_own_message(tmp_path: Path) -> None:
    # a MITM proxy with a bad cert must not read as "the network is down"
    r = preflight(tmp_path, reach_code="60", reach_http="000")
    assert r.returncode == 5
    msg = json.loads(r.stderr)["error"].lower()
    assert "tls" in msg or "certificate" in msg
    assert "proxy" in msg  # names the likely cause
    assert "not auth" in msg


def test_redirect_counts_as_reachable(tmp_path: Path) -> None:
    # Requiring 200 here would make a host that answers 301 on `/` look dead.
    assert preflight(tmp_path, reach_http="301").returncode == 0


def test_auth_failure_says_auth_not_network(tmp_path: Path) -> None:
    r = preflight(tmp_path, auth_code="1", auth_user="{}")
    assert r.returncode == 3
    msg = json.loads(r.stderr)["error"].lower()
    assert "authenticated" in msg
    assert "gh auth login" in msg
    assert "network" not in msg


def test_issues_disabled_is_its_own_exit_code(tmp_path: Path) -> None:
    r = preflight(tmp_path, perms="false")
    assert r.returncode == 4
    assert "issue" in json.loads(r.stderr)["error"]


def test_gate_is_about_filing_not_pushing() -> None:
    """Filing an issue is not pushing. On a public repo any authenticated user
    may open one, so gating on push permission would refuse the ordinary case
    of reporting something upstream from a fork."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert ".permissions.push" not in src
    assert "has_issues" in src


def test_probe_order_reachability_before_auth(tmp_path: Path) -> None:
    """An unreachable host must never reach the auth probe — that is the whole
    point of the ordering. With both broken, the report must be the network."""
    r = preflight(tmp_path, reach_code="56", reach_http="000", auth_code="1", auth_user="{}")
    assert r.returncode == 5
    assert "network" in json.loads(r.stderr)["error"].lower()


# ─── --repo shape ───────────────────────────────────────────────────────────
def test_host_prefixed_repo_is_accepted(tmp_path: Path) -> None:
    out = json.loads(preflight(tmp_path, repo="github.com/acme/sandbox").stdout)
    assert out["repo"] == "acme/sandbox"


@pytest.mark.parametrize(
    "repo",
    [
        "sandbox",
        "acme/sandbox/extra",
        "github.com/acme/sandbox/x",
        "/r",
        "/",
        "acme/",
        "../x",
        "./x",
        "acme/sand box",
        "acme/sandbox;id",
        "acme/$HOME",
    ],
)
def test_repo_that_is_not_owner_slash_repo_is_refused(tmp_path: Path, repo: str) -> None:
    r = preflight(tmp_path, repo=repo)
    assert r.returncode == 2
    assert "OWNER/REPO" in json.loads(r.stderr)["error"]


@pytest.mark.parametrize("argv", [("--repo",), ("--remote",), ("--repo", ""), ("--remote", "")])
def test_an_option_without_a_value_is_refused_not_hung(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    r = preflight(tmp_path, *argv, repo=None)
    assert r.returncode == 2
    assert "needs a value" in json.loads(r.stderr)["error"]


# ─── repo resolution from the git remotes ───────────────────────────────────
def clone_with_remotes(tmp_path: Path, remotes: dict[str, str]) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True, env=env)
    for name, url in remotes.items():
        subprocess.run(["git", "remote", "add", name, url], cwd=clone, check=True, env=env)
    return clone


def preflight_in(cwd: Path, tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    fix = write_fixtures(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "ISSUE_FIXTURE_DIR": str(fix),
        "GIT_CEILING_DIRECTORIES": str(tmp_path.parent),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=TIMEOUT,
    )


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:acme/sandbox.git",
        "https://github.com/acme/sandbox.git",
        "ssh://git@ssh.github.com:443/acme/sandbox.git",  # the alias host
        "https://oauth2:TOKEN@github.com/acme/sandbox",
    ],
)
def test_repo_is_resolved_from_origin(tmp_path: Path, url: str) -> None:
    clone = clone_with_remotes(tmp_path, {"origin": url})
    r = preflight_in(clone, tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["repo"] == "acme/sandbox"


def test_a_remote_with_an_invalid_repository_name_is_refused(tmp_path: Path) -> None:
    clone = clone_with_remotes(tmp_path, {"origin": "git@github.com:acme/sand box.git"})
    r = preflight_in(clone, tmp_path)
    assert r.returncode == 2
    assert "OWNER/REPO" in json.loads(r.stderr)["error"]


@pytest.mark.parametrize(
    "url",
    [
        # the parse FAILS
        "https://u:SECRET@github.com",  # a bare host
        "ftp://u:SECRET@h/x",
        "https://u:SECRET@",
        "ssh://u:SECRET@",
        # the parse works but the remote is refused
        "https://u:SECRET@git.example.com/g/p.git",
        "https://u:SE@CRET@git.example.com/g/p.git",  # "@" inside the password
        "https://u:pa/SECRET@github.com/acme/x",  # "/" inside the password
        "https://u:SECRET@github.com/ac%20me/x",  # a bad repository name
        "https://u:SECRET@github.com/acme/sand box",
    ],
)
def test_a_remote_url_is_never_echoed(tmp_path: Path, url: str) -> None:
    """Remote URLs can carry tokens (`https://user:TOKEN@host/...`); an error that
    quoted one would put the token into stderr and from there into agent logs."""
    clone = clone_with_remotes(tmp_path, {"origin": url})
    r = preflight_in(clone, tmp_path)
    assert r.returncode == 2
    output = r.stdout + r.stderr
    for fragment in ("SECRET", "CRET", "SE@", "u:"):
        assert fragment not in output, fragment
    assert "origin" in json.loads(r.stderr)["error"] or "OWNER/REPO" in r.stderr


def test_two_clones_resolve_to_their_own_repo(tmp_path: Path) -> None:
    """The same script must answer per clone: ui and api share it byte for byte."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ui = clone_with_remotes(tmp_path / "a", {"origin": "git@github.com:acme/web.git"})
    api = clone_with_remotes(tmp_path / "b", {"origin": "git@github.com:acme/api.git"})
    assert json.loads(preflight_in(ui, tmp_path).stdout)["repo"] == "acme/web"
    assert json.loads(preflight_in(api, tmp_path).stdout)["repo"] == "acme/api"


def test_a_remote_on_another_host_is_refused(tmp_path: Path) -> None:
    clone = clone_with_remotes(tmp_path, {"origin": "git@git.example.com:grp/proj.git"})
    r = preflight_in(clone, tmp_path)
    assert r.returncode == 2
    msg = json.loads(r.stderr)["error"]
    assert "git.example.com" in msg and "github.com only" in msg


def test_several_remotes_without_origin_are_ambiguous(tmp_path: Path) -> None:
    clone = clone_with_remotes(
        tmp_path,
        {"one": "git@github.com:acme/one.git", "two": "git@github.com:acme/two.git"},
    )
    r = preflight_in(clone, tmp_path)
    assert r.returncode == 2
    assert "--remote" in json.loads(r.stderr)["error"]
    chosen = preflight_in(clone, tmp_path, "--remote", "two")
    assert json.loads(chosen.stdout)["repo"] == "acme/two"


def test_no_remote_and_not_a_repo_are_refused(tmp_path: Path) -> None:
    bare = clone_with_remotes(tmp_path, {})
    assert preflight_in(bare, tmp_path).returncode == 2
    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    r = preflight_in(elsewhere, tmp_path)
    assert r.returncode == 2
    assert "not inside a git repository" in json.loads(r.stderr)["error"]


# ─── nested repositories ────────────────────────────────────────────────────
def test_nested_repos_are_reported(tmp_path: Path) -> None:
    """Resolving from cwd is syntactically right and can be semantically wrong:
    a repo whose subdirectories are themselves repos resolves to the outer one
    while the work often belongs to a nested one. Preflight cannot decide, so
    it must at least report what it found."""
    root = tmp_path / "outer"
    (root / ".git").mkdir(parents=True)
    for nested in ("projects/alpha", "projects/beta"):
        (root / nested / ".git").mkdir(parents=True)
    (root / "node_modules" / "pkg" / ".git").mkdir(parents=True)

    body = SCRIPT.read_text(encoding="utf-8")
    start = body.index('NESTED=""')
    end = body.index("# ─── probe 1")
    snippet = body[start:end].replace('[ -z "$REPO_ARG" ] && [ -z "$FIX" ]', "true")
    r = subprocess.run(
        [
            "bash",
            "-c",
            f'cd "{root}" || exit 1\n'
            'git() { [ "$2" = "--show-toplevel" ] && echo "' + str(root) + '"; }\n'
            f"{snippet}\nprintf '%s\\n' \"$NESTED\"",
        ],
        capture_output=True,
        text=True,
    )
    out = r.stdout.strip().splitlines()
    assert "projects/alpha" in out and "projects/beta" in out
    # a vendored dependency's repo is noise, not a candidate target
    assert not any("node_modules" in line for line in out)


def test_nested_scan_is_skipped_for_an_explicit_repo(tmp_path: Path) -> None:
    """--repo means the caller already knows the target; scanning the disk then
    would be work done to answer a question nobody asked."""
    out = json.loads(preflight(tmp_path).stdout)
    assert out["nested_repos"] == []
